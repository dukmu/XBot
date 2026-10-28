"""The settings client edits resources without knowing their owners."""
from __future__ import annotations

import copy

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Checkbox, Input, OptionList, Select, Static, TextArea

from XBotv2.config.api.resources import ConfigurationCatalog, ConfigurationDocument, ConfigurationResource
from XBotv2.tui.impl.view.settings import SettingsPanel


class Conflict(Exception):
    status_code = 409


class Backend:
    def __init__(self):
        self.catalog = ConfigurationCatalog(resources=(
            ConfigurationResource(id="new:one", title="First resource", scopes=("global", "workspace"), format="json", description="Applies on reload"),
            ConfigurationResource(id="new:two", title="Second resource", scopes=("session",), format="markdown", description="Applies immediately"),
        ))
        self.documents = {
            ("new:one", "global"): ConfigurationDocument(id="new:one", scope="global", revision="r1", content={"active": True, "count": 2, "extra": {"keep": True}}, config_schema={"type": "object", "properties": {"active": {"type": "boolean"}, "count": {"type": "integer"}, "items": {"type": "array", "items": {"type": "string"}}}}),
            ("new:one", "workspace"): ConfigurationDocument(id="new:one", scope="workspace", revision="r2", content={"count": 3}, config_schema={"type": "object", "properties": {"count": {"type": "integer"}}}),
            ("new:two", "session"): ConfigurationDocument(id="new:two", scope="session", revision="r3", content="original"),
        }
        self.calls = []
        self.conflict = False
        self.error = False

    async def read(self, resource_id, *, scope):
        self.calls.append(("read", resource_id, scope))
        return self.documents[(resource_id, scope)].model_copy(deep=True)

    async def save(self, document):
        self.calls.append(("save", document.id, document.scope, copy.deepcopy(document.content), document.revision))
        if self.conflict:
            raise Conflict("stale")
        if self.error:
            raise ValueError("invalid content")
        updated = document.model_copy(update={"revision": "new"}, deep=True)
        self.documents[(document.id, document.scope)] = updated
        return updated


class Harness(App):
    def __init__(self, backend, *, read_only=False):
        super().__init__()
        self.panel = SettingsPanel(backend.catalog, read=backend.read, save=backend.save, read_only=read_only)
        self.result = []
        self.panel.on_result = self.result.append

    def compose(self) -> ComposeResult:
        yield Static("Conversation")
        yield self.panel


async def test_generic_form_saves_changed_values_and_preserves_unknown_keys():
    backend = Backend()
    app = Harness(backend)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        assert backend.calls == [("read", "new:one", "global")]
        assert "Applies on reload" in str(app.query_one("#settings-heading", Static).content)
        app.query_one("#settings-field-0", Checkbox).value = False
        app.query_one("#settings-field-1", Input).value = "7"
        await pilot.pause()
        app.query_one("#settings-save", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert backend.calls[-1] == ("save", "new:one", "global", {"active": False, "count": 7, "extra": {"keep": True}}, "r1")


async def test_resources_filter_scope_and_source_document():
    backend = Backend()
    app = Harness(backend)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        app.query_one("#settings-scope", Select).value = "workspace"
        await pilot.pause()
        assert backend.calls[-1] == ("read", "new:one", "workspace")
        app.query_one("#settings-filter", Input).value = "second"
        await pilot.pause()
        listing = app.query_one("#settings-list", OptionList)
        listing.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert backend.calls[-1] == ("read", "new:two", "session")
        app.query_one("#settings-text", TextArea).load_text("updated")
        await pilot.pause()
        app.query_one("#settings-save", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert backend.calls[-1] == ("save", "new:two", "session", "updated", "r3")


async def test_conflict_retains_draft_until_explicit_discard():
    backend = Backend()
    app = Harness(backend)
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.pause()
        app.query_one("#settings-field-1", Input).value = "8"
        await pilot.pause()
        backend.conflict = True
        app.query_one("#settings-save", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#settings-field-1", Input).value == "8"
        assert app.query_one("#settings-save", Button).disabled
        assert "Draft retained" in str(app.query_one("#settings-message", Static).content)
        backend.documents[("new:one", "global")].content["count"] = 9
        backend.documents[("new:one", "global")].revision = "r4"
        app.query_one("#settings-reload", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#settings-field-1", Input).value == "9"
        assert app.panel.document.revision == "r4"


@pytest.mark.parametrize("read_only", [False, True])
async def test_narrow_view_supports_keyboard_and_read_only(read_only):
    backend = Backend()
    app = Harness(backend, read_only=read_only)
    async with app.run_test(size=(40, 14)) as pilot:
        await pilot.pause()
        assert app.query_one("#settings-field-1", Input).disabled == read_only
        assert app.query_one("#settings-save", Button).disabled
        await pilot.press("escape")
        assert app.result == [None]


async def test_json_mode_and_invalid_draft_are_preserved_on_save_error():
    backend = Backend()
    app = Harness(backend)
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.pause()
        app.query_one("#settings-mode", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        editor = app.query_one("#settings-text", TextArea)
        editor.load_text('{"active": false, "count": 5, "extra": [1, 2]}')
        await pilot.pause()
        backend.error = True
        app.query_one("#settings-save", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert editor.text == '{"active": false, "count": 5, "extra": [1, 2]}'
        assert "invalid content" in str(app.query_one("#settings-message", Static).content)
        editor.load_text('{"broken":')
        await pilot.pause()
        assert app.query_one("#settings-save", Button).disabled
        app.query_one("#settings-mode", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#settings-text", TextArea).text == '{"broken":'


async def test_dirty_draft_blocks_scope_change():
    backend = Backend()
    app = Harness(backend)
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.pause()
        app.query_one("#settings-field-1", Input).value = "10"
        await pilot.pause()
        app.query_one("#settings-scope", Select).value = "workspace"
        await pilot.pause()
        assert backend.calls == [("read", "new:one", "global")]
        assert app.query_one("#settings-field-1", Input).value == "10"
        assert app.query_one("#settings-scope", Select).value == "global"


async def test_narrow_view_can_edit_and_save_by_keyboard():
    backend = Backend()
    app = Harness(backend)
    async with app.run_test(size=(40, 14)) as pilot:
        await pilot.pause()
        editor = app.query_one("#settings-editor")
        assert editor.region.height > 0
        field = app.query_one("#settings-field-1", Input)
        field.focus()
        await pilot.press("home", "delete", "8")
        await pilot.pause()
        app.query_one("#settings-save", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert backend.calls[-1][0] == "save"
        assert backend.calls[-1][3]["count"] == 8
