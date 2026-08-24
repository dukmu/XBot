"""DSH-style XCore application boot lifecycle."""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import xcore
from xcore import Context
from xcore.state import StateService

from XBotv2.loader import PluginTree
from XBotv2.loader.runtime import mount_plugin_tree, validate_mounted_tree


async def boot_application(
    *,
    tree: PluginTree,
    data_dir: Path,
    state_service: StateService | None = None,
    plugin_dirs: list[Path | str] | None = None,
    prepare: Callable[[Context], Any] | None = None,
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
    def release_import_paths() -> None:
        for path in reversed(import_paths):
            try:
                sys.path.remove(path)
            except ValueError:
                pass

    ctx.on("dispose", release_import_paths)
    try:
        if prepare is not None:
            prepared = prepare(ctx)
            if inspect.isawaitable(prepared):
                await prepared
        handles = mount_plugin_tree(ctx, tree)
        await ctx.start()
        validate_mounted_tree(handles)
        return ctx
    except BaseException as startup_error:
        try:
            await ctx.destroy()
        except BaseException as cleanup_error:
            startup_error.add_note(
                "Application cleanup after startup failure also failed: "
                f"{cleanup_error!r}"
            )
        raise


__all__ = ["boot_application"]
