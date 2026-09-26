"""The Textual settings overlay, limited to published client capabilities."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, OptionList, Select, Static

from XBotv2.config.contracts import PatchPluginConfig
from XBotv2.tui.view.plugin_config import (
    SchemaField,
    SupportedSchema,
    UnsupportedSchema,
    apply_changes,
    plugin_schema,
)
from XBotv2.tui.view.selection import Option as Choice

if TYPE_CHECKING:
    from XBotv2.config import PluginConfigCatalog, PluginConfigScope, SessionPolicyResponse
    from XBotv2.permissions.contracts import PermissionRule
    from XBotv2.sandbox.contracts import SandboxConfig

SettingName = Literal["provider", "model", "effort", "agent", "thinking", "details"]

PAGES = ("Status", "Model", "Permissions", "Sandbox", "Plugins", "Appearance")

SETTINGS_CSS = """
SettingsScreen {
    align: center middle;
    background: $background 50%;
}
#settings {
    width: 96%;
    max-width: 110;
    height: 90%;
    border: round $accent;
    background: $panel;
    padding: 0 1;
}
#settings-title {
    height: 1;
    text-style: bold;
}
#settings-body {
    height: 1fr;
}
#settings-nav {
    width: 20;
    height: 1fr;
}
#settings-nav > .option-list--option-highlighted {
    color: $text;
    background: $primary-darken-2;
    text-style: bold;
}
#settings-content {
    width: 1fr;
    height: 1fr;
    padding: 0 1;
}
#settings-page-title {
    height: 1;
    text-style: bold;
}
#settings-page-content {
    height: 1fr;
}
#settings-page-actions {
    height: auto;
    width: 1fr;
    align-horizontal: right;
}
#settings-page-actions.hidden {
    display: none;
}
.settings-copy {
    height: auto;
    margin-bottom: 1;
}
Button.settings-action {
    width: 1fr;
    height: 1;
    min-height: 1;
    margin-bottom: 1;
    padding: 0 1;
    background: $panel;
    color: $text;
    text-align: left;
    content-align: left middle;
}
.settings-action:focus {
    background: $primary-darken-2;
    text-style: bold;
}
.settings-action:hover {
    background: $primary-darken-3;
}
#settings-help {
    height: 1;
    color: $text-muted;
}
"""


@dataclass(frozen=True, slots=True)
class SettingsData:
    """Presentation facts prepared by the client owner from public models."""

    status: str
    provider: str
    model: str
    effort: str
    agent: str
    providers: tuple[Choice, ...]
    models: tuple[Choice, ...]
    efforts: tuple[Choice, ...]
    agents: tuple[Choice, ...]
    provider_error: str = ""
    agent_error: str = ""
    policy_error: str = ""
    session_policy: SessionPolicyResponse | None = None
    plugin_config_error: str = ""
    plugin_config: PluginConfigCatalog | None = None
    plugin_draft: PluginConfigDraft | None = None
    plugin_status: str = ""
    reasoning_visible: bool = True
    tool_details_visible: bool = False


@dataclass(frozen=True, slots=True)
class PluginConfigDraft:
    """Unsaved changes made to one producer-owned scope overlay."""

    plugin_id: str
    values: dict[str, JsonValue]
    removals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SettingsAction:
    """A user choice for the app to execute through its existing owner."""

    name: SettingName
    choices: tuple[Choice, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginConfigSaveAction:
    """A typed user request for the app to save a plugin config patch."""

    plugin_id: str
    patch: PatchPluginConfig
    draft: PluginConfigDraft


class SettingsScreen(ModalScreen[SettingsAction | PluginConfigSaveAction | None]):
    """Keyboard-first settings navigation with explicit unavailable pages."""

    DEFAULT_CSS = SETTINGS_CSS
    BINDINGS = [("escape", "cancel", "Close")]

    def __init__(
        self,
        data: SettingsData,
        *,
        initial_page: str = "Status",
        read_only: bool = False,
    ) -> None:
        super().__init__()
        self.data = data
        self.read_only = read_only
        self.page_index = PAGES.index(initial_page) if initial_page in PAGES else 0
        catalog = data.plugin_config
        self._selected_plugin_id = (
            data.plugin_draft.plugin_id
            if data.plugin_draft is not None
            else catalog.plugins[0].plugin_id
            if catalog is not None and catalog.plugins
            else ""
        )
        self._plugin_changes: dict[str, dict[str, JsonValue]] = {}
        self._plugin_removals: dict[str, set[str]] = {}
        if data.plugin_draft is not None:
            self._plugin_changes[data.plugin_draft.plugin_id] = dict(
                data.plugin_draft.values
            )
            self._plugin_removals[data.plugin_draft.plugin_id] = set(
                data.plugin_draft.removals
            )
        self._plugin_field_bindings: dict[str, tuple[str, SchemaField]] = {}
        self._plugin_clear_bindings: dict[str, tuple[str, str]] = {}
        self._plugin_enum_values: dict[str, tuple[JsonValue, ...]] = {}
        self._invalid_plugin_fields: set[str] = set()
        self._plugin_message = data.plugin_status

    def compose(self) -> ComposeResult:
        with Vertical(id="settings"):
            yield Static(
                "Status" if self.read_only else "Settings",
                id="settings-title",
            )
            if self.read_only:
                with VerticalScroll(id="settings-page-content"):
                    yield Static("")
            else:
                with Horizontal(id="settings-body"):
                    yield OptionList(
                        *PAGES,
                        id="settings-nav",
                        markup=False,
                        classes="-textual-compact",
                    )
                    with Vertical(id="settings-content"):
                        yield Static(PAGES[self.page_index], id="settings-page-title")
                        with VerticalScroll(id="settings-page-content"):
                            yield Static("")
                        yield Horizontal(id="settings-page-actions", classes="hidden")
            yield Static(
                (
                    "Esc close"
                    if self.read_only
                    else "↑↓ choose a section · Tab to controls · Enter select · Esc close"
                ),
                id="settings-help",
            )

    async def on_mount(self) -> None:
        if self.read_only:
            self.query_one("#settings-page-content", VerticalScroll).focus()
        else:
            nav = self.query_one("#settings-nav", OptionList)
            nav.highlighted = self.page_index
            nav.focus()
        await self._show_page()

    @on(OptionList.OptionSelected, "#settings-nav")
    async def on_page_selected(self, event: OptionList.OptionSelected) -> None:
        self.page_index = event.option_index
        await self._show_page()

    @on(Button.Pressed)
    async def on_setting_action(self, event: Button.Pressed) -> None:
        name = event.button.id
        if name == "plugin-config-apply":
            self.dismiss(self._plugin_save_action())
            return
        if name in self._plugin_clear_bindings:
            plugin_id, field_name = self._plugin_clear_bindings[name]
            self._plugin_changes.setdefault(plugin_id, {}).pop(field_name, None)
            removals = self._plugin_removals.setdefault(plugin_id, set())
            if field_name in removals:
                removals.remove(field_name)
            else:
                removals.add(field_name)
            self._invalid_plugin_fields.clear()
            await self._show_page()
            return
        choices: tuple[Choice, ...] = ()
        if name == "setting-provider":
            name, choices = "provider", self.data.providers
        elif name == "setting-model":
            name, choices = "model", self.data.models
        elif name == "setting-effort":
            name, choices = "effort", self.data.efforts
        elif name == "setting-agent":
            name, choices = "agent", self.data.agents
        elif name == "setting-thinking":
            name = "thinking"
        elif name == "setting-details":
            name = "details"
        else:
            return
        self.dismiss(SettingsAction(name=name, choices=choices))

    @on(Select.Changed, "#plugin-config-selection")
    async def on_plugin_selected(self, event: Select.Changed) -> None:
        plugin_id = event.value
        if not isinstance(plugin_id, str) or plugin_id == self._selected_plugin_id:
            return
        if self._has_plugin_changes():
            self._plugin_message = "Apply or clear this draft before switching plugins."
            await self._show_page()
            return
        self._selected_plugin_id = plugin_id
        self._plugin_message = ""
        await self._show_page()

    @on(Input.Changed, ".plugin-config-input")
    def on_plugin_input_changed(self, event: Input.Changed) -> None:
        field_binding = self._plugin_field_bindings.get(event.input.id or "")
        if field_binding is None:
            return
        plugin_id, field = field_binding
        try:
            value = _parse_input_value(field, event.value)
        except ValueError as exc:
            self._invalid_plugin_fields.add(field.name)
            self._show_plugin_message(str(exc))
        else:
            self._invalid_plugin_fields.discard(field.name)
            self._set_plugin_value(plugin_id, field, value)
            self._show_plugin_message(self._plugin_message)
        self._refresh_plugin_apply()

    @on(Checkbox.Changed, ".plugin-config-checkbox")
    def on_plugin_checkbox_changed(self, event: Checkbox.Changed) -> None:
        field_binding = self._plugin_field_bindings.get(event.checkbox.id or "")
        if field_binding is None:
            return
        plugin_id, field = field_binding
        self._set_plugin_value(plugin_id, field, event.value)
        self._refresh_plugin_apply()

    @on(Select.Changed, ".plugin-config-enum")
    def on_plugin_enum_changed(self, event: Select.Changed) -> None:
        field_binding = self._plugin_field_bindings.get(event.select.id or "")
        options = self._plugin_enum_values.get(event.select.id or "")
        if field_binding is None or options is None:
            return
        plugin_id, field = field_binding
        if not isinstance(event.value, str) or not event.value.isdigit():
            return
        index = int(event.value)
        if index < len(options):
            self._set_plugin_value(plugin_id, field, options[index])
            self._refresh_plugin_apply()

    def action_cancel(self) -> None:
        self.dismiss(None)

    async def _show_page(self) -> None:
        title = PAGES[self.page_index]
        if not self.read_only:
            self.query_one("#settings-page-title", Static).update(title)
        self._plugin_field_bindings.clear()
        self._plugin_clear_bindings.clear()
        self._plugin_enum_values.clear()
        self._invalid_plugin_fields.clear()
        content = self.query_one("#settings-page-content", VerticalScroll)
        await content.remove_children()
        await content.mount(*self._page_widgets(title))
        if self.read_only:
            return
        actions = self.query_one("#settings-page-actions", Horizontal)
        await actions.remove_children()
        catalog = self.data.plugin_config
        selected = (
            next(
                (item for item in catalog.plugins if item.plugin_id == self._selected_plugin_id),
                None,
            )
            if catalog is not None
            else None
        )
        form = plugin_schema(selected) if selected is not None else None
        if isinstance(form, SupportedSchema) and form.fields:
            await actions.mount(
                Button(
                    "Apply changes",
                    id="plugin-config-apply",
                    disabled=not self._has_plugin_changes(),
                )
            )
            actions.remove_class("hidden")
        else:
            actions.add_class("hidden")

    def _page_widgets(self, page: str) -> tuple[Widget, ...]:
        if page == "Status":
            return (
                Static(
                    self.data.status,
                    id="settings-status-report",
                    classes="settings-copy",
                    markup=False,
                ),
            )
        if page == "Model":
            return self._model_widgets()
        if page == "Permissions":
            return self._permission_widgets()
        if page == "Sandbox":
            return self._sandbox_widgets()
        if page == "Plugins":
            return self._plugin_widgets()
        return (
            Static(
                "These display choices apply to this TUI process only.",
                classes="settings-copy",
            ),
            Button(
                f"Thinking details: {'shown' if self.data.reasoning_visible else 'hidden'}",
                id="setting-thinking",
                classes="settings-action",
                compact=True,
            ),
            Button(
                f"Tool details: {'shown' if self.data.tool_details_visible else 'hidden'}",
                id="setting-details",
                classes="settings-action",
                compact=True,
            ),
            Static(
                "Theme selection is not exposed by the current typed TUI configuration.",
                classes="settings-copy",
            ),
        )

    def _model_widgets(self) -> tuple[Static | Button, ...]:
        widgets: list[Static | Button] = [
            Static(
                f"Provider: {self.data.provider or 'unknown'}",
                classes="settings-copy",
                markup=False,
            ),
            Button(
                "Change provider",
                id="setting-provider",
                classes="settings-action",
                disabled=not self.data.providers,
                compact=True,
            ),
            Static(
                f"Model: {self.data.model or 'unknown'}",
                classes="settings-copy",
                markup=False,
            ),
            Button(
                "Change model",
                id="setting-model",
                classes="settings-action",
                disabled=not self.data.models,
                compact=True,
            ),
            Static(
                f"Reasoning effort: {self.data.effort or 'unknown'}",
                classes="settings-copy",
                markup=False,
            ),
            Button(
                "Change effort",
                id="setting-effort",
                classes="settings-action",
                disabled=not self.data.efforts,
                compact=True,
            ),
            Static(
                f"Agent: {self.data.agent or 'unknown'}",
                classes="settings-copy",
                markup=False,
            ),
            Button(
                "Change agent",
                id="setting-agent",
                classes="settings-action",
                disabled=not self.data.agents,
                compact=True,
            ),
        ]
        if self.data.provider_error:
            widgets.append(
                Static(
                    f"Provider catalog unavailable: {self.data.provider_error}",
                    classes="settings-copy",
                    markup=False,
                )
            )
        if self.data.agent_error:
            widgets.append(
                Static(
                    f"Agent catalog unavailable: {self.data.agent_error}",
                    classes="settings-copy",
                    markup=False,
                )
            )
        return tuple(widgets)

    def _permission_widgets(self) -> tuple[Static, ...]:
        policy = self.data.session_policy
        if policy is None:
            message = self.data.policy_error or "The session policy has not loaded."
            return (Static(message, classes="settings-copy", markup=False),)
        lines = ["Session policy", f"Default: {policy.permissions.default_decision}"]
        lines.extend(_permission_rule(rule) for rule in policy.permissions.rules)
        lines += [
            "Effective policy",
            f"Default: {policy.effective_permissions.default_decision}",
        ]
        lines.extend(
            _permission_rule(rule)
            for rule in policy.effective_permissions.rules
        )
        return (
            Static("\n".join(lines), classes="settings-copy", markup=False),
        )

    def _sandbox_widgets(self) -> tuple[Static, ...]:
        policy = self.data.session_policy
        if policy is None:
            message = self.data.policy_error or "The session policy has not loaded."
            return (Static(message, classes="settings-copy", markup=False),)
        lines = ["Session policy"]
        lines.extend(_sandbox_values(policy.sandbox))
        lines += ["Effective policy", *_sandbox_values(policy.effective_sandbox)]
        return (
            Static("\n".join(lines), classes="settings-copy", markup=False),
        )

    def _plugin_widgets(self) -> tuple[Widget, ...]:
        catalog = self.data.plugin_config
        if catalog is None:
            message = self.data.plugin_config_error or "Plugin configuration has not loaded."
            return (Static(message, classes="settings-copy", markup=False),)
        applies_to = {
            "current_session": "current session",
            "new_sessions": "new sessions",
        }[catalog.applies_to]
        if not catalog.plugins:
            return (
                Static(
                    f"Scope: {catalog.scope} · {catalog.workspace_root}\n"
                    "No plugin configuration is declared for this workspace.",
                    classes="settings-copy",
                    markup=False,
                ),
            )
        selected = next(
            (
                plugin
                for plugin in catalog.plugins
                if plugin.plugin_id == self._selected_plugin_id
            ),
            catalog.plugins[0],
        )
        self._selected_plugin_id = selected.plugin_id
        widgets: list[Widget] = [
            Static(
                f"Scope: {catalog.scope} · {catalog.workspace_root} · Applies to: {applies_to}",
                classes="settings-copy",
                markup=False,
            ),
            Select(
                [
                    (f"{plugin.name} ({plugin.plugin_id})", plugin.plugin_id)
                    for plugin in catalog.plugins
                ],
                id="plugin-config-selection",
                value=selected.plugin_id,
                allow_blank=False,
            ),
        ]
        widgets.append(
            Static(
                f"{selected.name} · {selected.plugin_id}",
                classes="settings-copy",
                markup=False,
            )
        )
        form = plugin_schema(selected)
        if isinstance(form, UnsupportedSchema):
            widgets.append(
                Static(
                    f"Configuration editor unavailable: {form.reason}",
                    classes="settings-copy",
                    markup=False,
                )
            )
        elif not form.fields:
            widgets.append(
                Static(
                    "This schema declares no editable fields.",
                    classes="settings-copy",
                    markup=False,
                )
            )
        else:
            for index, field in enumerate(form.fields):
                widgets.extend(self._plugin_field_widgets(selected, field, index))
        if self._plugin_message:
            widgets.append(
                Static(
                    self._plugin_message,
                    id="settings-plugin-message",
                    classes="settings-copy",
                    markup=False,
                )
            )
        return tuple(widgets)

    def _plugin_field_widgets(
        self, plugin, field: SchemaField, index: int
    ) -> tuple[Widget, ...]:
        changes = self._plugin_changes.get(plugin.plugin_id, {})
        removals = self._plugin_removals.get(plugin.plugin_id, set())
        if field.name in changes:
            value = changes[field.name]
        elif field.name in plugin.scope_config:
            value = plugin.scope_config[field.name]
        elif field.name in plugin.effective_config:
            value = plugin.effective_config[field.name]
        else:
            value = field.schema.get("default")
        scope_override = field.name in plugin.scope_config
        source = "scope override" if scope_override else "inherited / default"
        if field.name in removals:
            source = "will inherit after applying"

        field_id = f"plugin-config-field-{index}"
        self._plugin_field_bindings[field_id] = (plugin.plugin_id, field)
        controls: list[Widget] = [
            Static(f"{field.label} · {source}", classes="settings-copy", markup=False)
        ]
        if field.kind == "boolean":
            controls.append(
                Checkbox(
                    field.label,
                    id=field_id,
                    classes="plugin-config-checkbox",
                    value=value is True,
                )
            )
        elif field.kind == "enum":
            options = field.options
            if field.nullable and None not in options:
                options += (None,)
            self._plugin_enum_values[field_id] = options
            selected_index = next(
                (
                    option_index
                    for option_index, option in enumerate(options)
                    if type(option) is type(value) and option == value
                ),
                0 if options else -1,
            )
            controls.append(
                Select(
                    [
                        (
                            "— unset —" if option is None else str(option),
                            str(option_index),
                        )
                        for option_index, option in enumerate(options)
                    ],
                    id=field_id,
                    classes="plugin-config-enum",
                    value=str(selected_index) if selected_index >= 0 else Select.BLANK,
                    allow_blank=field.nullable,
                )
            )
        else:
            controls.append(
                Input(
                    value="" if value is None else str(value),
                    placeholder="Unset" if field.nullable else field.label,
                    id=field_id,
                    classes="plugin-config-input",
                )
            )
        if scope_override or field.name in removals:
            clear_id = f"plugin-config-clear-{index}"
            self._plugin_clear_bindings[clear_id] = (plugin.plugin_id, field.name)
            controls.append(
                Button(
                    "Keep override" if field.name in removals else "Clear override",
                    id=clear_id,
                    classes="settings-action",
                )
            )
        return tuple(controls)

    def _has_plugin_changes(self) -> bool:
        plugin_id = self._selected_plugin_id
        return bool(
            self._plugin_changes.get(plugin_id)
            or self._plugin_removals.get(plugin_id)
        )

    def _set_plugin_value(
        self, plugin_id: str, field: SchemaField, value: JsonValue
    ) -> None:
        catalog = self.data.plugin_config
        if catalog is None:
            return
        plugin = next(
            (item for item in catalog.plugins if item.plugin_id == plugin_id), None
        )
        if plugin is None:
            return
        changes = self._plugin_changes.setdefault(plugin_id, {})
        removals = self._plugin_removals.setdefault(plugin_id, set())
        removals.discard(field.name)
        if field.name in plugin.scope_config and _json_same(
            plugin.scope_config[field.name], value
        ):
            changes.pop(field.name, None)
        else:
            changes[field.name] = value

    def _plugin_draft(self) -> PluginConfigDraft:
        plugin_id = self._selected_plugin_id
        return PluginConfigDraft(
            plugin_id=plugin_id,
            values=dict(self._plugin_changes.get(plugin_id, {})),
            removals=tuple(sorted(self._plugin_removals.get(plugin_id, set()))),
        )

    def _plugin_save_action(self) -> PluginConfigSaveAction:
        catalog = self.data.plugin_config
        if catalog is None:
            raise RuntimeError("plugin configuration catalog is not loaded")
        plugin = next(
            item
            for item in catalog.plugins
            if item.plugin_id == self._selected_plugin_id
        )
        draft = self._plugin_draft()
        return PluginConfigSaveAction(
            plugin_id=plugin.plugin_id,
            patch=PatchPluginConfig(
                scope=catalog.scope,
                revision=catalog.revision,
                config=apply_changes(
                    plugin.scope_config, draft.values, draft.removals
                ),
            ),
            draft=draft,
        )

    def _show_plugin_message(self, message: str) -> None:
        self._plugin_message = message
        for widget in self.query("#settings-plugin-message"):
            if isinstance(widget, Static):
                widget.update(message)

    def _refresh_plugin_apply(self) -> None:
        for widget in self.query("#plugin-config-apply"):
            if isinstance(widget, Button):
                widget.disabled = (
                    not self._has_plugin_changes() or bool(self._invalid_plugin_fields)
                )


def _permission_rule(rule: PermissionRule) -> str:
    qualifiers = []
    if rule.param_patterns:
        qualifiers.append(f"parameters={rule.param_patterns}")
    if rule.path_scope is not None:
        qualifiers.append(f"path={rule.path_scope}")
    suffix = f" ({', '.join(qualifiers)})" if qualifiers else ""
    return f"{rule.decision}: {rule.tool_pattern}{suffix}"


def _sandbox_values(config: SandboxConfig) -> list[str]:
    values = config.model_dump(mode="json", exclude={"resources"})
    lines = [f"{name}: {value}" for name, value in values.items()]
    lines.extend(
        f"resource {resource.path}: {resource.access}"
        for resource in config.resources
    )
    return lines


def _parse_input_value(field: SchemaField, raw: str) -> JsonValue:
    if field.kind == "string":
        value: JsonValue = raw
        minimum = field.schema.get("minLength")
        maximum = field.schema.get("maxLength")
        if isinstance(minimum, int) and len(raw) < minimum:
            raise ValueError(f"{field.label} must have at least {minimum} characters.")
        if isinstance(maximum, int) and len(raw) > maximum:
            raise ValueError(f"{field.label} must have at most {maximum} characters.")
        return value
    if raw == "" and field.nullable:
        return None
    if raw == "":
        raise ValueError(f"{field.label} is required.")
    try:
        value = int(raw) if field.kind == "integer" else float(raw)
    except ValueError as exc:
        raise ValueError(f"{field.label} must be a {field.kind}.") from exc
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field.label} must be finite.")
    exclusive_minimum = field.schema.get("exclusiveMinimum")
    exclusive_maximum = field.schema.get("exclusiveMaximum")
    if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
        raise ValueError(f"{field.label} must be greater than {exclusive_minimum}.")
    if isinstance(exclusive_maximum, (int, float)) and value >= exclusive_maximum:
        raise ValueError(f"{field.label} must be less than {exclusive_maximum}.")
    minimum = field.schema.get("minimum")
    maximum = field.schema.get("maximum")
    if isinstance(minimum, (int, float)) and value < minimum:
        raise ValueError(f"{field.label} must be at least {minimum}.")
    if isinstance(maximum, (int, float)) and value > maximum:
        raise ValueError(f"{field.label} must be at most {maximum}.")
    return value


def _json_same(left: JsonValue, right: JsonValue) -> bool:
    return type(left) is type(right) and left == right


__all__ = [
    "PAGES",
    "SETTINGS_CSS",
    "SettingName",
    "SettingsAction",
    "SettingsData",
    "SettingsScreen",
    "PluginConfigDraft",
    "PluginConfigSaveAction",
]
