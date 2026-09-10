"""Runtime configuration service (``ctx.config``).

Provides the user context resolved by the config plugin from its tree config
and path-bound runtime config parsing for applications.  Provider
definitions are not read here — they live in the ``llm`` plugin's tree
config and are served through ``ctx.llm``.
"""

from __future__ import annotations

from pathlib import Path
from pydantic import JsonValue

from XBotv2.config.contracts import (
    PatchPluginConfig,
    PatchPolicy,
    PluginConfigCatalog,
    PluginConfigScope,
    PolicySnapshot,
    SettingsPort,
    UserContext,
)
from XBotv2.config.events import POLICY_CHANGED, PolicyChanged
from XBotv2.loader.contracts import PluginTree
from XBotv2.core.runtime_logging import RuntimeLog
from XBotv2.core.paths import RuntimePaths
from XBotv2.application.contracts import ApplicationEventsPort


class ConfigService(SettingsPort):
    """Path-bound configuration reader with a resolved user context."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        session_id: str,
        workspace_root: Path,
        events: ApplicationEventsPort,
        runtime_log: RuntimeLog,
        user_context: UserContext | None = None,
        extra_plugins: list[dict[str, JsonValue]] | None = None,
        plugin_dirs: list[Path | str] | None = None,
        is_subagent: bool = False,
        no_plugins: bool = False,
    ) -> None:
        self.paths = paths
        self.session_id = session_id
        self.workspace_root = workspace_root
        self.events = events
        self._log = runtime_log.bind("config", session_id=session_id)
        self._user_context = user_context or UserContext()
        self._extra_plugins = extra_plugins or []
        self._plugin_dirs = plugin_dirs or []
        self._is_subagent = is_subagent
        self._no_plugins = no_plugins

    def user_context(self) -> UserContext:
        return self._user_context

    def load_plugin_tree(self, workspace: Path, session_id: str) -> PluginTree:
        from XBotv2.config.loader import load_plugin_tree

        return load_plugin_tree(
            self.paths,
            workspace,
            session_id,
            extra_plugins=self._extra_plugins,
            plugin_dirs=self._plugin_dirs,
            is_subagent=self._is_subagent,
            no_plugins=self._no_plugins,
        )

    def memory(self) -> str:
        if not self.paths.memory_file.exists():
            return ""
        return self.paths.memory_file.read_text(encoding="utf-8")

    def policy(self) -> PolicySnapshot:
        from XBotv2.config.policy import load_session_policy

        tree = self.load_plugin_tree(self.workspace_root, self.session_id)
        permissions = _entry_config(tree, "permissions")
        sandbox = _entry_config(tree, "sandbox")
        return PolicySnapshot(
            policy=load_session_policy(self.paths, self.session_id),
            effective_permissions=permissions,
            effective_sandbox=sandbox,
        )

    async def update_policy(self, patch: PatchPolicy) -> PolicySnapshot:
        from XBotv2.config.policy import patch_session_policy

        policy = patch_session_policy(
            paths=self.paths,
            session_id=self.session_id,
            permissions=patch.permissions,
            remove_permissions=patch.remove_permissions,
            sandbox=patch.sandbox,
            remove_sandbox=patch.remove_sandbox,
        )
        tree = self.load_plugin_tree(self.workspace_root, self.session_id)
        permissions = _entry_config(tree, "permissions")
        sandbox = _entry_config(tree, "sandbox")
        await self.events.emit(
            POLICY_CHANGED,
            PolicyChanged(
                policy=policy,
                effective_permissions=permissions,
                effective_sandbox=sandbox,
            ),
        )
        self._log.info(
            "config.policy.updated",
            permission_fields=sorted((patch.permissions or {}).keys()),
            removed_permissions=sorted(patch.remove_permissions),
            sandbox_fields=sorted((patch.sandbox or {}).keys()),
            removed_sandbox=sorted(patch.remove_sandbox),
        )
        return PolicySnapshot(
            policy=policy,
            effective_permissions=permissions,
            effective_sandbox=sandbox,
        )

    def plugin_config_catalog(
        self,
        workspace: Path,
        scope: PluginConfigScope,
        session_id: str,
    ) -> PluginConfigCatalog:
        from XBotv2.config.plugin_catalog import plugin_config_catalog

        return plugin_config_catalog(self.paths, workspace, scope, session_id)

    def update_plugin_config(
        self,
        workspace: Path,
        plugin_id: str,
        patch: PatchPluginConfig,
        session_id: str,
    ) -> PluginConfigCatalog:
        from XBotv2.config.plugin_catalog import update_plugin_config

        return update_plugin_config(
            self.paths,
            workspace,
            plugin_id,
            patch,
            session_id,
        )


__all__ = ["ConfigService"]


def _entry_config(tree: PluginTree, plugin_id: str) -> dict[str, JsonValue]:
    """Return one generic plugin declaration without validating its fields."""
    entry = tree.entry(plugin_id)
    return dict(entry.config) if entry is not None and not entry.disabled else {}
