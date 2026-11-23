"""Checks of the image-build adapter run in process against a stand-in builder.

The adapter drives `buildctl` and reads the pipeline registry. These tests put a
scripted `buildctl` on PATH and serve the registry API from a local HTTP
server, redirecting only the adapter's registry URL.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from sdi_pipeline_integration import _image_build_adapter as adapter
from sdi_pipeline_integration._domain_contracts import ImageBuildResult
from sdi_pipeline_integration._json_input import canonical_json
from sdi_pipeline_integration._service_descriptions import (
    SERVICE_REPOSITORY_ROOT,
    capture_service_repository,
)
from sdi_pipeline_integration._stage_contracts import (
    AdapterResponse,
    StageProfile,
    contains_sensitive_contract_material,
    contains_sensitive_material,
)
from sdi_pipeline_integration._stage_registry import STAGES
from sdi_pipeline_integration._yaml_input import parse_yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

SOURCE_ROOT = Path(__file__).resolve().parents[2]
CASE = SOURCE_ROOT / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
EXECUTION_ID = "0b6f5d1e-2c3a-4d5e-8f60-718293a4b5c6"
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
LOCALIZATION = "fixture-localization"
NAVIGATION = "fixture-navigation"
MISSION = "fixture-mission-control"

FAKE_BUILDCTL = """\
import json, os, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
scenario = json.loads((here / "scenario.json").read_text())
arguments = sys.argv[1:]
with (here / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps({"argv": arguments, "pid": os.getpid(),
        "environment": sorted(os.environ)}) + "\\n")
if arguments[2:4] == ["debug", "workers"]:
    workers = scenario["workers"]
    sys.stdout.write(workers["output"])
    sys.exit(workers["exit"])
options = dict(zip(arguments[::2], arguments[1::2]))
output = next(item for item in arguments if item.startswith("type=image,"))
name = output.split("name=", 1)[1].split(",", 1)[0]
service_id = name.split("/sdi/", 1)[1].split(":", 1)[0]
build = scenario["builds"][service_id]
sys.stderr.write(build.get("log", "#1 DONE 0.0s\\n"))
sys.stderr.flush()
time.sleep(build.get("sleep", 0))
if build.get("exit", 0) == 0:
    metadata = arguments[arguments.index("--metadata-file") + 1]
    Path(metadata).write_text(json.dumps({"containerimage.digest": build["digest"]}))
sys.exit(build.get("exit", 0))
"""


def _description(service_id: str, artifacts: dict[str, Any]) -> str:
    front_matter = {
        "schema_version": "sdi.service-description/v1",
        "service_id": service_id,
        "title": service_id,
        "provenance": {"basis": "placeholder", "note": "An image-build test."},
        "artifacts": artifacts,
    }
    # JSON is YAML, so the front matter needs no YAML writer.
    return f"---\n{json.dumps(front_matter)}\n---\n\n# {service_id}\n"


def _artifact(architectures: list[str], dockerfile: str | None) -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "architectures": architectures,
        "route": {"kind": "image", "reference": "example/test"},
    }
    if dockerfile is not None:
        artifact["dockerfile"] = dockerfile
    return artifact


def _resolved(service_id: str, artifact_id: str = "debs") -> dict[str, str]:
    return {
        "service_id": service_id,
        "status": "resolved",
        "artifact_id": artifact_id,
        "host": "orin",
    }


def _unresolved(service_id: str) -> dict[str, str]:
    return {"service_id": service_id, "status": "unresolved", "host": "orin"}


# Each service's artifacts and the files beside its SDI.md.
type Services = dict[str, tuple[dict[str, Any], dict[str, str]]]

# Three services: a two-platform recipe in a subfolder, a noarch recipe and
# an artifact without a recipe.
SERVICES: Services = {
    LOCALIZATION: (
        {"debs": _artifact(["amd64", "arm64"], "recipes/Dockerfile.debs")},
        {"recipes/Dockerfile.debs": "FROM scratch\nCOPY SDI.md /\n"},
    ),
    NAVIGATION: (
        {
            "debs": _artifact(["amd64"], "Dockerfile.debs"),
            "any": _artifact(["noarch"], "Dockerfile.debs"),
            "bare": _artifact(["amd64"], None),
            "uncaptured": _artifact(["amd64"], "Dockerfile.missing"),
        },
        {"Dockerfile.debs": "FROM scratch\n"},
    ),
    MISSION: ({"debs": _artifact(["arm64"], None)}, {}),
}


class Stage:
    """One prepared adapter invocation and its stand-in builder."""

    def __init__(self, root: Path) -> None:  # noqa: D107
        self.root = root
        self.request = root / "request.json"
        self.input_root = root / "inputs"
        self.output_root = root / "candidate"
        self.bin = root / "bin"
        self.scenario: dict[str, Any] = {
            "workers": {"exit": 0, "output": "linux/amd64, linux/arm64\n"},
            "builds": {},
        }

    def calls(self) -> list[dict[str, Any]]:
        """Return every recorded buildctl invocation."""
        path = self.bin / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def run(self) -> tuple[AdapterResponse, ImageBuildResult | None, str]:
        """Run the adapter and return its response, result and diagnostic."""
        (self.bin / "scenario.json").write_text(json.dumps(self.scenario))
        exit_code = adapter.main(
            [
                "run",
                "--request",
                str(self.request),
                "--input-root",
                str(self.input_root),
                "--output-root",
                str(self.output_root),
            ]
        )
        assert exit_code == 0
        response = AdapterResponse.model_validate_json(
            (self.output_root / "response.json").read_bytes(), strict=False
        )
        result_path = self.output_root / "outputs/image-build-result.json"
        result = None
        if result_path.exists():
            # The runtime rejects a candidate holding sensitive material.
            assert not contains_sensitive_contract_material(
                json.loads(result_path.read_bytes())
            )
            result = ImageBuildResult.model_validate_json(result_path.read_bytes())
        diagnostic = (self.output_root / "diagnostic.txt").read_text()
        assert not contains_sensitive_material(diagnostic)
        assert (
            len(diagnostic.encode()) <= STAGES["image_build"].grant.diagnostic_max_bytes
        )
        return response, result, diagnostic


def _write_inputs(
    stage: Stage,
    placements: list[dict[str, str]],
    *,
    services: Services = SERVICES,
    deployment_repository_digest: str | None = None,
    work_limit_seconds: int = 1800,
) -> None:
    repository = stage.input_root / SERVICE_REPOSITORY_ROOT
    for service_id, (artifacts, files) in services.items():
        folder = repository / service_id
        folder.mkdir(parents=True)
        (folder / "SDI.md").write_text(_description(service_id, artifacts))
        for name, content in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(content)
    manifest = capture_service_repository(repository).manifest
    profile_path = SOURCE_ROOT / "integration/stage-profiles/image-build-v1.yaml"
    profile = StageProfile.model_validate(
        parse_yaml(profile_path.read_bytes(), str(profile_path))
    )
    repository_bytes = canonical_json(manifest.model_dump(mode="json"))
    repository_digest = hashlib.sha256(repository_bytes).hexdigest()
    deployment = json.loads((CASE / "deployment-schema.json").read_text())
    deployment |= {
        "evidence_basis": "implemented",
        "placements": placements,
        "source_inputs": [
            item
            | (
                {"sha256": deployment_repository_digest or repository_digest}
                if item["slot"] == "service_repository"
                else {}
            )
            for item in deployment["source_inputs"]
        ],
    }
    blueprint = json.loads((CASE / "composition-blueprint.json").read_text())
    contents = {
        "target_profile": (
            SOURCE_ROOT / "profiles/s-04/waffle-jetson-arm64.yaml"
        ).read_bytes(),
        "service_repository": repository_bytes,
        "composition_blueprint": canonical_json(
            blueprint | {"evidence_basis": "implemented"}
        ),
        "deployment_schema": canonical_json(deployment),
    }
    inputs: list[dict[str, Any]] = []
    for grant in profile.inputs:
        content = contents[grant.slot]
        (stage.input_root / grant.path).write_bytes(content)
        inputs.append(
            grant.model_dump(mode="json")
            | {
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    request = {
        "schema_version": "sdi.stage-adapter-request/v1",
        "correlation": {
            "execution_id": EXECUTION_ID,
            "stage": "image_build",
            "attempt_number": 1,
        },
        "work_limit_seconds": work_limit_seconds,
        "inputs": inputs,
        "outputs": [item.model_dump(mode="json") for item in profile.outputs],
        "diagnostic": profile.diagnostic.model_dump(mode="json"),
    }
    stage.request.write_bytes(canonical_json(request))


class Registry:
    """A pipeline registry that serves the manifests a test stores."""

    def __init__(self) -> None:  # noqa: D107
        self.manifests: dict[str, tuple[str, bytes]] = {}

    def store_index(self, service_id: str, platforms: list[str]) -> str:
        """Store an index over these platforms and return its digest."""
        entries = [
            {
                "mediaType": MANIFEST_TYPE,
                "digest": f"sha256:{hashlib.sha256(name.encode()).hexdigest()}",
                "size": 1,
                "platform": {"os": os_name, "architecture": architecture},
            }
            for name in platforms
            for os_name, architecture in [name.split("/")]
        ]
        body = json.dumps(
            {"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": entries}
        ).encode()
        digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
        self.manifests[f"/v2/sdi/{service_id}/manifests/{digest}"] = (INDEX_TYPE, body)
        return digest


def _platform_digest(name: str) -> str:
    return f"sha256:{hashlib.sha256(name.encode()).hexdigest()}"


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[Registry]:
    store = Registry()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/v2/":
                self.send_response(200)
                self.end_headers()
                return
            found = store.manifests.get(self.path)
            if found is None:
                self.send_response(404)
                self.end_headers()
                return
            media_type, body = found
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        adapter, "REGISTRY_URL", f"http://127.0.0.1:{server.server_port}"
    )
    yield store
    server.shutdown()
    server.server_close()


@pytest.fixture
def stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stage:
    prepared = Stage(tmp_path)
    prepared.input_root.mkdir()
    prepared.output_root.mkdir()
    prepared.bin.mkdir()
    buildctl = prepared.bin / "buildctl"
    buildctl.write_text(f"#!{sys.executable}\n{FAKE_BUILDCTL}")
    buildctl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{prepared.bin}{os.pathsep}{os.environ['PATH']}")
    return prepared


def _without_buildctl(stage: Stage) -> None:
    (stage.bin / "buildctl").unlink()


def _records(result: ImageBuildResult | None) -> dict[str, dict[str, Any]]:
    assert result is not None
    return {
        item.service_id: item.model_dump(mode="json", exclude={"service_id"})
        for item in result.images
    }


def _builds(stage: Stage) -> dict[str, list[str]]:
    builds: dict[str, list[str]] = {}
    for call in stage.calls():
        argv = call["argv"]
        if "build" in argv:
            output = next(item for item in argv if item.startswith("type=image,"))
            service_id = output.split("/sdi/", 1)[1].split(":", 1)[0]
            builds[service_id] = argv
    return builds


def _option(argv: list[str], flag: str, prefix: str) -> str:
    values = [
        value
        for name, value in itertools.pairwise(argv)
        if name == flag and value.startswith(prefix)
    ]
    assert len(values) == 1, (flag, prefix, argv)
    return values[0].removeprefix(prefix)


def test_each_placement_is_recorded_built_or_failed(
    stage: Stage, registry: Registry
) -> None:
    index = registry.store_index(
        LOCALIZATION, ["linux/amd64", "linux/arm64", "unknown/unknown"]
    )
    single = _platform_digest("linux/amd64")
    stage.scenario["builds"] = {
        LOCALIZATION: {"digest": index},
        NAVIGATION: {"digest": single},
    }
    _write_inputs(
        stage,
        [_resolved(LOCALIZATION), _resolved(NAVIGATION), _unresolved(MISSION)],
    )

    response, result, diagnostic = stage.run()

    assert response.execution_conclusion == "succeeded"
    assert response.domain_outcome == "succeeded"
    assert response.reason is not None
    assert response.reason.code == "sdi.image-build.recorded"
    assert response.produced_outputs == ["image_build_result"]
    assert result is not None
    assert result.evidence_basis == "implemented"
    assert result.image_build_result_id == "s-04-tc-03-c-05-image-build"
    assert [item.slot for item in result.source_inputs] == [
        "target_profile",
        "service_repository",
        "composition_blueprint",
        "deployment_schema",
    ]
    assert _records(result) == {
        LOCALIZATION: {
            "status": "built",
            "repository": f"sdi/{LOCALIZATION}",
            "digest": index,
            "platforms": [
                {"architecture": "amd64", "digest": _platform_digest("linux/amd64")},
                {"architecture": "arm64", "digest": _platform_digest("linux/arm64")},
            ],
        },
        NAVIGATION: {
            "status": "built",
            "repository": f"sdi/{NAVIGATION}",
            "digest": single,
            "platforms": [{"architecture": "amd64", "digest": single}],
        },
        MISSION: {
            "status": "failed",
            "reason": "unresolved-placement",
            "detail": "composition left the placement unresolved",
        },
    }
    assert "outcome: sdi.image-build.recorded" in diagnostic


def test_a_build_names_its_recipe_platforms_and_tag(
    stage: Stage, registry: Registry
) -> None:
    stage.scenario["builds"] = {
        LOCALIZATION: {
            "digest": registry.store_index(LOCALIZATION, ["linux/amd64", "linux/arm64"])
        },
        NAVIGATION: {
            "digest": registry.store_index(NAVIGATION, ["linux/amd64", "linux/arm64"])
        },
    }
    _write_inputs(
        stage,
        [_resolved(LOCALIZATION), _resolved(NAVIGATION, "any"), _unresolved(MISSION)],
    )

    stage.run()

    builds = _builds(stage)
    repository = stage.input_root / SERVICE_REPOSITORY_ROOT
    localization = builds[LOCALIZATION]
    assert _option(localization, "--local", "context=") == str(
        repository / LOCALIZATION
    )
    assert _option(localization, "--local", "dockerfile=") == str(
        repository / LOCALIZATION / "recipes"
    )
    assert _option(localization, "--opt", "filename=") == "Dockerfile.debs"
    assert _option(localization, "--opt", "platform=") == "linux/amd64,linux/arm64"
    assert _option(localization, "--output", "type=image,") == (
        f"name=registry:5000/sdi/{LOCALIZATION}:debs-{EXECUTION_ID},"
        "push=true,registry.insecure=true"
    )
    assert "--frontend" in localization
    assert not any(item.startswith("attest") for item in localization)
    # A noarch artifact builds for both architectures.
    assert (
        _option(builds[NAVIGATION], "--opt", "platform=") == "linux/amd64,linux/arm64"
    )


@pytest.mark.usefixtures("registry")
def test_buildctl_runs_with_only_a_fixed_environment(
    stage: Stage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "leak")
    stage.scenario["builds"] = {NAVIGATION: {"digest": _platform_digest("linux/amd64")}}
    _write_inputs(
        stage, [_unresolved(LOCALIZATION), _resolved(NAVIGATION), _unresolved(MISSION)]
    )

    stage.run()

    assert stage.calls()
    for call in stage.calls():
        assert call["environment"] == ["DOCKER_CONFIG", "HOME", "LC_ALL", "PATH"]


@pytest.mark.parametrize(
    "placements",
    [
        [
            _unresolved(LOCALIZATION),
            _resolved(NAVIGATION, "bare"),
            _unresolved(MISSION),
        ],
        [
            _unresolved(LOCALIZATION),
            _resolved(NAVIGATION, "uncaptured"),
            _resolved(MISSION),
        ],
    ],
)
def test_nothing_buildable_is_recorded_without_the_builder(
    stage: Stage, placements: list[dict[str, str]]
) -> None:
    _without_buildctl(stage)
    _write_inputs(stage, placements)

    response, result, _diagnostic = stage.run()

    assert response.domain_outcome == "succeeded"
    records = _records(result)
    assert records[NAVIGATION]["reason"] == "no-recipe"
    assert {records[LOCALIZATION]["reason"]} == {"unresolved-placement"}
    assert records[MISSION]["status"] == "failed"
    assert stage.calls() == []


@pytest.mark.usefixtures("registry")
def test_a_failed_build_records_its_redacted_last_error_line(stage: Stage) -> None:
    log = (Path(__file__).parent / "data/apt-failure.txt").read_text()
    stage.scenario["builds"] = {NAVIGATION: {"exit": 1, "log": log}}
    _write_inputs(
        stage,
        [_unresolved(LOCALIZATION), _resolved(NAVIGATION), _unresolved(MISSION)],
    )

    response, result, diagnostic = stage.run()

    assert response.domain_outcome == "succeeded"
    record = _records(result)[NAVIGATION]
    assert (record["status"], record["reason"]) == ("failed", "build-failed")
    assert record["detail"].startswith("error: failed to solve: process ")
    assert record["detail"].endswith("exit code: 100")
    assert "Unable to locate package ros-jazzy-nav2-controller" in diagnostic
    assert "Get:1 <url> noble InRelease" in diagnostic


@pytest.mark.parametrize("fault", ["platforms", "body"])
def test_an_index_that_does_not_match_the_build_fails_the_placement(
    stage: Stage, registry: Registry, fault: str
) -> None:
    if fault == "platforms":
        digest = registry.store_index(LOCALIZATION, ["linux/amd64"])
    else:
        digest = registry.store_index(LOCALIZATION, ["linux/amd64", "linux/arm64"])
        path = f"/v2/sdi/{LOCALIZATION}/manifests/{digest}"
        media_type, body = registry.manifests[path]
        registry.manifests[path] = (media_type, body + b" ")
    stage.scenario["builds"] = {LOCALIZATION: {"digest": digest}}
    _write_inputs(
        stage, [_resolved(LOCALIZATION), _unresolved(NAVIGATION), _unresolved(MISSION)]
    )

    _response, result, _diagnostic = stage.run()

    record = _records(result)[LOCALIZATION]
    assert (record["status"], record["reason"]) == ("failed", "build-failed")


@pytest.mark.usefixtures("registry")
def test_builds_still_running_at_the_deadline_fail_with_the_work_limit(
    stage: Stage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "DEADLINE_RESERVE_SECONDS", 9)
    monkeypatch.setattr(adapter, "CANCEL_GRACE_SECONDS", 1)
    stage.scenario["builds"] = {
        NAVIGATION: {"digest": _platform_digest("linux/amd64")},
        LOCALIZATION: {"digest": "unused", "sleep": 60},
    }
    _write_inputs(
        stage,
        [_resolved(LOCALIZATION), _resolved(NAVIGATION), _unresolved(MISSION)],
        work_limit_seconds=11,
    )

    response, result, _diagnostic = stage.run()

    assert response.domain_outcome == "succeeded"
    records = _records(result)
    assert records[NAVIGATION]["status"] == "built"
    assert (records[LOCALIZATION]["status"], records[LOCALIZATION]["reason"]) == (
        "failed",
        "work-limit",
    )
    for call in stage.calls():
        with pytest.raises(ProcessLookupError):
            os.kill(call["pid"], 0)


def _preflight(stage: Stage) -> str:
    response, result, _diagnostic = stage.run()
    assert response.execution_conclusion == "failed"
    assert response.domain_outcome == "not_evaluated"
    assert response.produced_outputs == []
    assert result is None
    assert response.reason is not None
    return response.reason.code


def test_a_service_repository_composition_did_not_read_fails_first(
    stage: Stage,
) -> None:
    services: Services = {
        f"svc-{index:02d}": ({"debs": _artifact(["amd64"], None)}, {})
        for index in range(49)
    }
    _write_inputs(
        stage,
        [_resolved(service_id) for service_id in services],
        services=services,
        deployment_repository_digest="0" * 64,
    )

    assert _preflight(stage) == "sdi.image-build.input-mismatch"


def test_more_placements_than_the_result_can_hold_are_refused(stage: Stage) -> None:
    services: Services = {
        f"svc-{index:02d}": ({"debs": _artifact(["amd64"], None)}, {})
        for index in range(49)
    }
    _write_inputs(
        stage, [_resolved(service_id) for service_id in services], services=services
    )

    assert _preflight(stage) == "sdi.image-build.too-many-placements"


@pytest.mark.parametrize("cause", ["refused", "missing"])
@pytest.mark.usefixtures("registry")
def test_an_unreachable_builder_fails_preflight(stage: Stage, cause: str) -> None:
    if cause == "missing":
        _without_buildctl(stage)
    stage.scenario["workers"] = {"exit": 1, "output": ""}
    _write_inputs(
        stage, [_resolved(LOCALIZATION), _unresolved(NAVIGATION), _unresolved(MISSION)]
    )

    assert _preflight(stage) == "sdi.image-build.builder-unreachable"


def test_an_unreachable_registry_fails_preflight(
    stage: Stage, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A port just released, so nothing listens on it.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(adapter, "REGISTRY_URL", f"http://127.0.0.1:{port}")
    _write_inputs(
        stage, [_resolved(LOCALIZATION), _unresolved(NAVIGATION), _unresolved(MISSION)]
    )

    assert _preflight(stage) == "sdi.image-build.registry-unreachable"


@pytest.mark.usefixtures("registry")
def test_arm64_is_required_only_when_a_build_needs_it(stage: Stage) -> None:
    stage.scenario["workers"] = {"exit": 0, "output": "linux/amd64\n"}
    stage.scenario["builds"] = {NAVIGATION: {"digest": _platform_digest("linux/amd64")}}
    _write_inputs(
        stage, [_resolved(LOCALIZATION), _unresolved(NAVIGATION), _unresolved(MISSION)]
    )

    assert _preflight(stage) == "sdi.image-build.arm64-unavailable"


@pytest.mark.usefixtures("registry")
def test_an_amd64_build_does_not_need_arm64(stage: Stage) -> None:
    stage.scenario["workers"] = {"exit": 0, "output": "linux/amd64\n"}
    stage.scenario["builds"] = {NAVIGATION: {"digest": _platform_digest("linux/amd64")}}
    _write_inputs(
        stage, [_unresolved(LOCALIZATION), _resolved(NAVIGATION), _unresolved(MISSION)]
    )

    response, result, _diagnostic = stage.run()

    assert response.domain_outcome == "succeeded"
    assert _records(result)[NAVIGATION]["status"] == "built"


@pytest.mark.usefixtures("registry")
def test_many_failed_builds_share_the_diagnostic_limit(stage: Stage) -> None:
    services: Services = {
        f"svc-{index:02d}": (
            {"debs": _artifact(["amd64"], "Dockerfile")},
            {"Dockerfile": "FROM scratch\n"},
        )
        for index in range(adapter.MAX_PLACEMENTS)
    }
    log = "".join(
        f"#5 {line}.000 E: Unable to fetch some archives\n" for line in range(400)
    )
    stage.scenario["builds"] = {
        service_id: {"exit": 1, "log": f"{log}error: exit code: 100\n"}
        for service_id in services
    }
    _write_inputs(
        stage, [_resolved(service_id) for service_id in services], services=services
    )

    response, result, diagnostic = stage.run()

    assert response.diagnostic.truncated
    assert {record["reason"] for record in _records(result).values()} == {
        "build-failed"
    }
    assert diagnostic.count("build log tail") == adapter.MAX_PLACEMENTS


@pytest.mark.usefixtures("registry")
def test_a_work_limit_inside_the_reserve_starts_no_build(stage: Stage) -> None:
    stage.scenario["builds"] = {NAVIGATION: {"digest": _platform_digest("linux/amd64")}}
    _write_inputs(
        stage,
        [_unresolved(LOCALIZATION), _resolved(NAVIGATION), _unresolved(MISSION)],
        work_limit_seconds=adapter.DEADLINE_RESERVE_SECONDS,
    )

    response, result, _diagnostic = stage.run()

    assert response.domain_outcome == "succeeded"
    assert _records(result)[NAVIGATION]["reason"] == "work-limit"
    assert _builds(stage) == {}
