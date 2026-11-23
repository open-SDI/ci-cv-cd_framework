"""Checks of the image-build result contract's per-placement record rules."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from sdi_pipeline_integration._adapter_kit import encode_document
from sdi_pipeline_integration._domain_contracts import ImageBuildResult, image_tag
from sdi_pipeline_integration._stage_contracts import (
    contains_sensitive_contract_material,
)
from sdi_pipeline_integration._stage_registry import (
    EXPECTED_STAGE_INPUT_SLOTS,
    STAGES,
)

DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"
DIGEST_C = f"sha256:{'c' * 64}"


def _built(service_id: str = "nav2-navigation") -> dict[str, Any]:
    return {
        "service_id": service_id,
        "status": "built",
        "repository": f"sdi/{service_id}",
        "digest": DIGEST_A,
        "platforms": [
            {"architecture": "amd64", "digest": DIGEST_B},
            {"architecture": "arm64", "digest": DIGEST_C},
        ],
    }


def _failed(service_id: str = "nav2-localization") -> dict[str, Any]:
    return {
        "service_id": service_id,
        "status": "failed",
        "reason": "build-failed",
        "detail": "process did not complete successfully: exit code: 100",
    }


def _result(
    images: list[dict[str, Any]], evidence_basis: str = "implemented"
) -> dict[str, Any]:
    return {
        "schema_version": "sdi.image-build-result/v1",
        "evidence_basis": evidence_basis,
        "image_build_result_id": "s-04-tc-03-c-05-image-build",
        "blueprint_id": "s-04-tc-03-c-05-composition",
        "deployment_schema_id": "s-04-tc-03-c-05-deployment",
        "scenario_id": "S-04",
        "testcase_id": "TC-03",
        "combination_id": "C-05",
        "profile_id": "waffle-jetson-arm64",
        "source_inputs": [
            {"slot": slot, "sha256": "0" * 64}
            for slot in EXPECTED_STAGE_INPUT_SLOTS["image_build"]
        ],
        "registry": "registry:5000",
        "images": images,
    }


def _validate(payload: dict[str, Any]) -> ImageBuildResult:
    return ImageBuildResult.model_validate(payload, strict=True, extra="forbid")


def test_an_implemented_result_records_built_and_failed_placements() -> None:
    result = _validate(_result([_built(), _failed()]))

    assert [item.status for item in result.images] == ["built", "failed"]


def test_a_fixture_result_records_every_placement_as_not_evaluated() -> None:
    images = [{"service_id": "fixture-navigation", "status": "not_evaluated"}]

    result = _validate(_result(images, evidence_basis="fixture"))

    assert result.images[0].status == "not_evaluated"


@pytest.mark.parametrize(
    ("evidence_basis", "images"),
    [
        ("fixture", [_built()]),
        ("fixture", [{"service_id": "a", "status": "not_evaluated"}, _failed()]),
        ("implemented", [{"service_id": "a", "status": "not_evaluated"}]),
        ("implemented", [_built(), {"service_id": "a", "status": "not_evaluated"}]),
    ],
)
def test_not_evaluated_records_belong_only_to_fixture_results(
    evidence_basis: str, images: list[dict[str, Any]]
) -> None:
    with pytest.raises(ValidationError, match="not_evaluated"):
        _validate(_result(images, evidence_basis=evidence_basis))


@pytest.mark.parametrize(
    "record",
    [
        {k: v for k, v in _built().items() if k != "digest"},
        {k: v for k, v in _built().items() if k != "platforms"},
        _built() | {"platforms": list[dict[str, str]]()},
        _built() | {"reason": "build-failed"},
        _built() | {"repository": "sdi/other-service"},
        _built() | {"digest": "sha256:" + "A" * 64},
        _built() | {"platforms": [{"architecture": "riscv64", "digest": DIGEST_B}]},
        _failed() | {"digest": DIGEST_A},
        _failed() | {"reason": "timed-out"},
        {k: v for k, v in _failed().items() if k != "detail"},
        _failed() | {"detail": ""},
        _failed() | {"detail": "x" * 257},
        _failed() | {"detail": 'quoted "value"'},
        _failed() | {"detail": "back\\slash"},
        _failed() | {"detail": "line\nbreak"},
    ],
)
def test_each_status_carries_exactly_its_own_fields(record: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _validate(_result([record]))


def test_a_built_image_names_each_architecture_once() -> None:
    record = _built() | {
        "platforms": [
            {"architecture": "arm64", "digest": DIGEST_B},
            {"architecture": "arm64", "digest": DIGEST_C},
        ]
    }

    with pytest.raises(ValidationError, match="architecture"):
        _validate(_result([record]))


def test_each_placement_has_one_record() -> None:
    with pytest.raises(ValidationError, match="unique"):
        _validate(_result([_built(), _built()]))


def test_the_result_names_only_the_stack_local_registry() -> None:
    with pytest.raises(ValidationError):
        _validate(_result([_built()]) | {"registry": "docker.io"})


def test_a_built_record_is_not_mistaken_for_sensitive_material() -> None:
    result = _validate(_result([_built(), _failed()]))

    assert not contains_sensitive_contract_material(result.model_dump(mode="json"))


def _worst_case(index: int, status: str) -> dict[str, Any]:
    service_id = f"{'s' * 61}{index:02d}"
    if status == "built":
        return _built(service_id)
    return _failed(service_id) | {"detail": "x" * 256}


@pytest.mark.parametrize("status", ["built", "failed"])
def test_forty_eight_worst_case_placements_fit_the_domain_output_limit(
    status: str,
) -> None:
    # Written with the adapter's own compact writer; indented output would not
    # fit this many placements.
    slug = "s" * 63
    payload = _result([_worst_case(index, status) for index in range(48)]) | {
        "image_build_result_id": slug,
        "blueprint_id": slug,
        "deployment_schema_id": slug,
        "profile_id": slug,
    }

    written = encode_document(_validate(payload))

    assert len(written) <= STAGES["image_build"].grant.domain_output_max_bytes


def test_image_tag_is_the_name_the_adapter_pushes_and_events_report() -> None:
    execution_id = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"

    tag = image_tag(
        service_id="nav2-navigation",
        artifact_id="jazzy-debs-arm64",
        execution_id=execution_id,
    )

    assert tag == f"registry:5000/sdi/nav2-navigation:jazzy-debs-arm64-{execution_id}"
