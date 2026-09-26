"""The Textual app: widgets, keys, and lifecycle. No business decisions.

This module answers only "what is on screen and what does a key do". Everything
else -- what the status is, what an event means, when to redraw -- belongs to the
controller, the reducer, and the transport.

``TextualViewAdapter`` is the one place that knows both worlds. It renders the
models it is handed and owns the transcript's scroll position; it derives nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Sequence

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual import on
from textual.widgets import Collapsible, TextArea

from XBotv2.jobs.contracts import JobView
from XBotv2.interactions.protocol import UserInputRequest
from XBotv2.permissions.contracts import NamedPermission, PermissionRequest, ToolPermission
from XBotv2.session.contracts import PendingInputData
from XBotv2.commands import CommandDescription, CommandsPort, split_command_args
from XBotv2.tui.attachments import load_image
from XBotv2.tui.commands import CommandRegistry, format_command_help
from XBotv2.tui.controller import TuiController
from XBotv2.tui.events import InteractionRequest, LocalNotice
from XBotv2.tui.theme import TUI_CSS
from XBotv2.tui.state import SessionState
from XBotv2.tui.transport import (
    SessionBackend,
    TransportConfig,
    XBotClientError,
)
from XBotv2.tui.view.completion import CompletionPopup, CompletionPresenter
from XBotv2.tui.view.palette import CommandPalette
from XBotv2.tui.view.pickers import (
    agent_options,
    filter_options,
    effort_options,
    effort_tiers,
    model_options,
    provider_options,
    session_options,
    thread_options,
)
from XBotv2.tui.view.selection import Option, SelectionScreen
from XBotv2.tui.view.settings import (
    PluginConfigDraft,
    PluginConfigSaveAction,
    SettingsAction,
    SettingsData,
    SettingsScreen,
)
from XBotv2.tui.view.composer import Composer, ComposerModel, composer_can_submit
from XBotv2.tui.view.entries import BlockVisibility, block_choice
from XBotv2.tui.view.footer import FooterBar
from XBotv2.tui.view.jobs import JobPanel
from XBotv2.tui.view.interaction import InteractionInputScreen
from XBotv2.tui.view.queue import QueuePanel
from XBotv2.tui.view.status_bar import StatusBar, StatusLine, status_report
from XBotv2.tui.view.transcript import TranscriptScroll, TranscriptView

if TYPE_CHECKING:
    from XBotv2.config import PluginConfigCatalog

class TextualViewAdapter:
    """Renders the controller's models into the mounted widgets."""

    def __init__(
        self,
        *,
        transcript: TranscriptView,
        status: StatusBar,
        jobs: JobPanel,
        queue: QueuePanel,
        job_disclosure: Collapsible,
        panels: Horizontal,
        composer: Composer,
        footer: FooterBar,
        present_interaction: Callable[[InteractionRequest | None], Awaitable[None]],
    ) -> None:
        self.transcript = transcript
        self.status = status
        self.jobs = jobs
        self.queue = queue
        self.job_disclosure = job_disclosure
        self.panels = panels
        self.composer = composer
        self.footer = footer
        self.present_interaction = present_interaction

    async def render_transcript(
        self, state: SessionState, *, thinking: bool
    ) -> bool:
        return await self.transcript.render(state, thinking=thinking)

    def render_status(self, model: StatusLine) -> None:
        self.status.show(model, width=self.status.size.width or 80)

    def render_jobs(self, jobs: Sequence[JobView]) -> None:
        self.jobs.show(jobs, width=self.jobs.size.width or 80)
        self.job_disclosure.title = _panel_title("Tasks", len(jobs))
        self._refresh_panels()

    def render_queue(self, items: Sequence[PendingInputData]) -> None:
        self.queue.show(items, width=self.queue.size.width or 80)
        self.queue.display = bool(items)

    def _refresh_panels(self) -> None:
        """One owner for the shared container: either panel may be showing.

        The job list remains an optional disclosure. Queued prompts are a
        separate visible list above the composer, so their content never hides
        behind a count-only summary.
        """
        jobs = self.jobs.rows > 0
        self.jobs.display = jobs
        self.job_disclosure.display = jobs
        self.panels.set_class(jobs, "visible")

    def render_composer(self, model: ComposerModel) -> None:
        self.composer.show(model)
        self.footer.show(model, width=self.footer.size.width or 80)

    async def render_interaction(self, request: InteractionRequest | None) -> None:
        await self.present_interaction(request)

    async def page_older(self, state: SessionState) -> bool:
        return await self.transcript.page_older(state)

    async def page_newer(self, state: SessionState) -> bool:
        return await self.transcript.page_newer(state)

    async def go_to_tail(self, state: SessionState) -> None:
        await self.transcript.go_to_tail(state)


# Which optional block each display command controls: model field, and the noun
# used when telling the user what changed.
_BLOCKS: dict[str, tuple[str, str]] = {
    "thinking": ("reasoning", "Reasoning"),
    "details": ("details", "Tool details"),
}


class TuiApp(App[None]):
    """The XBotv2 terminal UI."""

    TITLE = "XBotv2"
    CSS = TUI_CSS
    # Textual ships its own ctrl+p palette; the client's own catalogue owns that
    # binding, so the built-in one is turned off.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+c", "copy_or_quit", "Copy / quit", priority=True),
        Binding("ctrl+d", "quit", "Quit"),
        Binding("escape", "interrupt", "Interrupt the turn"),
        Binding("pageup", "older", "Older", show=False, priority=True),
        Binding("pagedown", "newer", "Newer", show=False, priority=True),
        Binding("ctrl+p", "palette", "Commands"),
        Binding("ctrl+t", "agents", "Agent threads"),
        Binding("f2", "settings", "Settings", priority=True),
        Binding("ctrl+e", "expand_blocks", "Expand folded content", priority=True),
    ]

    def __init__(
        self,
        *,
        backend: SessionBackend,
        commands: CommandsPort,
        config: TransportConfig | None = None,
        workspace: str = "",
        assistant_label: str = "Assistant",
        transcript_limit: int = 100,
        render_interval: float = 0.1,
        new_input_id: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._config = config or TransportConfig()
        self._workspace = workspace
        self.assistant_label = assistant_label
        self.transcript_limit = transcript_limit
        self.render_interval = render_interval
        self._new_input_id = new_input_id
        self.controller: TuiController | None = None
        self.view: TextualViewAdapter | None = None
        # The command catalogue is chrome, not session state: it describes what
        # the user can type, not what the session is.
        self.commands = CommandRegistry(commands)
        self.completion = CompletionPresenter(self.commands)
        self._tasks: list[asyncio.Task] = []
        self._presented_interaction_id: str | None = None
        self._active_interaction_screen: (
            SelectionScreen | InteractionInputScreen | None
        ) = None

    @property
    def workspace(self) -> str:
        """The workspace label shown in the status line."""
        return self._workspace

    @property
    def transport_config(self) -> TransportConfig:
        """How this app was told to attach; read-only, for diagnostics and tests."""
        return self._config

    @property
    def background_tasks(self) -> tuple[asyncio.Task, ...]:
        """The tasks this app started; empty after shutdown."""
        return tuple(self._tasks)

    def compose(self) -> ComposeResult:
        yield TranscriptScroll(id="transcript")
        with Horizontal(id="panels"):
            yield Collapsible(JobPanel(id="jobs"), title="Tasks", collapsed=True, id="job-panel")
        yield QueuePanel(id="queue")
        # Bottom of the screen: visible queued prompts, completion, composer,
        # transient status, then context-sensitive key hints.
        yield CompletionPopup(id="completion", registry=self.commands)
        yield Composer(id="composer", on_submit=self._submit, completion=self.completion)
        yield StatusBar(id="status")
        yield FooterBar(id="footer")

    async def on_mount(self) -> None:
        self.view = TextualViewAdapter(
            transcript=TranscriptView(
                self.query_one("#transcript", TranscriptScroll),
                limit=self.transcript_limit,
                assistant_label=self.assistant_label,
            ),
            status=self.query_one("#status", StatusBar),
            jobs=self.query_one("#jobs", JobPanel),
            queue=self.query_one("#queue", QueuePanel),
            job_disclosure=self.query_one("#job-panel", Collapsible),
            panels=self.query_one("#panels", Horizontal),
            composer=self.query_one("#composer", Composer),
            footer=self.query_one("#footer", FooterBar),
            present_interaction=self._present_interaction,
        )
        self.controller = TuiController(
            self._backend,
            view=self.view,
            config=self._config,
            workspace=self._workspace,
            new_input_id=self._new_input_id,
        )
        self.completion.attach(self.query_one("#completion", CompletionPopup))
        self.query_one("#composer", Composer).focus()
        await self._boot()

    async def _boot(self) -> None:
        assert self.controller is not None
        try:
            await self.controller.connect()
        except Exception:  # noqa: BLE001 — the reason must be readable, not a traceback
            # ``connect`` has already reported the failure as a visible error and
            # a DISCONNECTED state (the controller flushes in a ``finally``). A
            # client that cannot attach stays up: the user needs the reason on the
            # screen they are looking at (and to be able to run /exit), not a
            # traceback out of the alternate screen. Nothing else is started:
            # there is no stream to read and no watchdog to poll.
            return
        await self._adopt_commands()
        self._tasks = [
            asyncio.create_task(self.controller.run(), name="tui-read"),
            asyncio.create_task(
                self.controller.flush_loop(self.render_interval), name="tui-render"
            ),
            asyncio.create_task(self.controller.watch(), name="tui-watch"),
        ]

    async def on_unmount(self) -> None:
        if self.controller is not None:
            self.controller.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def merge_commands(self, catalog: Sequence[CommandDescription]) -> None:
        """Adopt the server's command catalogue."""
        self.commands.merge(catalog)

    async def _adopt_commands(self) -> None:
        """Ask the server what it offers for the attached thread.

        Server commands are discovered, never listed here: the catalogue decides
        what the user can type, so a plugin that adds a command needs no client
        change. A thread or session switch re-reads it, because the answer is
        per attached thread.
        """
        if self.controller is None:
            return
        try:
            catalog = await self.controller.commands()
        except Exception as exc:  # noqa: BLE001 — the user must see why discovery failed
            self._notify("command", f"Could not list server commands: {exc}")
            return
        self.merge_commands(catalog)

    @on(TextArea.Changed)
    def _composer_changed(self, event: TextArea.Changed) -> None:
        if self.view is not None and event.text_area is self.view.composer.input:
            self.completion.refresh(event.text_area.text)

    # --- actions ------------------------------------------------------

    async def _submit(
        self, text: str, delivery: Literal["queue", "steer"]
    ) -> None:
        """A slash command is the client's to run; anything else is a message.

        The client commands keep working in a read-only view (that is how the
        user leaves one); a message does not, and saying so is the difference
        between a rule and a silently swallowed keystroke.
        """
        if self.controller is None:
            return
        parsed = self.commands.parse(text)
        if parsed is None:
            if not composer_can_submit(self.controller.composer_model(), text=text):
                self._notify(
                    self.composer_refusal_kind(),
                    self.composer_refusal(),
                )
                return
            await self.controller.submit(text, delivery=delivery)
            return
        await self._run_command(parsed, delivery=delivery)

    def composer_refusal_kind(self) -> str:
        return "thread" if self.controller is not None and self.controller.state.read_only else "composer"

    def composer_refusal(self) -> str:
        if self.controller is not None and self.controller.state.read_only:
            return "This is a read-only thread view; only commands run here."
        return "There is nothing to send."

    async def _run_command(
        self, parsed, *, delivery: Literal["queue", "steer"]
    ) -> None:
        if parsed.description is None:
            self._notify("command", f"Unknown command: {parsed.raw}")
            return
        # Not one of ours. The catalogue says who runs it, so the client never
        # guesses: a prompt command is a prompt template and belongs on the
        # message endpoint; anything else is one command line for the server.
        kind = parsed.description.kind if parsed.description is not None else "server"
        if kind == "client":
            command = self.commands.client_command(parsed.name)
            if command is None or command.handler is None:
                self._notify("command", f"Client command /{parsed.name} is unavailable.")
                return
            result = await command.handler(parsed.args)
            if result.status == "error":
                self._notify(parsed.name, result.message)
            elif result.message:
                self._notify(parsed.name, result.message)
            return
        if kind == "prompt":
            assert self.controller is not None
            await self.controller.submit(parsed.raw, delivery=delivery)
            return
        await self._run_server_command(parsed)

    async def _run_server_command(self, parsed) -> None:
        assert self.controller is not None
        try:
            result = await self.controller.run_command(parsed.raw)
        except Exception as exc:  # noqa: BLE001 — a failed command must be visible
            self._notify(parsed.name, f"/{parsed.name} failed: {exc}")
            return
        message = result.message.strip()
        if not message:
            message = f"/{parsed.name}: no output"
        self._notify(parsed.name, message)

    def _notify(self, kind: str, text: str) -> None:
        if self.controller is not None:
            self.controller.dispatch(LocalNotice(notice_kind=kind, text=text))

    @staticmethod
    def _selection_screen(
        title: str,
        options: Sequence[Option],
        *,
        searchable: bool = True,
        description: str = "",
        hint: str = "",
        compact: bool = False,
        cancel_value: str | None = None,
    ) -> SelectionScreen:
        choices = tuple(options)
        return SelectionScreen(
            title,
            choices,
            search=(lambda query: filter_options(choices, query)) if searchable else None,
            placeholder="filter" if searchable else "",
            description=description,
            hint=hint,
            compact=compact,
            cancel_value=cancel_value,
        )

    async def _present_interaction(
        self, request: InteractionRequest | None
    ) -> None:
        """Focus the first new blocking request in a typed chooser.

        The request already belongs to the reducer's pending-interaction state;
        this method adds no lifecycle or response semantics. Dismissing with
        Escape leaves the transcript card and slash-command fallback available,
        while a later request gets its own chooser.
        """
        if request is None:
            self._presented_interaction_id = None
            active = self._active_interaction_screen
            self._active_interaction_screen = None
            if active is not None and self.screen is active:
                active.dismiss(None)
            return
        if request.interaction_id == self._presented_interaction_id:
            return
        screen = self._interaction_screen(request)
        if screen is None:
            return
        self._presented_interaction_id = request.interaction_id
        self._active_interaction_screen = screen
        await self.push_screen(
            screen,
            callback=lambda value: self._interaction_chosen(request, value),
        )

    def _interaction_chosen(
        self, request: InteractionRequest, value: str | None
    ) -> None:
        self._active_interaction_screen = None
        self.call_after_refresh(self.query_one("#composer", Composer).focus)
        if value is not None:
            self.run_worker(
                self._resolve_interaction(request, value),
                exclusive=False,
                name="tui-interaction",
            )

    async def _resolve_interaction(
        self, request: InteractionRequest, value: str
    ) -> None:
        if self.controller is None:
            return
        try:
            if isinstance(request, PermissionRequest):
                if value == "deny":
                    await self.controller.respond_permission(
                        request.interaction_id, "deny"
                    )
                else:
                    await self.controller.respond_permission(
                        request.interaction_id, "allow", value
                    )
            elif isinstance(request, UserInputRequest):
                await self.controller.respond_user_input(
                    request.interaction_id, value
                )
        except Exception as exc:  # noqa: BLE001 — failed interaction stays visible
            self._notify(
                "interaction",
                f"Could not respond to {request.interaction_id}: {exc}",
            )


    def _interaction_screen(
        self,
        request: InteractionRequest,
    ) -> SelectionScreen | InteractionInputScreen | None:
        """Build presentation directly from the public typed request."""
        if isinstance(request, PermissionRequest):
            if isinstance(request.subject, ToolPermission):
                subject = request.subject.tool_call.name
            elif isinstance(request.subject, NamedPermission):
                subject = request.subject.tool
            else:  # pragma: no cover - PermissionSubject is closed
                return None
            return self._selection_screen(
                "Permission required",
                (
                    Option("once", "Allow once"),
                    Option("session", "Allow for this session"),
                    Option("deny", "Deny"),
                ),
                searchable=False,
                description=f"Tool: {subject}\n{request.reason}".strip(),
                hint="↑↓ choose · Enter confirm · Esc deny",
                compact=True,
                cancel_value="deny",
            )
        if isinstance(request, UserInputRequest) and request.options:
            return self._selection_screen(
                "Question",
                tuple(
                    Option(option.label, option.label, option.description)
                    for option in request.options
                ),
                searchable=False,
                description=(
                    f"{request.question}\nRequested by {request.source}"
                    if request.source
                    else request.question
                ),
                hint="↑↓ choose · Enter confirm · Esc dismiss",
                compact=True,
            )
        if isinstance(request, UserInputRequest):
            return InteractionInputScreen(request.question, source=request.source)
        return None

    async def _cmd_help(self, raw_args: str) -> None:
        try:
            args = split_command_args(raw_args)
        except ValueError as error:
            self._notify("help", str(error))
            return
        if len(args) > 1:
            self._notify("help", "Usage: /help [command]")
            return
        if args:
            spec = self.commands.resolve(args[0])
            if spec is None:
                name = args[0].lstrip("/")
                self._notify(
                    "help",
                    f"Unknown command: /{name}\nUse /help to list available commands.",
                )
                return
            self._notify("help", format_command_help(spec))
            return
        lines = [
            f"{spec.slash}  {spec.description}"
            for name in self.commands.names()
            for spec in [self.commands.get(name)]
            if spec is not None
        ]
        self._notify("help", "\n".join(lines))

    async def _choose(self, *, title: str, load, apply) -> None:
        """Offer a selection and apply the chosen row.

        Every picker in the client comes through here -- ``/session``,
        ``/thread``, ``/provider``, ``/model``, ``/effort``, ``/agent``. Only two
        things differ between them: where the rows come from (a pure function of
        a model the server sent) and what a chosen row does. A command that
        pushes its own screen would be a second implementation of this one.
        """
        try:
            options = tuple(await load())
        except Exception as exc:  # noqa: BLE001 — the user must see why it is empty
            self._notify(title.lower(), f"Could not list {title.lower()}: {exc}")
            return
        if not options:
            self._notify(title.lower(), f"Nothing to choose from in {title.lower()}.")
            return
        await self.push_screen(
            self._selection_screen(title, options),
            callback=lambda value: self._chosen(value, apply),
        )

    def _chosen(self, value: str | None, apply) -> None:
        if value is not None:
            self.run_worker(apply(value), exclusive=False, name="tui-choose")

    async def _cmd_session(self, args: str) -> None:
        """Switch sessions by name, or offer the ones the server holds."""
        target = args.strip()
        if target:
            await self._switch_session(target)
            return
        await self._choose(
            title="Sessions",
            load=self._load_sessions,
            apply=self._apply_session,
        )

    async def _cmd_status(self, args: str) -> None:
        """Open the read-only status page from what the client already holds.

        The server publishes a ``/status`` for clients that have none of this;
        this one is local, so it costs no round trip and can include what the
        client knows (the running turn's elapsed time, the queue, the tasks).
        """
        if args.strip():
            await self._delegate("status", args)
            return
        await self._open_settings(read_only=True)

    async def _cmd_settings(self, args: str) -> None:
        if args.strip():
            self._notify("settings", "Usage: /settings")
            return
        await self._open_settings()

    async def _open_settings(
        self,
        *,
        initial_page: str = "Status",
        read_only: bool = False,
        plugin_config_catalog: PluginConfigCatalog | None = None,
        plugin_draft: PluginConfigDraft | None = None,
        plugin_status: str = "",
    ) -> None:
        """Read the settings inputs concurrently through the public client port."""
        if self.controller is None or self.view is None:
            return
        provider_result = agent_result = policy_result = plugin_config_result = None
        if not read_only:
            if plugin_config_catalog is None:
                (
                    provider_result,
                    agent_result,
                    policy_result,
                    plugin_config_result,
                ) = await asyncio.gather(
                    self.controller.providers(),
                    self.controller.agents(),
                    self.controller.session_policy(),
                    self.controller.plugin_config(),
                    return_exceptions=True,
                )
            else:
                provider_result, agent_result, policy_result = await asyncio.gather(
                    self.controller.providers(),
                    self.controller.agents(),
                    self.controller.session_policy(),
                    return_exceptions=True,
                )
                plugin_config_result = plugin_config_catalog
        state = self.controller.state
        provider_error = (
            str(provider_result)
            if isinstance(provider_result, BaseException)
            else ""
        )
        agent_error = (
            str(agent_result) if isinstance(agent_result, BaseException) else ""
        )
        policy_error = (
            str(policy_result) if isinstance(policy_result, BaseException) else ""
        )
        plugin_config_error = (
            str(plugin_config_result)
            if isinstance(plugin_config_result, BaseException)
            else ""
        )
        providers = (
            provider_options(provider_result, current=state.provider)
            if not read_only and not isinstance(provider_result, BaseException)
            else ()
        )
        models = (
            model_options(provider_result, provider=state.provider)
            if not read_only and not isinstance(provider_result, BaseException)
            else ()
        )
        efforts = (
            effort_options(
                effort_tiers(provider_result, provider=state.provider, model=state.model),
                current=state.model_mode,
            )
            if not read_only and not isinstance(provider_result, BaseException)
            else ()
        )
        agents = (
            agent_options(agent_result)
            if not read_only and not isinstance(agent_result, BaseException)
            else ()
        )
        visibility = self.view.transcript.visibility
        data = SettingsData(
            status=status_report(
                state,
                workspace=self._workspace,
                activity=self.controller.activity(),
            ),
            provider=state.provider,
            model=state.model,
            effort=state.model_mode,
            agent=state.agent_name,
            providers=providers,
            models=models,
            efforts=efforts,
            agents=agents,
            provider_error=provider_error,
            agent_error=agent_error,
            policy_error=policy_error,
            session_policy=(
                None if isinstance(policy_result, BaseException) else policy_result
            ),
            plugin_config_error=plugin_config_error,
            plugin_config=(
                None
                if isinstance(plugin_config_result, BaseException)
                else plugin_config_result
            ),
            plugin_draft=plugin_draft,
            plugin_status=plugin_status,
            reasoning_visible=visibility.reasoning,
            tool_details_visible=visibility.details,
        )
        self.push_screen(
            SettingsScreen(
                data,
                initial_page=initial_page,
                read_only=read_only,
            ),
            callback=self._settings_action_chosen,
        )

    def _settings_action_chosen(
        self, action: SettingsAction | PluginConfigSaveAction | None
    ) -> None:
        if action is None:
            self._restore_composer_focus()
            return
        if isinstance(action, PluginConfigSaveAction):
            self.run_worker(
                self._save_plugin_config(action),
                exclusive=False,
                name="tui-plugin-config-save",
            )
            return
        if action.name in {"thinking", "details"}:
            self.run_worker(
                self._apply_settings_visibility(action.name),
                exclusive=False,
                name="tui-settings-appearance",
            )
            return

        titles = {
            "provider": ("Providers", self._apply_provider),
            "model": ("Models", self._apply_model),
            "effort": ("Reasoning effort", self._apply_effort),
            "agent": ("Agents", self._apply_agent),
        }
        title, apply = titles[action.name]
        if not action.choices:
            self._notify("settings", f"No {title.lower()} are available.")
            self._restore_composer_focus()
            return
        self.push_screen(
            self._selection_screen(title, action.choices),
            callback=lambda value: self._settings_value_chosen(value, apply),
        )

    async def _save_plugin_config(self, action: PluginConfigSaveAction) -> None:
        if self.controller is None:
            return
        try:
            catalog = await self.controller.update_plugin_config(
                action.plugin_id, action.patch
            )
        except Exception as exc:
            if isinstance(exc, XBotClientError) and exc.code == "plugin_config_conflict":
                message = (
                    "The catalog changed. Your draft is preserved; review the "
                    "latest values and press Apply again to confirm."
                )
            else:
                message = (
                    f"Save failed: {exc}. Your draft is preserved; review it "
                    "and retry when ready."
                )
            await self._open_settings(
                initial_page="Plugins",
                plugin_draft=action.draft,
                plugin_status=message,
            )
            return
        await self._open_settings(
            initial_page="Plugins",
            plugin_config_catalog=catalog,
            plugin_status="Plugin configuration saved.",
        )

    def _settings_value_chosen(self, value: str | None, apply) -> None:
        if value is None:
            self._restore_composer_focus()
            return
        self.run_worker(
            self._apply_setting_and_restore_focus(value, apply),
            exclusive=False,
            name="tui-settings-selection",
        )

    async def _apply_setting_and_restore_focus(self, value: str, apply) -> None:
        try:
            await apply(value)
        finally:
            self._restore_composer_focus()

    async def _apply_settings_visibility(self, name: str) -> None:
        await (self._cmd_thinking("toggle") if name == "thinking" else self._cmd_details("toggle"))
        self._restore_composer_focus()

    def _restore_composer_focus(self) -> None:
        if self.is_mounted:
            self.call_after_refresh(self.query_one("#composer", Composer).input.focus)

    async def action_settings(self) -> None:
        await self._open_settings()

    async def action_agents(self) -> None:
        """Inspect the server's agent threads through the shared picker."""
        await self._choose(
            title="Agent threads",
            load=self._load_threads,
            apply=self._apply_thread,
        )

    async def _cmd_jobs(self, args: str) -> None:
        """Show the tasks this client is tracking; anything else is the server's."""
        if args.strip():
            await self._delegate("jobs", args)
            return
        assert self.controller is not None
        jobs = tuple(self.controller.state.jobs.values())
        if not jobs:
            self._notify("jobs", "No background tasks.")
            return
        from XBotv2.tui.view.jobs import order_jobs, job_row

        width = self.size.width or 80
        self._notify(
            "jobs",
            "\n".join(job_row(job, width=width) for job in order_jobs(jobs)),
        )

    async def _cmd_provider(self, args: str) -> None:
        if args.strip():
            await self._delegate("provider", args)
            return
        await self._choose(
            title="Providers", load=self._load_providers, apply=self._apply_provider
        )

    async def _cmd_model(self, args: str) -> None:
        if args.strip():
            await self._delegate("model", args)
            return
        await self._choose(title="Models", load=self._load_models, apply=self._apply_model)

    async def _cmd_effort(self, args: str) -> None:
        if args.strip():
            await self._delegate("effort", args)
            return
        await self._choose(
            title="Reasoning effort", load=self._load_efforts, apply=self._apply_effort
        )

    async def _cmd_agent(self, args: str) -> None:
        if args.strip():
            await self._delegate("agent", args)
            return
        await self._choose(title="Agents", load=self._load_agents, apply=self._apply_agent)

    # --- rows, and what choosing one does ---------------------------------
    async def _load_sessions(self):
        assert self.controller is not None
        return session_options(await self.controller.sessions())

    async def _load_threads(self):
        assert self.controller is not None
        return thread_options(await self.controller.threads())

    async def _load_providers(self):
        assert self.controller is not None
        return provider_options(
            await self.controller.providers(), current=self.controller.state.provider
        )

    async def _load_models(self):
        assert self.controller is not None
        return model_options(
            await self.controller.providers(), provider=self.controller.state.provider
        )

    async def _load_agents(self):
        assert self.controller is not None
        return agent_options(await self.controller.agents())

    async def _load_efforts(self):
        assert self.controller is not None
        state = self.controller.state
        catalog = await self.controller.providers()
        tiers = effort_tiers(catalog, provider=state.provider, model=state.model)
        return effort_options(tiers, current=state.model_mode)

    async def _apply_session(self, value: str) -> None:
        await self._switch_session(value)

    async def _apply_thread(self, value: str) -> None:
        await self._switch_thread(value)

    async def _apply_provider(self, value: str) -> None:
        await self._select(lambda: self.controller.select_provider(value), f"Provider: {value}")

    async def _apply_model(self, value: str) -> None:
        assert self.controller is not None
        provider = self.controller.state.provider
        await self._select(
            lambda: self.controller.select_provider(provider, value), f"Model: {value}"
        )

    async def _apply_effort(self, value: str) -> None:
        await self._select(lambda: self.controller.select_effort(value), f"Effort: {value}")

    async def _apply_agent(self, value: str) -> None:
        await self._select(lambda: self.controller.select_agent(value), f"Agent: {value}")

    async def _select(self, call, label: str) -> None:
        """Apply a server-side selection and say so; a refusal must be visible."""
        assert self.controller is not None
        try:
            await call()
        except Exception as exc:  # noqa: BLE001 — a refused selection must be visible
            self._notify("select", f"{label} failed: {exc}")
            return
        # A different provider or agent can offer different commands.
        await self._adopt_commands()
        self._notify("select", label)

    async def _delegate(self, name: str, args: str) -> None:
        """Run a command the server owns, with the arguments as typed."""
        assert self.controller is not None
        try:
            result = await self.controller.run_command(f"/{name} {args}".strip())
        except Exception as exc:  # noqa: BLE001 — a failed command must be visible
            self._notify(name, f"/{name} failed: {exc}")
            return
        message = result.message.strip() or f"/{name}: no output"
        self._notify(name, message)

    async def _cmd_thread(self, args: str) -> None:
        """Show what a session thread holds; a subagent thread is read-only.

        ``main`` (or the attached thread's own id) returns to the thread the
        session is driven from, and is the only target that resolves without the
        picker: the server decides which thread is the main one.
        """
        assert self.controller is not None
        target = args.strip()
        if target:
            if target in {"main", self.controller.state.thread_id}:
                await self._switch_thread("")
                return
            await self._switch_thread(target)
            return
        await self._choose(
            title="Threads", load=self._load_threads, apply=self._apply_thread
        )

    async def _switch_thread(self, thread_id: str) -> None:
        assert self.controller is not None
        label = thread_id or "main"
        try:
            await self.controller.switch_thread(thread_id)
        except Exception as exc:  # noqa: BLE001 — a failed read must be visible
            self._notify("thread", f"Could not read thread {label}: {exc}")
            return
        await self._adopt_commands()
        if self.controller.state.read_only:
            self._notify(
                "thread",
                f"Viewing subagent thread {self.controller.state.thread_id} (read-only)",
            )
        else:
            self._notify("thread", "Back on the main thread")

    async def _switch_session(self, session_id: str) -> None:
        assert self.controller is not None
        try:
            await self.controller.switch_session(session_id, "")
        except Exception as exc:  # noqa: BLE001 — a failed switch must be visible
            self._notify("session", f"Could not switch to {session_id}: {exc}")
            return
        await self._adopt_commands()
        self._notify("session", f"Switched to {session_id}")

    async def _cmd_thinking(self, args: str) -> None:
        await self._toggle_block("thinking", args)

    async def _cmd_details(self, args: str) -> None:
        await self._toggle_block("details", args)

    async def _toggle_block(self, name: str, args: str) -> None:
        """Show or hide one optional block; the choice is local to this reader.

        The argument is not guessed at: anything but on/off/toggle answers with
        the usage line, so a typo cannot silently flip what is on screen.
        """
        if self.view is None:
            return
        field, noun = _BLOCKS[name]
        visibility = self.view.transcript.visibility
        value = block_choice(args, current=getattr(visibility, field))
        if value is None:
            self._notify(name, f"Usage: /{name} [on|off|toggle]")
            return
        # The change is applied to the window; the render loop picks it up, the
        # same way every other state change reaches the screen.
        self.view.transcript.set_visibility(replace(visibility, **{field: value}))
        self._notify(name, f"{noun} {'shown' if value else 'hidden'}")

    async def _cmd_attach(self, args: str) -> None:
        """Hold a local image until the next message is sent.

        Reading the file happens here, not in the reducer: an unreadable or
        non-image path is reported to the user and leaves the pending list alone.
        """
        assert self.controller is not None
        target = args.strip()
        if not target:
            self._notify("attach", "Usage: /attach <path> | /attach clear")
            return
        if target == "clear":
            self.controller.clear_attachments()
            self._notify("attach", "Attachments cleared.")
            return
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = Path(self._workspace or Path.cwd()) / path
        try:
            image = load_image(path)
        except FileNotFoundError:
            self._notify("attach", f"No such file: {path}")
            return
        except ValueError as exc:  # a readable file that is not an image
            self._notify("attach", str(exc))
            return
        self.controller.attach(image)
        self._notify("attach", f"Attached {path.name}")

    async def _cmd_approve(self, args: str) -> None:
        parts = args.split()
        if len(parts) not in {1, 2} or (
            len(parts) == 2 and parts[1] not in {"once", "session"}
        ):
            self._notify(
                "permission",
                "Usage: /approve <interaction-id> [once|session]",
            )
            return
        if self.controller is None:
            return
        scope = parts[1] if len(parts) == 2 else "once"
        try:
            await self.controller.respond_permission(parts[0], "allow", scope)
        except Exception as exc:  # noqa: BLE001 — an invalid/stale id must be visible
            self._notify("permission", f"Could not approve request: {exc}")

    async def _cmd_deny(self, args: str) -> None:
        request_id = args.strip()
        if not request_id or any(char.isspace() for char in request_id):
            self._notify("permission", "Usage: /deny <interaction-id>")
            return
        if self.controller is None:
            return
        try:
            await self.controller.respond_permission(request_id, "deny")
        except Exception as exc:  # noqa: BLE001 — an invalid/stale id must be visible
            self._notify("permission", f"Could not deny request: {exc}")

    async def _cmd_answer(self, args: str) -> None:
        parts = args.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            self._notify("interaction", "Usage: /answer <interaction-id> <text>")
            return
        if self.controller is None:
            return
        try:
            await self.controller.respond_user_input(parts[0], parts[1])
        except Exception as exc:  # noqa: BLE001 — an invalid/stale id must be visible
            self._notify("interaction", f"Could not answer request: {exc}")

    async def _cmd_clear_screen(self, _args: str) -> None:
        assert self.controller is not None
        await self.controller.clear_transcript()

    async def _cmd_copy(self, _args: str) -> None:
        from XBotv2.tui.timeline import AssistantEntry

        assert self.controller is not None
        replies = [
            entry
            for entry in self.controller.state.timeline
            if isinstance(entry, AssistantEntry)
        ]
        if not replies:
            self._notify("copy", "There is no reply to copy yet.")
            return
        text = replies[-1].content
        self.copy_to_clipboard(text)
        self._notify("copy", f"Copied {len(text)} characters.")

    async def _cmd_exit(self, _args: str) -> None:
        self.exit()

    async def action_copy_or_quit(self) -> None:
        """Copy an active selection; keep Ctrl-C as the no-selection exit key."""
        from XBotv2.tui.view.composer import ComposerInput

        focused = self.screen.focused
        selected = (
            focused.selected_text
            if isinstance(focused, ComposerInput) and focused.selected_text
            else self.screen.get_selected_text()
        )
        if selected:
            self.copy_to_clipboard(selected)
            self.screen.clear_selection()
            self.notify(
                f"Copied {len(selected)} characters.",
                title="Copied",
                timeout=2,
                markup=False,
            )
            return
        self.exit()

    async def action_interrupt(self) -> None:
        if self.controller is None:
            return
        if self.controller.state.read_only:
            await self._switch_thread("")
            return
        await self.controller.interrupt()

    async def action_older(self) -> None:
        if self.controller is not None:
            await self.controller.page_older()

    async def action_newer(self) -> None:
        if self.controller is not None:
            await self.controller.page_newer()

    async def action_expand_blocks(self) -> None:
        """Fold content open (or closed again) without leaving the composer.

        One binding for the whole transcript: if anything folded is still shut,
        open those; otherwise close what is open. Expanding one block at a time
        from the keyboard would need focus moved into the transcript, which is
        where the reader is not.
        """
        from XBotv2.tui.view.blocks import ClampedBlock

        blocks = [item for item in self.query(ClampedBlock) if item.collapsible]
        if not blocks:
            return
        transcript = self.view.transcript if self.view is not None else None
        following = transcript.reader_at_end if transcript is not None else False
        opening = any(not item.expanded for item in blocks)
        for item in blocks:
            if item.expanded != opening:
                item.toggle()
        if following and transcript is not None:
            container = transcript.container
            self.call_after_refresh(container.scroll_to_tail)

    async def action_palette(self) -> None:
        await self.push_screen(CommandPalette(self.commands), callback=self._command_chosen)

    def _command_chosen(self, value: str | None) -> None:
        """Put the chosen command into the composer, ready for arguments."""
        if not value:
            return
        composer = self.query_one("#composer", Composer)
        composer.load_text(f"{value} ")
        composer.focus()


__all__ = ["TUI_CSS", "TextualViewAdapter", "TuiApp"]


def _panel_title(name: str, count: int) -> str:
    """One-line summary for a disclosure whose rows remain the source of detail."""
    return f"{name} · {count}"
