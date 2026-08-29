"""Command-line entry point for the XBot runtime and clients."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from XBotv2.core.paths import RuntimePaths

__version__ = "0.2.0"


_COMMANDS = {"serve", "tui", "web", "once", "acp"}
_WEB_STATIC_ROOT = Path(__file__).resolve().parent / "web_dist"
# $HOME/.local/state/xbotv2 is the default state dir on Linux
# on Windows, it will be %LOCALAPPDATA%\xbotv2\state
if sys.platform == "win32":
    _DEFAULT_STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "xbotv2" / "state"
else:
    _DEFAULT_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "xbotv2" / "state"


def _env(name: str, default: str | None = None) -> str | None:
    """Read one XBot environment setting."""
    return os.environ.get(f"XBOT_{name}", default)


def _default_data_dir() -> str:
    """Runtime data root: ``~/.xbot`` (XDG-style home directory).

    Sessions and memory live here by default; ``XBOT_DATA_DIR`` (or
    ``--data-dir``) overrides.  The initial global user tree
    (``config/plugins.yaml``) is written into this directory on first run;
    provider definitions and user context are plugin tree config.
    """
    return str(Path(os.environ.get("XBOT_HOME") or Path.home() / ".xbot"))


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--data-dir", default=_env("DATA_DIR", _default_data_dir()),
        help="runtime data directory (env: XBOT_DATA_DIR)",
    )
    parser.add_argument(
        "--provider", default=_env("PROVIDER", "default"),
        help="provider configuration name (env: XBOT_PROVIDER)",
    )
    parser.add_argument("--session", default=_env("SESSION"), help="session to resume")
    parser.add_argument(
        "--workspace", default=None,
        help="Agent workspace; defaults to the current directory",
    )
    parser.add_argument("--thread", default=_env("THREAD", "agent"), help="thread ID")
    parser.add_argument("--agent", default=_env("AGENT"), help="Agent definition name")
    parser.add_argument(
        "--no-plugins",
        action="store_true",
        default=str(_env("NO_PLUGINS", "")).lower() in {"1", "true", "yes"},
        help="disable optional Agent capabilities and external plugins",
    )
    parser.add_argument(
        "--log-level",
        default=_env("LOG_LEVEL", "INFO"),
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="runtime log level (env: XBOT_LOG_LEVEL)",
    )
    parser.add_argument(
        "--log-file", default=_env("LOG_FILE"), help="explicit log path"
    )
    return parser


def _build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="xbot",
        description="Readable plugin-extensible client/server Agent runtime",
    )
    parser.add_argument(
        "--version", action="version", version=f"xbot {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    tui = commands.add_parser(
        "tui", parents=[common], help="run the Textual client"
    )
    tui.add_argument(
        "--server", default=_env("SERVER"), help="connect to an existing API URL"
    )
    tui.add_argument(
        "--uds", default=_env("UDS"), help="socket for the auto-started server"
    )
    tui.add_argument(
        "--bind", default=_env("BIND", "127.0.0.1"), help=argparse.SUPPRESS
    )
    tui.add_argument(
        "--port", type=int, default=_env("PORT", "4096"), help=argparse.SUPPRESS
    )

    serve = commands.add_parser(
        "serve",
        parents=[common],
        help="run the HTTP/SSE API server",
    )
    serve.add_argument(
        "--bind", default=_env("BIND", "127.0.0.1"), help="API bind address"
    )
    serve.add_argument(
        "--port", type=int, default=_env("PORT", "4096"), help="API port"
    )
    serve.add_argument("--uds", default=_env("UDS"), help="serve over a Unix socket")
    serve.set_defaults(command="serve")

    web = commands.add_parser(
        "web", parents=[common], help="run the compiled Web workbench"
    )
    web_api = web.add_mutually_exclusive_group()
    web_api.add_argument(
        "--server", default=_env("SERVER"), help="connect to an existing API URL"
    )
    web_api.add_argument(
        "--uds", default=_env("UDS"), help="socket for the auto-started server"
    )
    web.add_argument(
        "--web-bind",
        default=_env("WEB_BIND", "127.0.0.1"),
        help="Web bind address",
    )
    web.add_argument(
        "--web-port", type=int, default=_env("WEB_PORT", "5173"), help="Web port"
    )
    web.add_argument("--no-open", action="store_true", help="do not open a browser")

    once = commands.add_parser(
        "once", parents=[common], help="run one prompt and exit"
    )
    once.add_argument("prompt", help="prompt to run")

    acp = commands.add_parser(
        "acp", parents=[common], help="run the Agent Client Protocol adapter"
    )
    acp.set_defaults(command="acp")
    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    """Default invocations without a named command to TUI mode."""
    if argv in (["-h"], ["--help"], ["--version"]):
        return argv
    if not argv or argv[0] not in _COMMANDS:
        return ["tui", *argv]
    return argv


def _parse_args(
    argv: list[str] | None = None,
) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = _build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_normalize_argv(raw_args))
    return parser, args


def main(argv: list[str] | None = None):
    parser, args = _parse_args(argv)

    from XBotv2.application.logging import setup_logging

    setup_logging(
        data_dir=args.data_dir,
        level=args.log_level,
        log_file=args.log_file,
    )

    try:
        if args.command == "serve":
            _run_server(args)
        elif args.command == "tui":
            _run_tui(args)
        elif args.command == "web":
            _run_web(args)
        elif args.command == "once":
            asyncio.run(_run_once(args))
        elif args.command == "acp":
            from XBotv2.acp_plugin.server import run_acp

            asyncio.run(run_acp(
                data_dir=args.data_dir,
                provider_name=args.provider,
                no_plugins=args.no_plugins,
                selected_agent=args.agent,
            ))
        else:
            parser.print_help()
    except ValueError as exc:
        parser.exit(2, f"Error: {exc}\n")


def _run_server(args) -> None:
    """Run the HTTP/SSE server with uvicorn."""

    if not getattr(args, "uds", None):
        logging.getLogger("xbotv2").info(
            "starting server mode data_dir=%s workspace=%s provider=%s bind=%s port=%s",
            args.data_dir, _workspace_root(args), args.provider, args.bind, args.port,
        )
        if args.bind != "127.0.0.1":
            print(
                f"Error: --bind {args.bind} is not supported in v1; "
                "use 127.0.0.1 only (see docs section 10.5.7).",
                file=sys.stderr,
            )
            sys.exit(2)

    try:
        import uvicorn
    except ImportError as exc:
        print(f"Error: uvicorn not installed: {exc}", file=sys.stderr)
        sys.exit(2)

    paths = RuntimePaths.from_data_dir(args.data_dir)
    workspace_root = str(_workspace_root(args))
    from XBotv2.application.server import start_server_application

    root_ctx = asyncio.run(start_server_application(
        paths=paths,
        provider_name=args.provider,
        workspace_root=workspace_root,
        no_plugins=args.no_plugins,
    ))
    app = root_ctx.server
    uds = getattr(args, "uds", None)
    try:
        if uds:
            uds_path = Path(uds).expanduser()
            uds_path.parent.mkdir(parents=True, exist_ok=True)
            uvicorn.run(
                app,
                uds=str(uds_path),
                log_level="warning",
                ws="none",
            )
        else:
            uvicorn.run(
                app, host=args.bind, port=args.port, log_level="warning", ws="none"
            )
    finally:
        asyncio.run(root_ctx.stop())


def _run_tui(args) -> None:
    """Run the Textual TUI; auto-spawn server on Unix socket unless --server is given."""

    logging.getLogger("xbotv2").info(
        "starting tui mode data_dir=%s workspace=%s provider=%s server=%s log_file=%s log_level=%s",
        args.data_dir,
        _workspace_root(args),
        args.provider,
        args.server,
        args.log_file,
        args.log_level,
    )

    server_url, uds_path, spawned_server = _local_server(args, "xbotv2")

    from XBotv2.tui.textual_client import TextualTuiClient

    client = TextualTuiClient(
        session_id=getattr(args, "session", None),
        thread_id=getattr(args, "thread", "agent"),
        agent=getattr(args, "agent", None),
        workspace_root=str(_workspace_root(args)),
        session_mode="resume" if getattr(args, "session", None) else "new",
        base_url=server_url,
        uds_path=uds_path,
    )
    try:
        asyncio.run(client.run())
    finally:
        if spawned_server is not None:
            _stop_process(spawned_server)
            _cleanup_socket(uds_path)


def _run_web(args) -> None:
    """Serve the compiled Web client and proxy its API requests."""
    static_root = _WEB_STATIC_ROOT
    if not (static_root / "index.html").is_file() or not (
        static_root / "assets"
    ).is_dir():
        raise SystemExit(
            f"Error: compiled Web client not found at {static_root}; "
            "run `npm run build` in XBotv2/web"
        )
    if args.web_bind != "127.0.0.1":
        raise SystemExit("Error: Web mode only supports --web-bind 127.0.0.1")

    api_url, uds_path, spawned_server = _local_server(args, "xbotv2-web")

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Error: uvicorn is required for `xbotv2 web`") from exc

    from XBotv2.web_server import create_web_app

    web_url = f"http://{args.web_bind}:{args.web_port}"
    app = create_web_app(static_root, api_url=api_url, uds_path=uds_path)
    try:
        print(f"XBot Web: {web_url}")
        if not args.no_open:
            webbrowser.open(web_url)
        uvicorn.run(
            app,
            host=args.web_bind,
            port=args.web_port,
            log_level="warning",
            ws="none",
        )
    finally:
        if spawned_server is not None:
            _stop_process(spawned_server)
            _cleanup_socket(uds_path)


def _cleanup_socket(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _local_server(
    args: argparse.Namespace,
    socket_prefix: str,
) -> tuple[str, str | None, subprocess.Popen | None]:
    if args.server is not None:
        return args.server, args.uds, None
    uds_path = args.uds or (
        f"{_DEFAULT_STATE_DIR}/{socket_prefix}-{os.getpid()}.sock"
    )
    args.uds = uds_path
    process = _spawn_server(args)
    if _wait_for_health("http://localhost", timeout=15.0, uds_path=uds_path):
        return "http://localhost", uds_path, process
    if process.poll() is not None:
        _, error = process.communicate(timeout=1)
        if error:
            print(error.decode("utf-8", errors="replace"), file=sys.stderr)
    _stop_process(process)
    _cleanup_socket(uds_path)
    raise SystemExit(
        f"Error: spawned server at {uds_path} did not become healthy"
    )


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _spawn_server(args) -> subprocess.Popen:
    """Launch a server as a subprocess and return the Popen handle."""

    env = os.environ.copy()
    env["PYTHONPATH"] = _spawn_pythonpath()
    cmd = [
        sys.executable, "-m", "XBotv2.main",
        "serve",
        "--data-dir", args.data_dir,
        "--provider", args.provider,
        "--workspace", str(_workspace_root(args)),
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


def _wait_for_health(
    url: str,
    *,
    timeout: float,
    uds_path: str | None = None,
    path: str = "/health",
) -> bool:
    """Poll an HTTP endpoint until it returns 200 or timeout expires."""

    import httpx
    import time

    client = (
        httpx.Client(transport=httpx.HTTPTransport(uds=uds_path))
        if uds_path
        else httpx.Client()
    )
    end = time.time() + timeout
    while time.time() < end:
        try:
            response = client.get(f"{url}{path}", timeout=1.0)
            if response.status_code == 200:
                client.close()
                return True
        except Exception:
            pass
        time.sleep(0.1)
    client.close()
    return False


def _spawn_pythonpath() -> str:
    """Subprocess import path: repo root + XCore, never XBotv2/.

    XBotv2 is a package root (``XBotv2.<pkg>``); putting the directory itself
    on PYTHONPATH would shadow the installed ``mcp``/``acp`` SDKs with the
    XBot plugin packages of the same names.
    """
    root = Path(__file__).resolve().parent.parent
    paths = [str(root), str(root / "XCore")]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    return os.pathsep.join(paths)


def _workspace_root(args) -> Path:
    return Path(getattr(args, "workspace", None) or Path.cwd()).resolve()


async def _run_once(args):
    """Run a single prompt and exit."""
    from XBotv2.application.app import start_application
    from XBotv2.application.host import mounted_application
    from XBotv2.session.runtime import SessionRuntime

    context = await start_application(
        paths=RuntimePaths.from_data_dir(args.data_dir),
        provider_name=args.provider,
        session_id=getattr(args, "session", None),
        thread_id=getattr(args, "thread", "agent"),
        workspace_root=str(_workspace_root(args)),
        no_plugins=args.no_plugins,
        selected_agent=getattr(args, "agent", None),
        interactive=False,
    )
    application = mounted_application(context)
    engine = application.driver
    await engine.start_session()
    session = context.loop_state.session
    runtime = SessionRuntime(
        session_id=session.session_id,
        thread_id=session.thread_id,
        provider_name=args.provider,
        paths=RuntimePaths.from_data_dir(args.data_dir),
        workspace_root=str(_workspace_root(args)),
        no_plugins=args.no_plugins,
        application=application,
        engine=engine,
        interactive=False,
    )
    try:
        async for event in runtime.stream_message(args.prompt, "once"):
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
            elif etype == "permission_denied":
                print(f"\n[permission denied] {data.get('reason', '')}")
            elif etype == "error":
                print(f"\nError: {data.get('message', 'unknown')}")
    finally:
        await runtime.close()


if __name__ == "__main__":
    main()
