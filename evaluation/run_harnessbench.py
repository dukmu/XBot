from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run HarnessBench against one concurrent XBot server."
    )
    parser.add_argument("-j", "--jobs", type=int, default=4)
    parser.add_argument(
        "--name",
        help="result directory name; defaults to a timestamped HarnessBench run",
    )
    parser.add_argument(
        "--provider",
        default=os.environ.get("XBOT_EVAL_PROVIDER", "minimax"),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / "XBotv2" / "data",
    )
    parser.add_argument(
        "--limit",
        help="Inspect sample limit, for example 4 or 1:10",
    )
    args, inspect_args = parser.parse_known_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    run_name = args.name or time.strftime("harnessbench-%Y%m%d-%H%M%S")
    run_root = EVALUATION_ROOT / "results" / run_name
    runtime_data = run_root / "data"
    run_root.mkdir(parents=True, exist_ok=True)
    if runtime_data.exists():
        parser.error(f"result directory already contains data: {run_root}")
    _copy_runtime_config(args.data_dir.resolve(), runtime_data)

    socket_path = run_root / "xbot.sock"
    log_path = run_root / "server.log"
    command = Path(
        os.environ.get(
            "XBOT_EVAL_COMMAND",
            REPO_ROOT / ".venv" / "bin" / "xbot",
        )
    ).resolve()
    server_args = [
        str(command),
        "serve",
        "--data-dir",
        str(runtime_data),
        "--provider",
        args.provider,
        "--workspace",
        str(REPO_ROOT),
        "--uds",
        str(socket_path),
    ]
    env = os.environ.copy()
    pythonpath = [
        str(REPO_ROOT / "XBotv2"),
        str(EVALUATION_ROOT / "src"),
    ]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["XBOT_EVAL_UDS"] = str(socket_path)
    env.setdefault("HARNESSBENCH_PUBLIC_URL_TEMPLATE", "{local_url}")

    inspect_command = [
        str(Path(sys.executable).parent / "inspect"),
        "eval",
        "evaluation/tasks/harnessbench_full.py",
        "--model",
        "none",
        "--max-samples",
        str(args.jobs),
        "--continue-on-fail",
        "--ctl-server",
        "false",
        "--log-dir",
        str(run_root / "logs"),
    ]
    if args.limit:
        inspect_command.extend(["--limit", args.limit])
    inspect_command.extend(inspect_args)

    with log_path.open("wb") as server_log:
        server = subprocess.Popen(
            server_args,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_server(socket_path, server, log_path)
            return subprocess.run(
                inspect_command,
                env=env,
                cwd=REPO_ROOT,
            ).returncode
        finally:
            _stop(server)
            socket_path.unlink(missing_ok=True)


def _copy_runtime_config(source: Path, target: Path) -> None:
    target.mkdir(parents=True)
    for name in ("config", ".agents", "memory"):
        path = source / name
        if path.exists():
            shutil.copytree(path, target / name)


def _wait_for_server(
    socket_path: Path,
    process: subprocess.Popen[bytes],
    log_path: Path,
) -> None:
    transport = httpx.HTTPTransport(uds=str(socket_path))
    with httpx.Client(transport=transport) as client:
        for _ in range(150):
            if process.poll() is not None:
                detail = log_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                raise RuntimeError(f"XBot server exited early:\n{detail}")
            try:
                if client.get("http://localhost/health").is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise RuntimeError(f"XBot server did not become ready: {log_path}")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
