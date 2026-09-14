"""Enforce immutable Jenkins agent execution boundaries."""

from pathlib import Path

from ._yaml_input import InputError

_LABEL_FILE = Path("/etc/sdi/jenkins-agent-label")
_ROLE_FILE = Path("/etc/sdi/jenkins-agent-role")


def enforce_integration_execution_boundary() -> None:
    """Require Jenkins-side integration work to run on its immutable agent."""
    if not _ROLE_FILE.exists():
        return
    if _ROLE_FILE.read_text().strip() != "integration":
        msg = "Jenkins integration work requires the integration agent"
        raise InputError(msg)


def enforce_domain_execution_boundary(*, expected_label: str | None = None) -> None:
    """Allow Domain execution only on the matching immutable agent identity."""
    if not _ROLE_FILE.exists():
        return
    agent_role = _ROLE_FILE.read_text().strip()
    if agent_role == "integration":
        msg = "the integration Jenkins agent cannot execute a Domain adapter"
        raise InputError(msg)
    if agent_role != "domain":
        msg = "the immutable Jenkins agent role is invalid"
        raise InputError(msg)
    if not _LABEL_FILE.is_file():
        msg = "the Domain Jenkins agent has no immutable label"
        raise InputError(msg)
    configured_label = _LABEL_FILE.read_text().strip()
    if expected_label is not None and configured_label != expected_label:
        msg = "the Domain agent label does not match the adapter descriptor"
        raise InputError(msg)
