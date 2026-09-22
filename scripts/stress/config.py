"""Command-line configuration for the stress runner."""

from __future__ import annotations

import argparse
import os
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_BASE_URL = "http://127.0.0.1:4096"


@dataclass(slots=True)
class StressConfig:
    layer: str
    scenario: str
    base_url: str
    server_urls: tuple[str, ...]
    auto_server: bool
    web_url: str
    tui_command: str
    in_process: bool
    in_process_http: bool
    mock_tools: bool
    servers: int
    concurrency: int
    turns: int
    timeout: float
    payload_size: int
    report_path: Path | None
    data_dir: Path | None
    browser: str
    mobile: bool
    duration: float
    history_turns: int
    failure_mode: str
    llama_cpp_url: str
    llama_cpp_model: str

    def as_dict(self) -> dict[str, object]:
        values = asdict(self)
        for key in ("report_path", "data_dir"):
            value = values[key]
            values[key] = str(value) if value is not None else None
        return values


def parse_args(argv: list[str] | None = None) -> StressConfig:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.stress",
        description="Exercise XBot server, WebUI, and TUI with one report.",
    )
    parser.add_argument(
        "--layer",
        choices=("server", "web", "tui", "all"),
        default="server",
        help="client layer to exercise",
    )
    parser.add_argument(
        "--scenario",
        default="all",
        help="comma-separated scenario names or all",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="existing server URL; omit to start an isolated real HTTP server",
    )
    parser.add_argument(
        "--server-urls",
        default="",
        help="comma-separated URLs of already running servers for multi-server probes",
    )
    parser.add_argument(
        "--external",
        action="store_true",
        help="require an existing server instead of auto-starting one",
    )
    parser.add_argument("--web-url", default="")
    parser.add_argument("--tui-command", default="")
    parser.add_argument(
        "--in-process",
        action="store_true",
        help="start an isolated MockLLM application in-process",
    )
    parser.add_argument(
        "--in-process-http",
        action="store_true",
        help="start an isolated MockLLM application on an ephemeral real HTTP port",
    )
    parser.add_argument(
        "--mock-tools",
        action="store_true",
        help="make the isolated MockLLM request a shell tool call once per turn",
    )
    parser.add_argument(
        "--servers",
        type=int,
        default=1,
        help="number of isolated server instances for the disposable harness",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--payload-size", type=int, default=4096)
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument(
        "--history-turns",
        type=int,
        default=20,
        help="turns for the long_history probe (defaults to 20)",
    )
    parser.add_argument(
        "--failure-mode",
        choices=("provider", "http_truncate"),
        default="provider",
        help="fault injected by stream_failure/incomplete scenarios",
    )
    parser.add_argument(
        "--llama-cpp-url",
        default=os.environ.get("XBOT_LLAMA_CPP_URL", ""),
        help="optional llama.cpp OpenAI-compatible endpoint for llama_cpp",
    )
    parser.add_argument(
        "--llama-cpp-model",
        default=os.environ.get("XBOT_LLAMA_CPP_MODEL", ""),
        help="optional model id for llama_cpp (defaults to remote first model)",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--browser", default="chromium", choices=("chromium", "firefox", "webkit"))
    parser.add_argument("--mobile", action="store_true")
    args = parser.parse_args(argv)
    if args.in_process and args.in_process_http:
        parser.error("--in-process and --in-process-http are mutually exclusive")
    if args.external and (args.in_process or args.in_process_http):
        parser.error("--external cannot be combined with an in-process server")
    if args.concurrency < 1 or args.turns < 1 or args.servers < 1 or args.history_turns < 1:
        parser.error("--concurrency, --turns, --servers, and --history-turns must be positive")
    if args.payload_size < 1 or args.timeout <= 0:
        parser.error("--payload-size must be positive and --timeout must be > 0")
    requested_urls = args.server_urls or args.base_url
    server_urls = tuple(
        value.strip()
        for value in requested_urls.split(",")
        if value.strip()
    )
    auto_server = not args.external and not requested_urls
    if not server_urls and not auto_server:
        parser.error("--server-urls must contain at least one URL")
    if not server_urls:
        server_urls = (DEFAULT_BASE_URL,)
    return StressConfig(
        layer=args.layer,
        scenario=args.scenario,
        base_url=server_urls[0],
        server_urls=server_urls,
        auto_server=auto_server,
        web_url=args.web_url,
        tui_command=args.tui_command,
        in_process=args.in_process,
        in_process_http=args.in_process_http,
        mock_tools=args.mock_tools,
        servers=args.servers,
        concurrency=args.concurrency,
        turns=args.turns,
        timeout=args.timeout,
        payload_size=args.payload_size,
        report_path=args.report,
        data_dir=args.data_dir,
        browser=args.browser,
        mobile=args.mobile,
        duration=args.duration,
        history_turns=args.history_turns,
        failure_mode=args.failure_mode,
        llama_cpp_url=args.llama_cpp_url.rstrip("/"),
        llama_cpp_model=args.llama_cpp_model,
    )
