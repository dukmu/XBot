"""DSH-style XCore application boot lifecycle."""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import xcore

from XBotv2.loader import PluginTree
from XBotv2.loader.runtime import LoaderComponent


async def boot_application(
    *,
    tree: PluginTree,
    data_dir: Path,
    plugin_dirs: list[Path | str] | None = None,
    prepare: Callable[[Any], Any] | None = None,
) -> Any:
    """Create, prepare, mount, and settle one XCore application context."""
    import_paths: list[str] = []
    for plugin_dir in plugin_dirs or []:
        root = Path(plugin_dir)
        if root.exists():
            sys.path.insert(0, str(root))
            import_paths.append(str(root))

    ctx = xcore.Context(data_dir=data_dir)
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
        ctx.plugin(LoaderComponent(tree))
        await ctx.start()
        await ctx.loader.load()
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
