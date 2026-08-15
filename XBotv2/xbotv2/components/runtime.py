"""Runtime component: static runtime information as XCore services."""

from __future__ import annotations

from typing import Any

from xbotv2.api.paths import RuntimePaths


class RuntimeComponent:
    """Register static runtime info services on the XCore context.

    Services: ``paths`` (RuntimePaths), ``session`` (SessionInfo),
    ``workspace_root`` / ``data_root`` (Path), ``variables``
    (RuntimeVariables), ``runtime`` (RuntimeConfig), ``state_store``
    (CoreStateStore), and ``data_root``.  ``ctx.state`` is XCore-managed
    (created at ``data_dir/state.json`` from the context's ``data_dir``).
    """

    def __init__(
        self,
        *,
        paths: RuntimePaths,
        session: Any,
        workspace_root: Any,
        data_root: Any,
        variables: Any,
        runtime_config: Any,
        state_store: Any,
    ) -> None:
        self._paths = paths
        self._session = session
        self._workspace_root = workspace_root
        self._data_root = data_root
        self._variables = variables
        self._runtime_config = runtime_config
        self._state_store = state_store
        self.name = "xbot.runtime"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("paths", self._paths)
        ctx.set("session", self._session)
        ctx.set("workspace_root", self._workspace_root)
        ctx.set("data_root", self._data_root)
        ctx.set("variables", self._variables)
        ctx.set("runtime", self._runtime_config)
        ctx.set("state_store", self._state_store)
