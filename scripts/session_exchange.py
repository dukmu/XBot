"""Explicit canonical rollout exchange through the normal process root."""

from __future__ import annotations
import argparse
import asyncio
from pathlib import Path
from XBotv2.main import open_runtime
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.agentloop.api.storage import ARCHIVE


async def run(options):
    root = await open_runtime(paths=RuntimePaths.from_data_dir(options.data_dir))
    try:
        archive = root.require(ARCHIVE)
        if options.action == "export":
            path = await archive.export_rollout(
                options.session_id, options.thread_id, options.file
            )
            print(path)
        else:
            identity = await archive.import_rollout(
                options.file, workspace_root=options.workspace
            )
            print(identity)
    finally:
        await root.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="action", required=True)
    export = commands.add_parser("export")
    export.add_argument("session_id")
    export.add_argument("file", type=Path)
    export.add_argument("--thread-id", default="main")
    importing = commands.add_parser("import")
    importing.add_argument("file", type=Path)
    importing.add_argument("--workspace", type=Path)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
