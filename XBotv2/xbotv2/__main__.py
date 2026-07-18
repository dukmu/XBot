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

from xbotv2 import __version__
from xbotv2.api.paths import RuntimePaths


_COMMAND_ALIASES = {"server": "serve"}
_COMMANDS = {"serve", "server", "tui", "web", "once", "terminal"}
_WEB_STATIC_ROOT = Path(__file__).resolve().parent / "web_dist"


def _env(name: str, default: str | None = None) -> str | None:
    """Read an XBOT setting, accepting the old XBOTV2 prefix as fallback."""
    return os.environ.get(f"XBOT_{name}", os.environ.get(f"XBOTV2_{name}", default))


def _default_data_dir() -> str:
    source_data = Path(__file__).resolve().parents[1] / "data"
    return str(source_data if source_data.is_dir() else Path(sys.prefix) / "data")


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
        help="disable plugin discovery",
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

    terminal = commands.add_parser(
        "terminal", parents=[common], help="run the basic interactive terminal"
    )
    terminal.set_defaults(command="terminal")

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
        aliases=["server"],
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
    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    """Translate the legacy --mode form and default to terminal mode."""
    if argv in (["-h"], ["--help"], ["--version"]):
        return argv
    if "--mode" in argv:
        index = argv.index("--mode")
        if index + 1 >= len(argv):
            return argv
        command = _COMMAND_ALIASES.get(argv[index + 1], argv[index + 1])
        return [command, *argv[:index], *argv[index + 2:]]
    if not argv or argv[0] not in _COMMANDS:
        return ["terminal", *argv]
    argv[0] = _COMMAND_ALIASES.get(argv[0], argv[0])
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

    from xbotv2.core.logging_config import setup_logging

    setup_logging(
        data_dir=args.data_dir,
        level=args.log_level,
        log_file=args.log_file,
    )

    if not (
        args.command in {"tui", "web"}
        and getattr(args, "server", None)
    ):
        from xbotv2.config.loader import load_provider_config

        try:
            load_provider_config(
                RuntimePaths.from_data_dir(args.data_dir),
                args.provider,
            )
        except ValueError as exc:
            parser.exit(2, f"Error: {exc}\n")

    if args.command == "serve":
        _run_server(args)
    elif args.command == "tui":
        _run_tui(args)
    elif args.command == "web":
        _run_web(args)
    elif args.command == "terminal":
        asyncio.run(_terminal_loop(args))
    elif args.command == "once":
        asyncio.run(_run_once(args))
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
            print(
                f"Error: --bind {args.bind} is not supported in v1; "
                "use 127.0.0.1 only (see docsv2 section 10.5.7).",
                file=sys.stderr,
            )
            sys.exit(2)

    try:
        import uvicorn
    except ImportError as exc:
        print(f"Error: uvicorn not installed: {exc}", file=sys.stderr)
        sys.exit(2)

    from xbotv2.protocol.http_server import create_app

    app = create_app(
        paths=RuntimePaths.from_data_dir(args.data_dir),
        provider_name=args.provider,
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
        args.data_dir,
        _workspace_root(args),
        args.provider,
        args.server,
        args.log_file,
        args.log_level,
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
            spawned_server.terminate()
            try:
                spawned_server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                spawned_server.kill()
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

    api_url = args.server or "http://localhost"
    uds_path = args.uds
    spawned_server: subprocess.Popen | None = None
    if args.server is None:
        if uds_path is None:
            uds_path = f"/tmp/xbotv2-web-{os.getpid()}.sock"
        args.uds = uds_path
        spawned_server = _spawn_server(args)
        if not _wait_for_health(api_url, timeout=15.0, uds_path=uds_path):
            if spawned_server.poll() is not None:
                _, error = spawned_server.communicate(timeout=1)
                if error:
                    print(error.decode("utf-8", errors="replace"), file=sys.stderr)
            _stop_process(spawned_server)
            _cleanup_socket(uds_path)
            raise SystemExit(
                f"Error: spawned server at {uds_path} did not become healthy"
            )

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Error: uvicorn is required for `xbotv2 web`") from exc

    from xbotv2.web_server import create_web_app

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
        sys.executable, str(Path(__file__).resolve()),
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
    paths = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    return os.pathsep.join(paths)


def _workspace_root(args) -> Path:
    return Path(getattr(args, "workspace", None) or Path.cwd()).resolve()


async def _terminal_loop(args):
    """Direct engine terminal session — reads from stdin, prints responses."""
    from xbotv2.core.bootstrap import bootstrap

    print(f"XBotv2 [{args.provider}] workspace={_workspace_root(args)} — type /quit to exit\n")

    try:
        engine = await bootstrap(
            paths=RuntimePaths.from_data_dir(args.data_dir),
            provider_name=args.provider,
            session_id=getattr(args, "session", None),
            thread_id=getattr(args, "thread", "agent"),
            workspace_root=str(_workspace_root(args)),
            plugin_dirs=[] if args.no_plugins else None,
            selected_agent=getattr(args, "agent", None),
        )
        await engine.start_session()
        engine.set_client_event_sink(_terminal_interaction)
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


async def _terminal_interaction(
    event: dict,
    *,
    timeout_seconds: float | None = None,
    tool_call_id: str = "",
) -> dict:
    """Resolve one live Engine interaction through stdin."""
    del timeout_seconds, tool_call_id
    event_type = str(event.get("type") or "")
    data = event.get("data") or {}
    request_id = str(data.get("request_id") or "")
    try:
        if event_type == "permission_request":
            call = data.get("tool_call") or {}
            tool = call.get("name") or "tool"
            answer = await asyncio.to_thread(
                input,
                f"\nAllow {tool}? [y] once / [a] session / [N] deny: ",
            )
            choice = answer.strip().lower()
            return {
                "request_id": request_id,
                "status": "answered",
                "decision": "allow"
                if choice in {"y", "yes", "a", "always"}
                else "deny",
                "scope": "session" if choice in {"a", "always"} else "once",
            }

        options = data.get("options") or []
        print(f"\n{data.get('question', 'Input required')}")
        for index, option in enumerate(options, 1):
            label = option.get("label", "") if isinstance(option, dict) else str(option)
            description = option.get("description", "") if isinstance(option, dict) else ""
            suffix = f" - {description}" if description else ""
            print(f"  {index}. {label}{suffix}")
        answer = await asyncio.to_thread(input, "Select an option: ")
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            selected = options[int(answer) - 1]
            answer = (
                selected.get("label", "")
                if isinstance(selected, dict)
                else str(selected)
            )
        return {"request_id": request_id, "status": "answered", "answer": answer}
    except (EOFError, KeyboardInterrupt):
        return {
            "request_id": request_id,
            "status": "cancelled",
            "reason": "terminal_input_cancelled",
        }


async def _run_once(args):
    """Run a single prompt and exit."""
    from xbotv2.core.bootstrap import bootstrap
    from xbotv2.core.session import SessionRuntime

    engine = await bootstrap(
        paths=RuntimePaths.from_data_dir(args.data_dir),
        provider_name=args.provider,
        session_id=getattr(args, "session", None),
        thread_id=getattr(args, "thread", "agent"),
        workspace_root=str(_workspace_root(args)),
        plugin_dirs=[] if args.no_plugins else None,
        selected_agent=getattr(args, "agent", None),
        interactive=False,
    )
    await engine.start_session()
    runtime = SessionRuntime(
        session_id=engine.state_store.session_id,
        thread_id=engine.state_store.thread_id,
        provider_name=args.provider,
        paths=engine.paths,
        workspace_root=str(_workspace_root(args)),
        no_plugins=args.no_plugins,
        engine=engine,
        interactive=False,
    )
    if engine.subagents is not None:
        engine.subagents.on_complete = None

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
