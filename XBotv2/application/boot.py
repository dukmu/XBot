"""DSH-style XCore application boot lifecycle."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Any

import xcore
from xcore import Context
from xcore.state import StateService

from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG
from XBotv2.loader import PluginTree
from XBotv2.loader.runtime import mount_plugin_tree, validate_mounted_tree


async def boot_application(
    *,
    tree: PluginTree,
    data_dir: Path,
    state_service: StateService | None = None,
    plugin_dirs: list[Path | str] | None = None,
    services: Mapping[str, object] | None = None,
) -> Context:
    """Create, prepare, mount, and start one XCore application context."""
    import_paths: list[str] = []
    for plugin_dir in plugin_dirs or []:
        root = Path(plugin_dir)
        if root.exists():
            sys.path.insert(0, str(root))
            import_paths.append(str(root))

    ctx = xcore.Context(data_dir=data_dir, state_service=state_service)
    _ = ctx.state
    ctx.on("dispose", partial(_release_import_paths, import_paths))
    runtime_log = DEFAULT_RUNTIME_LOG
    application_log = runtime_log.bind("application")
    try:
        supplied = dict(services or {})
        ctx.set("runtime_log", runtime_log)
        application_log.info(
            "application.boot",
            data_dir=str(data_dir),
            plugins=[entry.id for entry in tree.entries if not entry.disabled],
            supplied_services=sorted(supplied),
        )
        for name, value in supplied.items():
            ctx.set(name, value)
        handles = mount_plugin_tree(ctx, tree)
        await ctx.start()
        validate_mounted_tree(handles)
        application_log.info(
            "application.booted",
            plugins_running=len(handles),
        )
        return ctx
    except BaseException as startup_error:
        application_log.error(
            "application.boot.failed",
            error_type=type(startup_error).__name__,
        )
        try:
            await ctx.destroy()
        except BaseException as cleanup_error:
            startup_error.add_note(
                "Application cleanup after startup failure also failed: "
                f"{cleanup_error!r}"
            )
        raise


def _release_import_paths(paths: list[str]) -> None:
    for path in reversed(paths):
        try:
            sys.path.remove(path)
        except ValueError:
            pass


__all__ = ["boot_application"]
