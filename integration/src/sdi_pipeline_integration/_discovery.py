"""Bounded lexical-plus-dense discovery of the descriptions supplied to generation.

Each requirement's original section and the profile's terms query the service
repository twice: SQLite FTS5 BM25 over every description's text, and cosine
similarity over embeddings from the configured endpoint when one is set. A
declared endpoint must answer unless the config allows lexical-only degradation.
Reciprocal rank fusion merges the two rankings. Each query admits its top
services in turn, and each admitted service pulls in the services its
depends_on names and every service that provides one of its inputs, because
descriptions declare most collaborators through ROS interfaces rather than
depends_on. Expansion is depth-first, so a service arrives with its full
providers before the next query's hit. Admission stops at the description byte
budget, and nothing fills the rest:
the model sees only what discovery selected, and the trace says what it left out.

SQLite FTS5 ships with CPython and so adds no dependency. A vector store such as
ChromaDB was not adopted: it brings a server-grade dependency tree, its default
embedder downloads a model at first use, it offers no BM25, and a repository of
tens of descriptions needs no index.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from pydantic import Field

from ._assessment import ROLE_PAIRS
from ._contracts import ContractModel, RoleMaps

if TYPE_CHECKING:
    from ._assessment import AssessmentInputs
    from ._service_descriptions import DescriptionFile

TOP_K = 3
TRACE_LENGTH = 10
RRF_K = 60
EMBEDDINGS_TIMEOUT_SECONDS = 60
TOKENIZER = "porter unicode61"
PROFILE_QUERY = "profile"
HEADING = re.compile(r"^#{1,6} ", re.MULTILINE)
TOKEN = re.compile(r"\w+")
SHALL = re.compile(r"\*\*Shall:\*\*(.+?)(?:\n\s*\n|\Z)", re.DOTALL)


class EmbeddingsConfig(ContractModel):
    """The endpoint that embeds descriptions and queries for dense discovery."""

    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    # An unset key or a failed call fails the run unless this allows lexical only.
    on_unavailable: Literal["fail", "lexical"] = "fail"


def service_block(file: DescriptionFile) -> str:
    """Return one description exactly as generation supplies it."""
    front_matter = json.dumps(
        file.description.model_dump(mode="json", exclude_none=True), indent=2
    )
    return (
        f'<service id="{file.description.service_id}" path="{file.path}">\n'
        f"{front_matter}\n\n{file.body.strip()}\n</service>"
    )


def _search_text(file: DescriptionFile) -> str:
    """Return what a description says it does, without its artifact routes."""
    description = file.description
    terms = [description.service_id.replace("-", " "), description.title]
    terms.extend(description.devices or [])
    for pair in ROLE_PAIRS:
        for role in pair:
            terms.extend(f"{role} {name}" for name in _names(description, role))
    return "\n".join([*terms, file.body.strip()])


def _requirement_text(requirement_id: str, body: str) -> str:
    """Return the requirement's ID words and its original obligation statement.

    The statement is the section's Shall paragraph, or the whole section when it
    has none; rationale and verification wording would only add noise.
    """
    heading = re.search(
        rf"^#{{1,6}} {re.escape(requirement_id)}\s*$", body, re.MULTILINE
    )
    words = requirement_id.replace("-", " ")
    if heading is None:
        return words
    following = HEADING.search(body, heading.end())
    section = body[heading.end() : following.start() if following else len(body)]
    shall = SHALL.search(section)
    statement = section if shall is None else shall.group(1)
    return f"{words}\n{' '.join(statement.split())}"


def _profile_text(inputs: AssessmentInputs) -> str:
    profile = inputs.profile
    terms: list[str] = []
    for host in profile.hosts.values():
        terms.append(host.architecture)
        terms.extend(host.devices or [])
        if host.baseline is not None:
            terms.extend(
                str(value)
                for value in host.baseline.model_dump(exclude_none=True).values()
            )
    roles = profile.orchestrator_provides or RoleMaps()
    for pair in ROLE_PAIRS:
        for role in pair:
            terms.extend(_names(roles, role))
    return " ".join(dict.fromkeys(terms))


def _lexical(documents: dict[str, str], query: str) -> list[str]:
    """Rank the documents matching any query token by FTS5 BM25."""
    tokens = list(dict.fromkeys(TOKEN.findall(query.lower())))
    if not tokens:
        return []
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            f"CREATE VIRTUAL TABLE docs USING fts5(id UNINDEXED, text, "
            f"tokenize='{TOKENIZER}')"
        )
        connection.executemany("INSERT INTO docs VALUES (?, ?)", documents.items())
        rows = connection.execute(
            "SELECT id FROM docs WHERE docs MATCH ? ORDER BY rank, rowid",
            (" OR ".join(f'"{token}"' for token in tokens),),
        ).fetchall()
    return [row[0] for row in rows]


def _cosine(first: list[float], second: list[float]) -> float:
    dot = sum(a * b for a, b in zip(first, second, strict=True))
    norm = math.sqrt(sum(a * a for a in first)) * math.sqrt(sum(b * b for b in second))
    return dot / norm if norm else 0.0


def _embed(
    texts: list[str], config: EmbeddingsConfig | None
) -> tuple[list[list[float]] | None, dict[str, object]]:
    """Embed every text in one request, or say why dense ranking is absent."""
    if config is None:
        return None, {"status": "not-configured"}
    record: dict[str, object] = {
        "model": config.model,
        "base_url": config.base_url,
        "timeout_seconds": EMBEDDINGS_TIMEOUT_SECONDS,
    }
    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        return None, record | {"status": "key-unset"}
    import openai  # noqa: PLC0415

    try:
        client = openai.OpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=EMBEDDINGS_TIMEOUT_SECONDS,
            max_retries=0,
        )
        response = client.embeddings.create(model=config.model, input=texts)
    except openai.OpenAIError as error:
        return None, record | {
            "status": "failed",
            "error_type": type(error).__name__,
            "status_code": getattr(error, "status_code", None),
        }
    vectors = [item.embedding for item in sorted(response.data, key=lambda d: d.index)]
    if len(vectors) != len(texts):
        return None, record | {"status": "failed", "error_type": "count-mismatch"}
    return vectors, record | {"status": "used", "dimensions": len(vectors[0])}


def _fuse(rankings: list[list[str]]) -> list[str]:
    """Merge rankings by reciprocal rank fusion; ties go to the lower service_id."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, service_id in enumerate(ranking, start=1):
            scores[service_id] = scores.get(service_id, 0.0) + 1 / (RRF_K + rank)
    return sorted(scores, key=lambda service_id: (-scores[service_id], service_id))


def _names(roles: RoleMaps, role: str) -> list[str]:
    return list(cast("dict[str, object] | None", getattr(roles, role)) or {})


@dataclass
class Discovery:
    """The descriptions discovery selected and the trace of how it chose them."""

    selected: list[DescriptionFile]
    omitted: list[str]
    record: dict[str, object] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Say whether every repository description was supplied."""
        return not self.omitted

    @property
    def over_budget(self) -> int:
        """Count the descriptions retrieved but left out for the budget."""
        omitted = cast("list[dict[str, str]]", self.record["omitted"])
        return sum(1 for item in omitted if item["reason"] == "budget")

    @property
    def dense_status(self) -> str:
        """Say whether dense search was used, and why not when it was not."""
        return str(cast("dict[str, object]", self.record["dense"])["status"])

    def stop_reason(
        self, embeddings: EmbeddingsConfig | None
    ) -> tuple[str | None, bool]:
        """Return why the run stops before generation, and if it is lexical only.

        Declared embeddings that were unusable stop the run unless the config
        allows lexical only; an empty selection always stops it.
        """
        lexical_only = False
        if embeddings is not None and self.dense_status in {"key-unset", "failed"}:
            if embeddings.on_unavailable == "fail":
                return "dense-discovery-unavailable", False
            lexical_only = True
        if self.selected:
            return None, lexical_only
        if self.over_budget:
            return "discovery-over-budget", lexical_only
        return "discovery-no-result", lexical_only


class _Selection:
    """Admission of services within the byte budget, with their expansion."""

    def __init__(self, inputs: AssessmentInputs, max_bytes: int) -> None:
        self.files = {file.description.service_id: file for file in inputs.files}
        self.max_bytes = max_bytes
        self.used = 0
        self.selected: dict[str, dict[str, object]] = {}
        self.over_budget: set[str] = set()
        self.unexpanded: list[dict[str, object]] = []
        # Inputs the orchestrator serves need no service to provide them.
        orchestrator = inputs.profile.orchestrator_provides or RoleMaps()
        self.orchestrator_provides = {
            f"{consumer_role} {name}"
            for consumer_role, provider_role in ROLE_PAIRS
            for name in _names(orchestrator, provider_role)
        }

    def providers(self, roles: RoleMaps) -> list[tuple[str, list[str]]]:
        """Return each input reference and the services that provide it."""
        return [
            (
                f"{consumer_role} {name}",
                [
                    service_id
                    for service_id, file in self.files.items()
                    if name in _names(file.description, provider_role)
                ],
            )
            for consumer_role, provider_role in ROLE_PAIRS
            for name in _names(roles, consumer_role)
        ]

    def expand(self, label: str, roles: RoleMaps, depends_on: list[str]) -> None:
        references = [
            (f"depends_on {dependency}", [dependency]) for dependency in depends_on
        ]
        references.extend(
            (reference, [item for item in providers if item != label])
            for reference, providers in self.providers(roles)
            if reference not in self.orchestrator_provides
        )
        for reference, providers in references:
            for provider in providers:
                self.admit(provider, f"provides {reference} of {label}")
            missing = [item for item in providers if item not in self.selected]
            if providers and len(missing) == len(providers):
                self.unexpanded.append(
                    {"service_id": label, "reference": reference, "providers": missing}
                )

    def admit(self, service_id: str, reason: str) -> None:
        if service_id in self.selected or service_id in self.over_budget:
            return
        file = self.files[service_id]
        size = len(service_block(file).encode())
        if self.used + size > self.max_bytes:
            self.over_budget.add(service_id)
            return
        self.used += size
        self.selected[service_id] = {
            "service_id": service_id,
            "path": file.path,
            "reason": reason,
            "bytes": size,
        }
        self.expand(service_id, file.description, file.description.depends_on or [])


def discover(
    inputs: AssessmentInputs,
    *,
    embeddings: EmbeddingsConfig | None,
    max_context_bytes: int,
) -> Discovery:
    """Select the descriptions generation sees and record how they were found."""
    service_ids = [file.description.service_id for file in inputs.files]
    documents = {
        file.description.service_id: _search_text(file) for file in inputs.files
    }
    queries = {
        requirement_id: _requirement_text(requirement_id, inputs.requirements_body)
        for requirement_id in inputs.requirements.requirement_ids
    } | {PROFILE_QUERY: _profile_text(inputs)}
    vectors, dense = _embed(
        list(documents.values()) + list(queries.values()), embeddings
    )
    query_records: list[dict[str, object]] = []
    fused_by_query: list[list[str]] = []
    for index, (query_id, text) in enumerate(queries.items()):
        lexical = _lexical(documents, text)
        rankings = [lexical]
        dense_ranking: list[str] = []
        if vectors is not None:
            query_vector = vectors[len(documents) + index]
            similarity = {
                service_id: _cosine(vectors[position], query_vector)
                for position, service_id in enumerate(service_ids)
            }
            dense_ranking = sorted(
                service_ids, key=lambda item: (-similarity[item], item)
            )
            rankings.append(dense_ranking)
        fused = _fuse(rankings)
        fused_by_query.append(fused)
        query_records.append(
            {
                "query_id": query_id,
                "text": text,
                "ranked": [
                    {
                        "service_id": service_id,
                        "fused": rank,
                        "lexical": lexical.index(service_id) + 1
                        if service_id in lexical
                        else None,
                        "dense": dense_ranking.index(service_id) + 1
                        if service_id in dense_ranking
                        else None,
                    }
                    for rank, service_id in enumerate(fused[:TRACE_LENGTH], start=1)
                ],
            }
        )

    selection = _Selection(inputs, max_context_bytes)
    orchestrator = inputs.profile.orchestrator_provides or RoleMaps()
    selection.expand("orchestrator", orchestrator, [])
    for rank in range(TOP_K):
        for query_id, fused in zip(queries, fused_by_query, strict=True):
            if rank < len(fused):
                selection.admit(fused[rank], f"{query_id} rank {rank + 1}")

    selected = [
        file
        for file in inputs.files
        if file.description.service_id in selection.selected
    ]
    omitted = [item for item in service_ids if item not in selection.selected]
    record: dict[str, object] = {
        "queries": query_records,
        "selected": list(selection.selected.values()),
        "omitted": [
            {
                "service_id": service_id,
                "reason": "budget"
                if service_id in selection.over_budget
                else "not retrieved",
            }
            for service_id in omitted
        ],
        "unexpanded_references": selection.unexpanded,
        "budget": {"max_context_bytes": max_context_bytes, "used": selection.used},
        "top_k": TOP_K,
        "rrf_k": RRF_K,
        "lexical": {
            "engine": f"sqlite {sqlite3.sqlite_version} fts5 bm25",
            "tokenizer": TOKENIZER,
        },
        "dense": dense,
        "complete": not omitted,
    }
    return Discovery(selected, omitted, record)
