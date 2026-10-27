"""Plugin-owned defaults for process-level session management."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    workspace_root: Path = Path(".")
    provider_name: str | None = None
    no_plugins: bool = False
    replay_process_bytes: int = Field(default=32 * 1024 * 1024, ge=0)

    @field_validator("workspace_root")
    @classmethod
    def resolve_workspace(cls, value: Path) -> Path:
        return value.resolve()
