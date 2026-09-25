"""Runtime configuration service (``ctx.config``).

Provides the user context resolved by the config plugin from its tree config
and path-bound runtime config parsing for applications.  Provider
definitions are not read here — they live in the ``llm`` plugin's tree
config and are served through ``ctx.llm``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from pydantic import JsonValue

from XBotv2.config.loader import load_plugin_tree as resolve_plugin_tree
from XBotv2.config.plugin_catalog import update_plugin_config
from XBotv2.config.policy import load_session_policy, patch_session_policy

logger = logging.getLogger("xbotv2.config")

from XBotv2.config.contracts import (
    ConfigPluginConfig,
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
from XBotv2.permissions.contracts import PermissionPolicy


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
        self._extra_plugins = extra_plugins or []
        self._plugin_dirs = plugin_dirs or []
        self._is_subagent = is_subagent
        self._no_plugins = no_plugins

    def user_context(self) -> UserContext:
        """Read the user identity from the live resolved Config declaration.

        Session-scope overlay writes change the resolved tree; returning a
        construction-time capture made this reader stale while ``policy()``
        already re-resolved. Both now read the same source at call time.
        """
        tree = self.load_plugin_tree(self.workspace_root, self.session_id)
        entry = tree.entry("config")
        if entry is None or entry.disabled:
            raise RuntimeError(
                "The resolved plugin tree has no enabled 'config' entry; "
                "cannot resolve the user context"
            )
        return ConfigPluginConfig.model_validate(entry.config).user

    def load_plugin_tree(
        self, workspace: Path, session_id: str, thread_id: str = "agent"
    ) -> PluginTree:
        return resolve_plugin_tree(
            self.paths,
            workspace,
            session_id,
            thread_id=thread_id,
            extra_plugins=self._extra_plugins,
            plugin_dirs=self._plugin_dirs,
            is_subagent=self._is_subagent,
            no_plugins=self._no_plugins,
        )

    def permission_policies(self) -> tuple[PermissionPolicy, ...]:
        """Resolve every applicable permission layer for security meet."""
        stages = (
            (False, False, False, None),
            (True, False, False, None),
            (True, True, False, None),
            (True, True, True, None),
            (True, True, True, self._extra_plugins),
        )
        policies: list[PermissionPolicy] = []
        for include_global, include_workspace, include_session, extra in stages:
            tree = resolve_plugin_tree(
                self.paths,
                self.workspace_root,
                self.session_id,
                extra_plugins=extra,
                plugin_dirs=self._plugin_dirs,
                is_subagent=self._is_subagent,
                no_plugins=self._no_plugins,
                include_global=include_global,
                include_workspace=include_workspace,
                include_session=include_session,
            )
            entry = tree.entry("permissions")
            if entry is None or entry.disabled:
                continue
            policy = PermissionPolicy.model_validate(entry.config)
            if not policies or policy != policies[-1]:
                policies.append(policy)
        return tuple(policies)

    def memory(self) -> str:
        if not self.paths.memory_file.exists():
            return ""
        return self.paths.memory_file.read_text(encoding="utf-8")

    def policy(self) -> PolicySnapshot:
        tree = self.load_plugin_tree(self.workspace_root, self.session_id)
        permissions = _entry_config(tree, "permissions")
        sandbox = _entry_config(tree, "sandbox")
        return PolicySnapshot(
            policy=load_session_policy(self.paths, self.session_id),
            effective_permissions=permissions,
            effective_sandbox=sandbox,
        )

    async def update_policy(self, patch: PatchPolicy) -> PolicySnapshot:
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
                permission_policies=self.permission_policies(),
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
        return update_plugin_config(
            self.paths,
            workspace,
            plugin_id,
            patch,
            session_id,
        )


__all__ = ["ConfigService"]


def _entry_config(tree: PluginTree, plugin_id: str) -> dict[str, JsonValue]:
    """Return one generic plugin declaration without validating its fields.

    A missing or disabled entry is reported: an absent policy entry must not
    look identical to "no overrides", or a typo'd/disabled plugin silently
    degrades to model defaults.
    """
    entry = tree.entry(plugin_id)
    if entry is None:
        logger.info("config.policy.entry.missing plugin=%s", plugin_id)
        return {}
    if entry.disabled:
        logger.info("config.policy.entry.disabled plugin=%s", plugin_id)
        return {}
    return dict(entry.config)
