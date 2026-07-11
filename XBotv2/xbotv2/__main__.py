"""XBotv2 — Plugin-extensible AI agent runtime.

Usage:
    python main.py                              # Interactive terminal mode
    python main.py --mode tui                   # Textual TUI (auto-spawns HTTP server)
    python main.py --mode server                # HTTP/SSE server (default 127.0.0.1:4096)
    python main.py attach <url>                 # Connect TUI to an existing HTTP server
    python main.py --mode once "prompt"         # Single-shot query

Options:
    --data-dir PATH     Data directory (default: data)
    --provider NAME     Provider config to use (default: default)
    --workspace PATH    Workspace root (default: current directory)
    --mode MODE         Run mode: server, terminal, tui, curses, once, attach
    --bind HOST         Server bind address (must be 127.0.0.1 in v1)
    --port PORT         Server port (default: 4096)
    --server URL        Use a specific HTTP server URL (TUI mode only)
    --no-plugins        Disable plugin discovery for pure-core runs
    --help              Show this help
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="XBotv2 — Plugin-extensible AI agent runtime",
        prog="xbotv2",
    )
    parser.add_argument(
        "--data-dir", default="data", help="Data directory (default: data)"
    )
    parser.add_argument(
        "--provider", default="default", help="Provider config to use"
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Session ID to resume/connect. Defaults to a new UUID session.",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace root (default: current directory)",
    )
    parser.add_argument(
        "--thread",
        default="agent",
        help="Thread ID within the session (default: agent)",
    )
    parser.add_argument(
        "--no-plugins",
        action="store_true",
        help="Disable plugin discovery for pure-core runs",
    )
    parser.add_argument(
        "--mode",
        default="terminal",
        choices=["server", "terminal", "tui", "curses", "once"],
        help="Run mode (default: terminal)",
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Server bind address (must be 127.0.0.1 in v1; see doc §10.5.7)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=4096,
        help="Server port (default: 4096, matches OpenCode)",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="TUI mode only: connect to a specific HTTP server URL instead of auto-spawning",
    )
    parser.add_argument(
        "--uds",
        default=None,
        help="Unix domain socket path (auto-generated when TUI spawns server without --server)",
    )
    parser.add_argument(
        "prompt", nargs="?", default=None, help="Single-shot prompt (for --mode once) or attach URL"
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("XBOTV2_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO, or $XBOTV2_LOG_LEVEL)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Explicit log file path. Overrides --data-dir/logs/xbotv2.log "
        "and $XBOTV2_LOG_FILE.",
    )
    args = parser.parse_args()

    from xbotv2.core.logging_config import setup_logging

    setup_logging(
        data_dir=args.data_dir,
        level=args.log_level,
        log_file=args.log_file,
    )

    if args.prompt and args.prompt.startswith("http"):
        # `python main.py attach http://...` style: first arg looks like a URL
        return _run_attach(args, args.prompt)

    if args.mode == "server":
        _run_server(args)
    elif args.mode == "tui":
        _run_tui(args)
    elif args.mode == "curses":
        _run_curses(args)
    elif args.mode == "terminal":
        _run_terminal(args)
    elif args.mode == "once":
        _run_once(args)
    else:
        parser.print_help()


def _run_server(args) -> None:
    """Run the HTTP/SSE server with uvicorn."""

    if not getattr(args, "uds", None):
        logging.getLogger("xbotv2").info(
            "starting server mode data_dir=%s workspace=%s provider=%s bind=%s port=%s",
            args.data_dir, _workspace_root(args), args.provider, args.bind, args.port,
        )
        if args.bind != "127.0.0.1":
            print(f"Error: --bind {args.bind} is not supported in v1; use 127.0.0.1 only (see docsv2 §10.5.7).", file=sys.stderr)
            sys.exit(2)

    try:
        import uvicorn
    except ImportError as exc:
        print(f"Error: uvicorn not installed: {exc}", file=sys.stderr)
        sys.exit(2)

    from xbotv2.protocol.http_server import create_app

    app = create_app(
        provider_name=args.provider,
        data_dir=args.data_dir,
        workspace_root=str(_workspace_root(args)),
        no_plugins=args.no_plugins,
    )
    uds = getattr(args, "uds", None)
    if uds:
        uvicorn.run(app, uds=uds, log_level="warning", ws="none")
    else:
        uvicorn.run(
            app, host=args.bind, port=args.port, log_level="warning", ws="none"
        )


def _run_tui(args) -> None:
    """Run the Textual TUI; auto-spawn server on Unix socket unless --server is given."""

    logging.getLogger("xbotv2").info(
        "starting tui mode data_dir=%s workspace=%s provider=%s server=%s log_file=%s log_level=%s",
        args.data_dir, _workspace_root(args), args.provider, args.server, args.log_file, args.log_level,
    )

    server_url = args.server
    uds_path: str | None = getattr(args, "uds", None)
    spawned_server: subprocess.Popen | None = None

    if server_url is None:
        if uds_path is None:
            uds_path = f"/tmp/xbotv2-{os.getpid()}.sock"
        server_url = "http://localhost"
        args.uds = uds_path
        spawned_server = _spawn_server(args)
        if not _wait_for_health(server_url, timeout=15.0, uds_path=uds_path):
            print(f"Error: spawned server at {uds_path} did not become healthy", file=sys.stderr)
            if spawned_server is not None:
                if spawned_server.poll() is not None:
                    _, err = spawned_server.communicate(timeout=1)
                    if err:
                        print("Server stderr:", file=sys.stderr)
                        for line in err.decode("utf-8", errors="replace").splitlines()[-20:]:
                            print(f"  {line}", file=sys.stderr)
                spawned_server.terminate()
                spawned_server.wait()
            _cleanup_socket(uds_path)
            sys.exit(2)

    from xbotv2.tui.textual_client import TextualTuiClient

    client = TextualTuiClient(
        data_dir=args.data_dir,
        provider_name=args.provider,
        session_id=getattr(args, "session", None),
        thread_id=getattr(args, "thread", "agent"),
        workspace_root=str(_workspace_root(args)),
        no_plugins=args.no_plugins,
        base_url=server_url,
        uds_path=uds_path,
    )
    try:
        asyncio.run(client.run())
    finally:
        if spawned_server is not None:
            spawned_server.terminate()
            try:
                spawned_server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                spawned_server.kill()
            _cleanup_socket(uds_path)


def _cleanup_socket(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _run_attach(args, url: str) -> None:
    """Connect the TUI to a running HTTP server at ``url``."""

    from xbotv2.tui.textual_client import TextualTuiClient

    client = TextualTuiClient(
        data_dir=args.data_dir,
        provider_name=args.provider,
        session_id=getattr(args, "session", None),
        thread_id=getattr(args, "thread", "agent"),
        workspace_root=str(_workspace_root(args)),
        no_plugins=args.no_plugins,
        base_url=url,
    )
    asyncio.run(client.run())


def _spawn_server(args) -> subprocess.Popen:
    """Launch a server as a subprocess and return the Popen handle."""

    env = os.environ.copy()
    env["PYTHONPATH"] = _spawn_pythonpath()
    cmd = [
        sys.executable, str(Path(__file__).resolve()),
        "--data-dir", args.data_dir,
        "--provider", args.provider,
        "--workspace", str(_workspace_root(args)),
        "--mode", "server",
        "--log-level", args.log_level,
    ]
    uds = getattr(args, "uds", None)
    if uds:
        cmd.extend(["--uds", uds])
    else:
        cmd.extend(["--bind", args.bind, "--port", str(args.port)])
    if args.log_file:
        cmd.extend(["--log-file", args.log_file])
    if args.no_plugins:
        cmd.append("--no-plugins")
    logging.getLogger("xbotv2").info("spawning server subprocess cmd=%s", cmd)
    return subprocess.Popen(
        cmd, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _wait_for_health(url: str, *, timeout: float, uds_path: str | None = None) -> bool:
    """Poll GET /health until 200 or timeout."""

    import httpx
    import time

    client = httpx.Client(transport=httpx.HTTPTransport(uds=uds_path)) if uds_path else httpx.Client()
    end = time.time() + timeout
    while time.time() < end:
        try:
            response = client.get(f"{url}/health", timeout=1.0)
            if response.status_code == 200:
                client.close()
                return True
        except Exception:
            pass
        time.sleep(0.1)
    client.close()
    return False


def _spawn_pythonpath() -> str:
    paths = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    return os.pathsep.join(paths)


def _workspace_root(args) -> Path:
    return Path(getattr(args, "workspace", None) or Path.cwd()).resolve()


def _run_terminal(args):
    """Run interactive terminal mode using direct engine."""
    asyncio.run(_terminal_loop(args))


def _run_curses(args):
    """Run the legacy curses TUI over the HTTP server boundary."""
    from xbotv2.tui.client import CursesTuiClient

    client = CursesTuiClient(
        data_dir=args.data_dir,
        provider_name=args.provider,
        session_id=getattr(args, "session", None),
        thread_id=getattr(args, "thread", "agent"),
        workspace_root=str(_workspace_root(args)),
        no_plugins=args.no_plugins,
        server_url=f"http://{args.bind}:{args.port}",
    )
    asyncio.run(client.run())


async def _terminal_loop(args):
    """Direct engine terminal session — reads from stdin, prints responses."""
    from xbotv2.core.bootstrap import bootstrap

    data_dir = Path(args.data_dir).resolve()

    print(f"XBotv2 [{args.provider}] workspace={_workspace_root(args)} — type /quit to exit\n")

    try:
        engine = await bootstrap(
            config_dir=str(data_dir),
            provider_name=args.provider,
            session_id=getattr(args, "session", None),
            thread_id=getattr(args, "thread", "agent"),
            workspace_root=str(_workspace_root(args)),
            plugin_dirs=[] if args.no_plugins else None,
        )
        await engine.start_session()
    except Exception as exc:
        print(f"Error starting engine: {exc}")
        return

    while True:
        try:
            user_input = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input.strip():
            continue
        if user_input.strip() == "/quit":
            print("Goodbye.")
            break

        try:
            async for event in engine.run_turn(user_input):
                etype = event.get("type", "")
                data = event.get("data", {})

                if etype == "assistant_message":
                    content = data.get("content", "")
                    tool_calls = data.get("tool_calls")
                    if content:
                        print(content)
                    if tool_calls:
                        print(f"\n[tool calls: {len(tool_calls)}]")
                elif etype == "tool_result":
                    tc_id = data.get("tool_call_id", "")
                    content = data.get("content", "")
                    print(f"  [{tc_id}]: {content[:200]}")
                elif etype == "client_message":
                    print(f"\n[message] {data.get('message', '')}")
                elif etype == "permission_request":
                    print(f"\n[approval required] {data.get('reason', '')}")
                elif etype == "permission_denied":
                    print(f"\n[permission denied] {data.get('reason', '')}")
                elif etype == "user_input_required":
                    print(f"\n[question] {data.get('question', '')}")
                elif etype == "error":
                    print(f"\nError: {data.get('message', 'unknown')}")
        except Exception as exc:
            print(f"\nError: {exc}")

    await engine.close_session()


def _run_once(args):
    """Run a single prompt and exit."""
    from xbotv2.core.bootstrap import bootstrap

    if not args.prompt:
        print("Error: --mode once requires a prompt argument")
        sys.exit(1)

    data_dir = Path(args.data_dir).resolve()

    async def single_shot():
        engine = await bootstrap(
            config_dir=str(data_dir),
            provider_name=args.provider,
            session_id=getattr(args, "session", None),
            thread_id=getattr(args, "thread", "agent"),
            workspace_root=str(_workspace_root(args)),
            plugin_dirs=[] if args.no_plugins else None,
        )
        await engine.start_session()

        async for event in engine.run_turn(args.prompt):
            etype = event.get("type", "")
            data = event.get("data", {})

            if etype == "assistant_message":
                content = data.get("content", "")
                if content:
                    print(content)
            elif etype == "tool_result":
                tc_id = data.get("tool_call_id", "")
                content = data.get("content", "")
                print(f"\n[{tc_id}]: {content[:300]}")
            elif etype == "client_message":
                print(f"\n[message] {data.get('message', '')}")
            elif etype == "permission_request":
                print(f"\n[approval required] {data.get('reason', '')}")
            elif etype == "permission_denied":
                print(f"\n[permission denied] {data.get('reason', '')}")
            elif etype == "user_input_required":
                print(f"\n[question] {data.get('question', '')}")
            elif etype == "error":
                print(f"\nError: {data.get('message', 'unknown')}")

        await engine.close_session()

    asyncio.run(single_shot())


if __name__ == "__main__":
    main()
