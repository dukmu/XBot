"""First-class process Workspace registry behavior."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from xcore.state import StateService

from XBotv2.workspaces import WorkspaceNotFound, WorkspaceRegistry
from XBotv2.workspaces.contracts import WORKSPACE_RESOURCE_CHANGED
from XBotv2.workspaces.protocol import build_router
from XBotv2.workspaces.directories import DirectoryBrowser


class Sessions:
    def __init__(self, items=()) -> None:
        self.items = tuple(items)

    async def list_sessions(self):
        return self.items


class Events:
    def __init__(self) -> None:
        self.items = []

    async def emit(self, event, *args):
        self.items.append((event, args))


@pytest.mark.asyncio
async def test_workspace_registry_persists_and_projects_session_membership(tmp_path):
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    sessions = Sessions([
        SimpleNamespace(session_id="s2", workspace_root=str(first_path)),
        SimpleNamespace(session_id="s1", workspace_root=str(first_path)),
        SimpleNamespace(session_id="other", workspace_root=str(tmp_path / "other")),
    ])
    state_file = tmp_path / "state.json"
    registry = WorkspaceRegistry(StateService(path=state_file), sessions, Events())

    first, created = await registry.create(first_path)
    same, duplicate_created = await registry.create(first_path / ".")
    second, _ = await registry.create(second_path)

    assert created is True
    assert duplicate_created is False
    assert same.workspace_id == first.workspace_id
    assert [item.workspace_id for item in (await registry.list()).items] == [
        first.workspace_id,
        second.workspace_id,
    ]
    listed_first = (await registry.list()).items[0]
    assert listed_first.session_ids == ("s2", "s1")

    moved = await registry.insert_session_before(first.workspace_id, "s1", "s2")
    assert moved.session_ids == ("s1", "s2")

    restored = WorkspaceRegistry(StateService(path=state_file), sessions, Events())
    assert [item.workspace_id for item in (await restored.list()).items] == [
        first.workspace_id,
        second.workspace_id,
    ]
    assert (await restored.list()).items[0].session_ids == ("s1", "s2")

    with pytest.raises(ValueError, match="not in Workspace"):
        await restored.insert_session_before(first.workspace_id, "missing", None)


@pytest.mark.asyncio
async def test_workspace_rename_reorder_and_delete_are_strict(tmp_path):
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    registry = WorkspaceRegistry(StateService(path=tmp_path / "state.json"), Sessions(), Events())
    first, _ = await registry.create(first_path)
    second, _ = await registry.create(second_path)

    renamed = await registry.rename(first.workspace_id, "Primary")
    assert renamed.title == "Primary"
    assert await registry.insert_before(
        second.workspace_id,
        first.workspace_id,
    ) == (second.workspace_id, first.workspace_id)
    assert [item.workspace_id for item in (await registry.list()).items] == [
        second.workspace_id,
        first.workspace_id,
    ]
    assert await registry.delete(second.workspace_id) is True
    assert await registry.delete(second.workspace_id) is False

    with pytest.raises(WorkspaceNotFound):
        await registry.rename("ws_missing", "Missing")
    with pytest.raises(ValueError, match="non-empty"):
        await registry.rename(first.workspace_id, "   ")
    with pytest.raises(WorkspaceNotFound):
        await registry.insert_before(first.workspace_id, "ws_missing")


@pytest.mark.asyncio
async def test_workspace_create_requires_an_existing_directory(tmp_path):
    registry = WorkspaceRegistry(StateService(path=tmp_path / "state.json"), Sessions(), Events())

    with pytest.raises(ValueError, match="existing directory"):
        await registry.create(tmp_path / "missing")

    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="existing directory"):
        await registry.create(file_path)


@pytest.mark.asyncio
async def test_workspace_boot_registration_does_not_read_session_history(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()

    class CorruptSessions:
        async def list_sessions(self):
            raise ValueError("corrupt session history")

    events = Events()
    registry = WorkspaceRegistry(
        StateService(path=tmp_path / "state.json"),
        CorruptSessions(),
        events,
    )

    assert await registry.ensure(workspace_path) is True
    assert [event for event, _args in events.items] == [WORKSPACE_RESOURCE_CHANGED]
    with pytest.raises(ValueError, match="corrupt session history"):
        await registry.create(workspace_path)


@pytest.mark.asyncio
async def test_session_events_update_durable_workspace_membership(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    events = Events()
    sessions = Sessions([SimpleNamespace(
        session_id="session-1",
        workspace_root=str(workspace_path),
    )])
    registry = WorkspaceRegistry(
        StateService(path=tmp_path / "state.json"),
        sessions,
        events,
    )

    await registry.attach_session("session-1", workspace_path)
    await registry.attach_session("session-1", workspace_path)
    attached = (await registry.list()).items[0]
    sessions.items = ()
    await registry.detach_session("session-1")
    detached = (await registry.list()).items[0]

    assert attached.session_ids == ("session-1",)
    assert detached.session_ids == ()
    assert [event for event, _args in events.items] == [
        WORKSPACE_RESOURCE_CHANGED,
        WORKSPACE_RESOURCE_CHANGED,
    ]


@pytest.mark.asyncio
async def test_workspace_archive_uses_the_same_snapshot_and_validates_session(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    sessions = Sessions([SimpleNamespace(
        session_id="session-1",
        workspace_root=str(workspace_path),
    )])
    state_file = tmp_path / "state.json"
    registry = WorkspaceRegistry(StateService(path=state_file), sessions, Events())
    workspace, _ = await registry.create(workspace_path)

    assert await registry.set_archived("session-1", True) == ("session-1",)
    await registry.rename(workspace.workspace_id, "Renamed")
    assert (await registry.list()).archived_session_ids == ("session-1",)

    sessions.items = ()
    assert (await registry.list()).archived_session_ids == ()
    restored = WorkspaceRegistry(StateService(path=state_file), sessions, Events())
    assert (await restored.list()).archived_session_ids == ()

    sessions.items = (SimpleNamespace(
        session_id="session-1",
        workspace_root=str(workspace_path),
    ),)
    assert await registry.set_archived("session-1", False) == ()
    with pytest.raises(LookupError, match="missing"):
        await registry.set_archived("missing", True)


@pytest.mark.asyncio
async def test_workspace_http_resources_use_stable_wire_fields(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / "project").mkdir()
    (workspace_path / ".hidden").mkdir()
    (workspace_path / "README.md").write_text("not a directory", encoding="utf-8")
    registry = WorkspaceRegistry(
        StateService(path=tmp_path / "state.json"),
        Sessions([SimpleNamespace(
            session_id="session-1",
            workspace_root=str(workspace_path),
        )]),
        Events(),
    )
    app = FastAPI()
    app.include_router(build_router(
        workspaces=registry,
        workspace_events=SimpleNamespace(sequence=0, subscribe=lambda _after: None),
        directories=DirectoryBrowser(tmp_path),
    ))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post("/workspaces", json={"path": str(workspace_path)})
        workspace = created.json()["workspace"]
        listed = await client.get("/workspaces")
        renamed = await client.patch(
            f"/workspaces/{workspace['workspace_id']}",
            json={"title": "Renamed"},
        )
        archived = await client.put("/sessions/session-1/archive")
        reordered = await client.post(
            f"/workspaces/{workspace['workspace_id']}/sessions/session-1/order",
            json={"before_session_id": None},
        )
        directories = await client.get(
            "/directories",
            params={"path": str(workspace_path)},
        )

    assert created.status_code == 200
    assert created.json()["created"] is True
    assert listed.json()["items"][0]["session_ids"] == ["session-1"]
    assert renamed.json()["workspace"]["title"] == "Renamed"
    assert renamed.json()["workspace"]["session_ids"] == ["session-1"]
    assert archived.json()["archived_session_ids"] == ["session-1"]
    assert reordered.json()["workspace"]["session_ids"] == ["session-1"]
    assert directories.status_code == 200
    assert directories.json()["path"] == str(workspace_path.resolve())
    assert [entry["name"] for entry in directories.json()["entries"]] == [
        ".hidden",
        "project",
    ]
    assert directories.json()["entries"][0]["hidden"] is True


@pytest.mark.asyncio
async def test_workspace_baseline_cursor_precedes_the_snapshot_read(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace_events = SimpleNamespace(sequence=7)

    class RacingWorkspaces:
        async def list(self):
            workspace_events.sequence = 8
            return SimpleNamespace(items=(), archived_session_ids=())

    app = FastAPI()
    app.include_router(build_router(
        workspaces=RacingWorkspaces(),
        workspace_events=workspace_events,
        directories=DirectoryBrowser(tmp_path),
    ))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/workspaces")

    assert response.status_code == 200
    assert response.json()["event_cursor"] == 7
