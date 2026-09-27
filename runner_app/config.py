"""Local owner policy. Remote request fields must never populate this model."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DRIVE_ROOT_ID = "177IEHeBVezHIlbIijZsIvRDSbTJ42kxe"
DRIVE_INDEX_ID = "1x2KIBZCZfHs2w-s_Ah3yMW80elgG4KseTe7OZq7L_hA"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    listen_host: Literal["127.0.0.1"] = "127.0.0.1"
    listen_port: int = Field(default=8765, ge=1, le=65535)
    armed: Literal[False] = False
    drive_mode: Literal["attach_existing"] = "attach_existing"
    drive_root_id: str = DRIVE_ROOT_ID
    drive_index_id: str = DRIVE_INDEX_ID
    max_parallel_executions: int = Field(default=2, ge=1, le=16)


def setup_requirements() -> dict[str, object]:
    """Non-secret, owner-visible gate; these are requirements, not successful probes."""
    return {
        "state": "NOT_CONFIGURED",
        "armed": False,
        "ready_commands": 0,
        "google": {
            "status": "NOT_CONFIGURED",
            "workspace_status": "EXISTS_OWNER_REPORTED",
            "mode": "attach_existing",
            "root_folder_id": DRIVE_ROOT_ID,
            "installation_index_document_id": DRIVE_INDEX_ID,
            "required": [
                "Local OAuth desktop application; credentials entered locally, never in chat",
                "Owner-authorized full Drive scope and access to the existing Installation Index and registered Docs",
                "Read and validate INSTALLATION INDEX against the selected root before any writes",
                "Explicit approval of the disposable live smoke-test writes",
            ],
            "bootstrap_allowed": False,
            "probe_performed": False,
        },
        "github": {
            "status": "NOT_CONFIGURED",
            "required": [
                "Explicit owner/organization and disposable private repository namespace",
                "Local credential reference and actual identity/capability probe",
                "Draft PR entitlement and permitted branches/publication policy",
                "Explicit approval of the disposable live smoke-test writes",
            ],
            "probe_performed": False,
        },
        "local_policy": {
            "permitted_import_roots": [],
            "approved_environment_templates": [],
            "required": [
                "Owner-approved import roots; imports use separate managed copies",
                "Rootless runtime, digest-pinned images, approved profiles and lockfile build phase",
                "Credential storage: OS keyring or owner-only 0600 files under a 0700 directory",
            ],
        },
    }
