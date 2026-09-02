"""Strict persisted and projected Workspace models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    path: str
    title: str
    session_ids: tuple[str, ...] = ()
    created_at: str
    updated_at: str

class WorkspaceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    items: tuple[WorkspaceRecord, ...] = ()
    archived_session_ids: tuple[str, ...] = ()

class WorkspaceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    session_ids: tuple[str, ...] = ()
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class WorkspaceListing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[WorkspaceView, ...] = ()
    archived_session_ids: tuple[str, ...] = ()


__all__ = ["WorkspaceListing", "WorkspaceRecord", "WorkspaceSnapshot", "WorkspaceView"]
