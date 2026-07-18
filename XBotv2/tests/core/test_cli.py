"""Command-line parsing and entrypoint tests."""

import argparse
import subprocess
from pathlib import Path

import pytest

import xbotv2.__main__ as cli


def parse(argv: list[str]) -> argparse.Namespace:
    return cli._parse_args(argv)[1]


def test_default_command_is_terminal():
    args = parse([])

    assert args.command == "terminal"
    assert args.provider == "default"
    assert args.thread == "agent"


@pytest.mark.parametrize("command", ["serve", "tui", "web", "terminal"])
def test_named_commands(command):
    args = parse([command, "--provider", "minimax", "--workspace", "work"])

    assert args.command == command
    assert args.provider == "minimax"
    assert args.workspace == "work"


def test_tui_has_server_defaults_for_auto_spawn():
    args = parse(["tui"])

    assert args.bind == "127.0.0.1"
    assert args.port == 4096


def test_workspace_defaults_to_startup_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XBOT_WORKSPACE", "/ignored/environment/workspace")
    monkeypatch.chdir(tmp_path)

    args = parse(["terminal"])

    assert args.workspace is None
    assert cli._workspace_root(args) == tmp_path.resolve()


def test_once_requires_and_preserves_prompt():
    args = parse(["once", "review this repository"])

    assert args.command == "once"
    assert args.prompt == "review this repository"


def test_legacy_mode_form_remains_compatible():
    args = parse([
        "--data-dir", "runtime", "--provider", "minimax",
        "--mode", "server", "--port", "4100",
    ])

    assert args.command == "serve"
    assert args.data_dir == "runtime"
    assert args.provider == "minimax"
    assert args.port == 4100


def test_xbot_environment_defaults_override_legacy_prefix(monkeypatch):
    monkeypatch.setenv("XBOT_PROVIDER", "current")
    monkeypatch.setenv("XBOTV2_PROVIDER", "legacy")
    monkeypatch.setenv("XBOT_PORT", "4200")

    args = parse(["serve"])

    assert args.provider == "current"
    assert args.port == 4200


def test_bash_entrypoint_has_valid_syntax():
    entrypoint = Path(__file__).parents[3] / "xbot"

    subprocess.run(["bash", "-n", str(entrypoint)], check=True)


def test_web_runs_api_and_vite_then_cleans_up(monkeypatch):
    class Process:
        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def kill(self):
            self.returncode = -9

    server = Process()
    client = Process()
    launched = {}
    monkeypatch.setattr(cli, "_spawn_server", lambda _args: server)
    monkeypatch.setattr(cli, "_wait_for_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda command, **kwargs: launched.update(command=command, kwargs=kwargs) or client,
    )
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)

    cli._run_web(parse(["web", "--port", "4100", "--web-port", "5180"]))

    assert launched["command"][-2:] == ["--port", "5180"]
    assert launched["kwargs"]["env"]["XBOT_API_URL"] == "http://127.0.0.1:4100"
    assert opened == ["http://127.0.0.1:5180"]
    assert server.terminated


@pytest.mark.asyncio
async def test_terminal_permission_choice_can_apply_to_session(monkeypatch):
    async def read_input(_function, _prompt):
        return "a"

    monkeypatch.setattr(cli.asyncio, "to_thread", read_input)

    result = await cli._terminal_interaction({
        "type": "permission_request",
        "data": {
            "request_id": "permission:c1",
            "tool_call": {"name": "filesystem_write"},
        },
    })

    assert result == {
        "request_id": "permission:c1",
        "status": "answered",
        "decision": "allow",
        "scope": "session",
    }


@pytest.mark.asyncio
async def test_terminal_user_choice_returns_option_label(monkeypatch):
    async def read_input(_function, _prompt):
        return "2"

    monkeypatch.setattr(cli.asyncio, "to_thread", read_input)

    result = await cli._terminal_interaction({
        "type": "user_input_required",
        "data": {
            "request_id": "user_input:c1",
            "question": "Which mode?",
            "options": [
                {"label": "Fast", "description": "Use fewer tokens"},
                {"label": "Thorough", "description": "Inspect more files"},
            ],
        },
    })

    assert result["answer"] == "Thorough"
