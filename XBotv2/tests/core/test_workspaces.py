"""First-class process Workspace registry behavior."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.main import open_runtime
from XBotv2.persistence.api.contracts import PERSISTENCE

from XBotv2.workspaces.api.contracts import WORKSPACES, WorkspaceNotFound
from XBotv2.session.api.contracts import SessionSummary
from XBotv2.workspaces.impl.service import WorkspaceRegistry
from XBotv2.workspaces.api.contracts import WORKSPACE_RESOURCE_CHANGED
from XBotv2.workspaces.impl.protocol import build_router
from XBotv2.workspaces.impl.directories import DirectoryBrowser


@pytest.fixture
async def workspace_state(tmp_path):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(tmp_path / "data"))
    try:
        registry = root.require(WORKSPACES)
        for workspace in (await registry.list()).items:
            await registry.delete(workspace.workspace_id)
        yield root.require(PERSISTENCE).state()
    finally:
        await root.close()


class Sessions:
    def __init__(self, items=()) -> None:
        self.items = tuple(items)

    def session_exists(self, session_id):
        return any(item.session_id == session_id for item in self.items)

    async def catalog_summary(self, session_id):
        item = next(item for item in self.items if item.session_id == session_id)
        return SessionSummary(session_id=item.session_id, workspace_root=item.workspace_root, status="inactive")


class Events:
    def __init__(self) -> None:
        self.items = []

    async def emit(self, event, *args):
        self.items.append((event, args))


@pytest.mark.asyncio
async def test_workspace_registry_persists_and_projects_session_membership(tmp_path, workspace_state):
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    sessions = Sessions([
        SimpleNamespace(session_id="s2", workspace_root=str(first_path)),
        SimpleNamespace(session_id="s1", workspace_root=str(first_path)),
        SimpleNamespace(session_id="other", workspace_root=str(tmp_path / "other")),
    ])
    registry = WorkspaceRegistry(workspace_state, sessions, Events())

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
    await registry.attach_session("s1", first_path)
    await registry.attach_session("s2", first_path)
    assert [s.session_id for s in (await registry.list_sessions(first.workspace_id)).sessions] == ["s2", "s1"]

    moved = await registry.insert_session_before(first.workspace_id, "s1", "s2")
    assert moved.session_count == 2
    assert [s.session_id for s in (await registry.list_sessions(first.workspace_id)).sessions] == ["s1", "s2"]

    restored = WorkspaceRegistry(workspace_state, sessions, Events())
    assert [item.workspace_id for item in (await restored.list()).items] == [
        first.workspace_id,
        second.workspace_id,
    ]
    assert [s.session_id for s in (await restored.list_sessions(first.workspace_id)).sessions] == ["s1", "s2"]

    with pytest.raises(ValueError, match="not in workspace"):
        await restored.insert_session_before(first.workspace_id, "missing", None)


@pytest.mark.asyncio
async def test_workspace_rename_reorder_and_delete_are_strict(tmp_path, workspace_state):
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    registry = WorkspaceRegistry(workspace_state, Sessions(), Events())
    first, _ = await registry.create(first_path)
    second, _ = await registry.create(second_path)

    renamed = await registry.rename(first.workspace_id, "Primary")
    assert renamed.title == "Primary"
    await registry.insert_before(second.workspace_id, first.workspace_id)
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
async def test_workspace_create_requires_an_existing_directory(tmp_path, workspace_state):
    registry = WorkspaceRegistry(workspace_state, Sessions(), Events())

    with pytest.raises(ValueError, match="existing directory"):
        await registry.create(tmp_path / "missing")

    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="existing directory"):
        await registry.create(file_path)


@pytest.mark.asyncio
async def test_workspace_boot_registration_does_not_read_session_history(tmp_path, workspace_state):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()

    class CorruptSessions:
        async def list_sessions(self):
            raise ValueError("corrupt session history")

    events = Events()
    registry = WorkspaceRegistry(
        workspace_state,
        CorruptSessions(),
        events,
    )

    assert await registry.ensure(workspace_path) is True
    assert [event for event, _args in events.items] == [WORKSPACE_RESOURCE_CHANGED]
    assert (await registry.create(workspace_path))[1] is False


@pytest.mark.asyncio
async def test_session_events_update_durable_workspace_membership(tmp_path, workspace_state):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    events = Events()
    sessions = Sessions([SimpleNamespace(
        session_id="session-1",
        workspace_root=str(workspace_path),
    )])
    registry = WorkspaceRegistry(
        workspace_state,
        sessions,
        events,
    )

    await registry.attach_session("session-1", workspace_path)
    await registry.attach_session("session-1", workspace_path)
    attached = (await registry.list()).items[0]
    sessions.items = ()
    await registry.detach_session("session-1")
    detached = (await registry.list()).items[0]

    assert attached.session_count == 1
    assert detached.session_count == 0
    assert [event for event, _args in events.items] == [
        WORKSPACE_RESOURCE_CHANGED,
        WORKSPACE_RESOURCE_CHANGED,
    ]


@pytest.mark.asyncio
async def test_workspace_archive_uses_the_same_snapshot_and_validates_session(tmp_path, workspace_state):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    sessions = Sessions([SimpleNamespace(
        session_id="session-1",
        workspace_root=str(workspace_path),
    )])
    registry = WorkspaceRegistry(workspace_state, sessions, Events())
    workspace, _ = await registry.create(workspace_path)

    await registry.attach_session("session-1", workspace_path)
    await registry.set_archived("session-1", True)
    await registry.rename(workspace.workspace_id, "Renamed")
    assert [s.session_id for s in (await registry.list_archived()).sessions] == ["session-1"]
    assert not (await registry.list_sessions(workspace.workspace_id)).sessions
    await registry.set_archived("session-1", False)
    assert not (await registry.list_archived()).sessions
    with pytest.raises(LookupError, match="missing"):
        await registry.set_archived("missing", True)


@pytest.mark.asyncio
async def test_workspace_http_resources_use_stable_wire_fields(tmp_path, workspace_state):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / "project").mkdir()
    (workspace_path / ".hidden").mkdir()
    (workspace_path / "README.md").write_text("not a directory", encoding="utf-8")
    registry = WorkspaceRegistry(
        workspace_state,
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
        await registry.attach_session("session-1", workspace_path)
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
    assert listed.json()["items"][0]["session_count"] == 1
    assert renamed.json()["workspace"]["title"] == "Renamed"
    assert renamed.json()["workspace"]["session_count"] == 1
    assert archived.json() == {"session_id": "session-1", "archived": True}
    assert reordered.json()["workspace"]["session_count"] == 0
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
        async def list(self, **kwargs):
            workspace_events.sequence = 8
            return SimpleNamespace(items=(), next_cursor=None, previous_cursor=None)

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


@pytest.mark.asyncio
async def test_attach_session_never_requires_the_directory_to_exist(tmp_path, workspace_state):
    """Session-lifecycle projections (attach/detach) must not be able to veto
    a live session when its workspace directory was deleted on disk; only
    explicit user-facing creation validates the directory."""
    missing = tmp_path / "deleted-workspace"
    assert not missing.exists()
    # The session manager knows the live session even though its workspace
    # directory was deleted on disk.
    sessions = Sessions([
        SimpleNamespace(session_id="s-live", workspace_root=str(missing)),
    ])
    registry = WorkspaceRegistry(workspace_state, sessions, Events())

    # Projection attaches even though the directory is gone.
    await registry.attach_session("s-live", missing)
    listing = await registry.list()
    views = [view for view in listing.items if view.path == str(missing.resolve())]
    assert len(views) == 1
    assert views[0].session_count == 1

    # Boot-time identity also tolerates a missing directory (the record
    # already exists here, so ensure reports "not created").
    assert await registry.ensure(missing) is False
    # ...while explicit creation still validates loudly.
    with pytest.raises(ValueError, match="existing directory"):
        await registry.create(missing)
