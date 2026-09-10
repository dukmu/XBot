"""DSH-style XCore application boot lifecycle."""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path

from xcore import Context

from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG
from XBotv2.loader import PluginTree
from XBotv2.loader.runtime import mount_plugin_tree, validate_mounted_tree


async def boot_application(
    *,
    ctx: Context,
    tree: PluginTree,
    plugin_dirs: list[Path | str] | None = None,
) -> Context:
    """Mount and start a host-prepared XCore application context."""
    import_paths: list[str] = []
    for plugin_dir in plugin_dirs or []:
        root = Path(plugin_dir)
        if root.exists():
            sys.path.insert(0, str(root))
            import_paths.append(str(root))

    _ = ctx.state
    ctx.on("dispose", partial(_release_import_paths, import_paths))
    runtime_log = DEFAULT_RUNTIME_LOG
    application_log = runtime_log.bind("application")
    try:
        ctx.set("runtime_log", runtime_log)
        application_log.info(
            "application.boot",
            plugins=[entry.id for entry in tree.entries if not entry.disabled],
        )
        handles = mount_plugin_tree(ctx, tree)
        await ctx.start()
        validate_mounted_tree(handles, nested=ctx.registry.handles())
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
