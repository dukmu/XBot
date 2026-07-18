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
    entrypoint = Path(__file__).parents[2] / "bin" / "xbot"

    subprocess.run(["bash", "-n", str(entrypoint)], check=True)


def compiled_web_root(monkeypatch, tmp_path):
    root = tmp_path / "web_dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<main>XBot</main>", encoding="utf-8")
    monkeypatch.setattr(cli, "_WEB_STATIC_ROOT", root)


def test_web_runs_compiled_client_with_auto_uds_then_cleans_up(
    monkeypatch,
    tmp_path,
):
    compiled_web_root(monkeypatch, tmp_path)
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
    spawned = {}
    health = {}

    def spawn(args):
        spawned["uds"] = args.uds
        return server

    def wait_for_health(url, **kwargs):
        health.update(url=url, **kwargs)
        return True

    monkeypatch.setattr(cli, "_spawn_server", spawn)
    monkeypatch.setattr(cli, "_wait_for_health", wait_for_health)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    import uvicorn

    served = {}
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app, **kwargs: served.update(app=app, **kwargs),
    )

    cli._run_web(parse(["web", "--web-port", "5180"]))

    assert spawned["uds"].startswith("/tmp/xbotv2-web-")
    assert health["url"] == "http://localhost"
    assert health["uds_path"] == spawned["uds"]
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 5180
    assert opened == ["http://127.0.0.1:5180"]
    assert server.terminated


def test_web_can_use_existing_server_without_spawning(monkeypatch, tmp_path):
    compiled_web_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli,
        "_spawn_server",
        lambda _args: pytest.fail("external Web server must not spawn an API"),
    )
    monkeypatch.setattr(cli.webbrowser, "open", lambda _url: None)
    import uvicorn

    served = {}
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app, **kwargs: served.update(app=app, **kwargs),
    )

    cli._run_web(parse([
        "web",
        "--server", "http://127.0.0.1:4100",
        "--no-open",
    ]))

    assert served["app"] is not None


def test_web_server_and_uds_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        parse([
            "web",
            "--server", "http://localhost:4096",
            "--uds", "/tmp/xbot.sock",
        ])


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
