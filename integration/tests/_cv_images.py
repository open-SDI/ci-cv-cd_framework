"""Find the linux/amd64 digest CV pulls for an image a test pushed."""

from __future__ import annotations

from typing import cast

import requests

INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
MANIFEST_ACCEPT = ", ".join(
    (
        *INDEX_TYPES,
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
AMD64 = {"architecture": "amd64", "os": "linux"}


def amd64_digest(registry_url: str, repository: str, tag: str) -> str:
    """Return the per-platform amd64 manifest digest of a pushed tag."""
    response = requests.get(
        f"{registry_url}/v2/{repository}/manifests/{tag}",
        headers={"Accept": MANIFEST_ACCEPT},
        timeout=30,
    )
    response.raise_for_status()
    document = cast("dict[str, object]", response.json())
    if response.headers["Content-Type"] not in INDEX_TYPES:
        return response.headers["Docker-Content-Digest"]
    manifests = cast("list[dict[str, object]]", document["manifests"])
    return next(
        str(item["digest"]) for item in manifests if item.get("platform") == AMD64
    )
