"""Runtime component: static runtime information as XCore services."""

from __future__ import annotations

from typing import Any

from api.paths import RuntimePaths
from api.variables import RuntimeVariables


class RuntimeComponent:
    """Register static runtime info services on the XCore context.

    Services: ``paths`` (RuntimePaths), ``session`` (SessionInfo),
    ``workspace_root`` / ``data_root`` (Path), ``variables``
    (RuntimeVariables), ``runtime`` (RuntimeConfig), and ``state_store``
    (CoreStateStore).  ``ctx.state`` is XCore-managed (created at
    ``data_dir/state.json`` from the context's ``data_dir``).
    """

    name = "xbot.runtime"

    def apply(self, ctx: Any, config: Any = None) -> None:
        paths: RuntimePaths = config["paths"]
        session: Any = config["session"]
        workspace_root: Any = config["workspace_root"]
        data_root: Any = config["data_root"]
        runtime_config: Any = config["runtime_config"]
        state_store: Any = config["state_store"]
        variables = RuntimeVariables.for_thread(
            paths, workspace_root, state_store.paths
        )
        ctx.set("paths", paths)
        ctx.set("session", session)
        ctx.set("workspace_root", workspace_root)
        ctx.set("data_root", data_root)
        ctx.set("variables", variables)
        ctx.set("runtime", runtime_config)
        ctx.set("state_store", state_store)


plugin = RuntimeComponent()
