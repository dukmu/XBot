"""Generic editor for configuration resources published by the server."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, OptionList, Select, Static, TextArea

from XBotv2.tui.impl.view.plugin_config import SchemaField, schema_fields

if TYPE_CHECKING:
    from XBotv2.config.api.resources import ConfigurationCatalog, ConfigurationDocument


SETTINGS_CSS = """
SettingsPanel { width: 100%; height: 1fr; }
#settings { width: 100%; height: 100%; padding: 0 1; }
#settings-title { height: 1; text-style: bold; }
#settings-body { height: 1fr; }
#settings-resources { width: 30%; height: 1fr; }
#settings-filter { height: 3; }
#settings-list { height: 1fr; }
#settings-content { width: 1fr; height: 1fr; padding-left: 1; }
#settings-heading { height: auto; }
#settings-scope { height: 3; }
#settings-editor { height: 1fr; }
#settings-editor TextArea { height: 1fr; min-height: 6; }
#settings-fields { height: 1fr; }
.settings-field-label { height: auto; margin-top: 1; }
.settings-field-action { height: 1; min-height: 1; }
#settings-message { height: auto; }
#settings-actions { height: auto; align-horizontal: right; }
#settings-actions Button { min-width: 6; }
#settings-help { height: 1; color: $text-muted; }
"""


class SettingsPanel(Widget):
    """Browse and edit server-described configuration without resource-specific logic."""

    DEFAULT_CSS = SETTINGS_CSS
    BINDINGS = [("escape", "cancel", "Close"), ("ctrl+s", "save", "Save")]

    def __init__(
        self,
        catalog: ConfigurationCatalog,
        *,
        read: Callable[..., Awaitable[ConfigurationDocument]],
        save: Callable[[ConfigurationDocument], Awaitable[ConfigurationDocument]],
        read_only: bool = False,
    ) -> None:
        super().__init__()
        self.on_result: Callable[[None], None] | None = None
        self.catalog = catalog
        self.read = read
        self.save = save
        self.read_only = read_only
        self.resource: Any = None
        self.document: Any = None
        self.content: Any = None
        self._filtered = list(catalog.resources)
        self._raw_mode = False
        self._invalid: set[str] = set()
        self._fields: dict[str, SchemaField] = {}
        self._controls: dict[str, SchemaField] = {}
        self._actions: dict[str, tuple[str, str]] = {}
        self._busy = False
        self._conflict = False

    def compose(self) -> ComposeResult:
        with Vertical(id="settings"):
            yield Static("Settings", id="settings-title")
            with Horizontal(id="settings-body"):
                with Vertical(id="settings-resources"):
                    yield Input(placeholder="Filter resources", id="settings-filter")
                    yield OptionList(id="settings-list", markup=False)
                with Vertical(id="settings-content"):
                    yield Static("Select a resource", id="settings-heading", markup=False)
                    yield Select([], id="settings-scope", allow_blank=True, disabled=True)
                    with VerticalScroll(id="settings-editor"):
                        yield Static("Select a resource to edit.")
                    yield Static("", id="settings-message", markup=False)
                    with Horizontal(id="settings-actions"):
                        yield Button("JSON", id="settings-mode", disabled=True)
                        yield Button("Discard", id="settings-reload", disabled=True)
                        yield Button("Save", id="settings-save", disabled=True)
            yield Static("Enter select · Tab controls · Ctrl+S save · Esc close", id="settings-help")

    async def on_mount(self) -> None:
        self.query_one("#settings-editor", VerticalScroll).can_focus = False
        self._refresh_list()
        self.query_one("#settings-list", OptionList).focus()
        if self._filtered:
            if self._filtered[0].scopes:
                await self._load(self._filtered[0], self._filtered[0].scopes[0])
            else:
                self._message("This resource has no available scope.")

    def _refresh_list(self) -> None:
        query = self.query_one("#settings-filter", Input).value.casefold().strip()
        self._filtered = [item for item in self.catalog.resources if query in f"{item.title} {item.description}".casefold()]
        listing = self.query_one("#settings-list", OptionList)
        listing.clear_options()
        listing.add_options([item.title for item in self._filtered])
        if self._filtered:
            listing.highlighted = 0

    @on(Input.Changed, "#settings-filter")
    def filter_resources(self) -> None:
        self._refresh_list()

    @on(OptionList.OptionSelected, "#settings-list")
    async def select_resource(self, event: OptionList.OptionSelected) -> None:
        if event.option_index >= len(self._filtered):
            return
        resource = self._filtered[event.option_index]
        if self.resource is not None and resource.id == self.resource.id:
            return
        if self._dirty() or self._invalid:
            self._message("Save or close this draft before switching resources.")
            return
        if not resource.scopes:
            self._message("This resource has no available scope.")
            return
        await self._load(resource, resource.scopes[0])

    @on(Select.Changed, "#settings-scope")
    async def select_scope(self, event: Select.Changed) -> None:
        if self._busy or self.document is None or not isinstance(event.value, str) or event.value == self.document.scope:
            return
        if self._dirty() or self._invalid:
            self._message("Save or close this draft before switching scope.")
            event.select.value = self.document.scope
            return
        await self._load(self.resource, event.value)

    async def _load(self, resource, scope: str) -> None:
        self._busy = True
        try:
            document = await self.read(resource.id, scope=scope)
        except Exception as exc:
            self._message(f"Could not load configuration: {exc}")
            if self.document is not None:
                self.query_one("#settings-scope", Select).value = self.document.scope
            self._busy = False
            return
        self.resource = resource
        self.document = document
        self.content = copy.deepcopy(document.content)
        self._invalid.clear()
        self._raw_mode = False
        self._conflict = False
        heading = self.query_one("#settings-heading", Static)
        heading.update(f"{resource.title}\n{resource.description}")
        selector = self.query_one("#settings-scope", Select)
        selector.set_options([(scope.title(), scope) for scope in resource.scopes])
        selector.disabled = len(resource.scopes) < 2
        selector.value = document.scope
        await self._render_editor()
        self._message("")
        self._busy = False
        self._refresh_actions()

    async def _render_editor(self) -> None:
        self._fields.clear()
        self._controls.clear()
        self._actions.clear()
        editor = self.query_one("#settings-editor", VerticalScroll)
        await editor.remove_children()
        if self.document is None:
            return
        fields = schema_fields(self.document.config_schema)
        if self.resource.format == "json" and isinstance(self.content, dict) and fields and not self._raw_mode:
            widgets: list[Widget] = []
            for index, field in enumerate(fields):
                self._fields[field.name] = field
                description = field.schema.get("description")
                widgets.append(Static(f"{field.label}" + (f" — {description}" if isinstance(description, str) and description else ""), classes="settings-field-label", markup=False))
                action_id = f"settings-field-action-{index}"
                present = field.name in self.content
                self._actions[action_id] = (field.name, "remove" if present else "add")
                if present:
                    value = self.content[field.name]
                    control_id = f"settings-field-{index}"
                    self._controls[control_id] = field
                    disabled = self.read_only or field.schema.get("readOnly") is True
                    if field.kind == "boolean" and not field.nullable:
                        widgets.append(Checkbox(field.label, value=value is True, id=control_id, classes="settings-field-checkbox", disabled=disabled))
                    elif field.kind == "enum":
                        options = [("null" if option is None else str(option), str(n)) for n, option in enumerate(field.options)]
                        selected = next((str(n) for n, option in enumerate(field.options) if type(value) is type(option) and value == option), Select.BLANK)
                        widgets.append(Select(options, value=selected, allow_blank=True, id=control_id, classes="settings-field-enum", disabled=disabled))
                    else:
                        text = json.dumps(value, ensure_ascii=False) if field.kind == "json" else "" if value is None else str(value)
                        widgets.append(Input(value=text, id=control_id, classes="settings-field-input", disabled=disabled, password=field.schema.get("format") == "password" or field.schema.get("writeOnly") is True))
                widgets.append(Button("Remove override" if present else "Set value", id=action_id, classes="settings-field-action", disabled=self.read_only or field.schema.get("readOnly") is True, compact=True))
            await editor.mount(*widgets)
        else:
            text = json.dumps(self.content, ensure_ascii=False, indent=2) if self.resource.format == "json" else str(self.content)
            await editor.mount(TextArea(text, id="settings-text", read_only=self.read_only))
        self._refresh_actions()

    @on(Button.Pressed)
    async def button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id or ""
        if name == "settings-save":
            await self._save()
        elif name == "settings-reload" and self.document is not None:
            await self._load(self.resource, self.document.scope)
        elif name == "settings-mode" and self.document is not None:
            if self._invalid:
                self._message("Correct the invalid value before switching editor.")
                return
            self._raw_mode = not self._raw_mode
            await self._render_editor()
        elif name in self._actions:
            field_name, operation = self._actions[name]
            field = self._fields[field_name]
            if operation == "remove":
                self.content.pop(field_name, None)
            else:
                self.content[field_name] = copy.deepcopy(field.schema.get("default", _default_value(field)))
            self._invalid.discard(field_name)
            await self._render_editor()

    @on(Input.Changed, ".settings-field-input")
    def field_input_changed(self, event: Input.Changed) -> None:
        field = self._controls.get(event.input.id or "")
        if field is None or self._busy:
            return
        try:
            value = _parse_value(field, event.value)
        except ValueError as exc:
            self._invalid.add(field.name)
            self._message(str(exc))
        else:
            self._invalid.discard(field.name)
            self.content[field.name] = value
            if not self._invalid:
                self._message("")
        self._refresh_actions()

    @on(Checkbox.Changed, ".settings-field-checkbox")
    def field_checkbox_changed(self, event: Checkbox.Changed) -> None:
        field = self._controls.get(event.checkbox.id or "")
        if field is not None and not self._busy:
            self.content[field.name] = event.value
            self._refresh_actions()

    @on(Select.Changed, ".settings-field-enum")
    def field_enum_changed(self, event: Select.Changed) -> None:
        field = self._controls.get(event.select.id or "")
        if field is not None and not self._busy and isinstance(event.value, str) and event.value.isdigit():
            index = int(event.value)
            if index < len(field.options):
                self.content[field.name] = field.options[index]
                self._refresh_actions()

    @on(TextArea.Changed, "#settings-text")
    def text_changed(self, event: TextArea.Changed) -> None:
        if self._busy or self.document is None:
            return
        text = event.text_area.text
        if self.resource.format == "json":
            try:
                self.content = json.loads(text, parse_constant=_reject_constant)
            except ValueError as exc:
                self._invalid.add("document")
                self._message(f"Invalid JSON: {exc}")
            else:
                self._invalid.discard("document")
                self._message("")
        else:
            self.content = text
        self._refresh_actions()

    def _dirty(self) -> bool:
        return self.document is not None and self.content != self.document.content

    async def _save(self) -> None:
        if self._busy or self.document is None or self.read_only or self._invalid or self._conflict or not self._dirty():
            return
        self._busy = True
        self._refresh_actions()
        try:
            updated = await self.save(self.document.model_copy(update={"content": copy.deepcopy(self.content)}))
        except Exception as exc:
            if getattr(exc, "status_code", None) == 409:
                self._conflict = True
                self._message("Configuration changed on the server. Draft retained; Discard reloads latest and loses this draft.")
            else:
                self._message(f"Save failed: {exc}. Your draft is retained.")
        else:
            self.document = updated
            self.content = copy.deepcopy(updated.content)
            await self._render_editor()
            self._message("Saved. " + self.resource.description)
        self._busy = False
        self._refresh_actions()

    def _message(self, message: str) -> None:
        self.query_one("#settings-message", Static).update(message)

    def _refresh_actions(self) -> None:
        save = self.query_one("#settings-save", Button)
        save.disabled = self.read_only or self._busy or self.document is None or not self._dirty() or bool(self._invalid) or self._conflict
        self.query_one("#settings-reload", Button).disabled = self._busy or self.document is None
        mode = self.query_one("#settings-mode", Button)
        mode.disabled = self._busy or self.document is None or self.resource.format != "json" or not schema_fields(self.document.config_schema) or not isinstance(self.content, dict)
        mode.label = "Form" if self._raw_mode else "JSON"

    def action_cancel(self) -> None:
        if self.on_result is not None:
            self.on_result(None)

    async def action_save(self) -> None:
        await self._save()


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite number: {value}")


def _default_value(field: SchemaField) -> Any:
    if field.nullable:
        return None
    if field.kind == "boolean":
        return False
    if field.kind in {"integer", "number"}:
        return 0
    if field.kind == "enum":
        return field.options[0] if field.options else None
    if field.kind == "string":
        return ""
    return None


def _parse_value(field: SchemaField, raw: str) -> Any:
    if raw == "" and field.nullable:
        return None
    if field.kind == "string":
        return raw
    try:
        if field.kind == "json":
            return json.loads(raw, parse_constant=_reject_constant)
        if field.kind == "boolean":
            value = json.loads(raw)
            if not isinstance(value, bool):
                raise ValueError("expected boolean")
            return value
        value = int(raw) if field.kind == "integer" else float(raw)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("not finite")
        return value
    except ValueError as exc:
        raise ValueError(f"{field.label}: invalid {field.kind} value") from exc
