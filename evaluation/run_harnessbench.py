from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run HarnessBench through the Inspect Agent Bridge."
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

    data_dir = args.data_dir.resolve()
    provider = _provider_config(data_dir, args.provider)
    provider_type = str(provider["provider"])
    model = str(provider["model"])
    base_url = provider.get("base_url")

    run_name = args.name or time.strftime("harnessbench-%Y%m%d-%H%M%S")
    run_root = EVALUATION_ROOT / "results" / run_name
    run_root.mkdir(parents=True, exist_ok=True)
    if any(run_root.iterdir()):
        parser.error(f"result directory is not empty: {run_root}")
    evaluation_data = run_root / "data"
    _copy_evaluation_data(data_dir, evaluation_data)

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(REPO_ROOT / "XBotv2"),
        str(EVALUATION_ROOT / "src"),
        env.get("PYTHONPATH"),
    ]))
    env["XBOT_EVAL_COMMAND"] = str(Path(
        env.get("XBOT_EVAL_COMMAND", REPO_ROOT / ".venv" / "bin" / "xbot")
    ).resolve())
    env["XBOT_EVAL_DATA_DIR"] = str(evaluation_data)
    env.setdefault("HARNESSBENCH_PUBLIC_URL_TEMPLATE", "{local_url}")
    _provider_credentials(env, provider_type, provider)

    command = [
        str(Path(sys.executable).parent / "inspect"),
        "eval",
        "evaluation/tasks/harnessbench_full.py",
        "--model",
        f"{provider_type}/{model}",
        "--max-samples",
        str(args.jobs),
        "--retry-on-error=2",
        "--continue-on-fail",
        "--ctl-server",
        "false",
        "--log-dir",
        str(run_root),
    ]
    if base_url:
        command.extend(["--model-base-url", str(base_url)])
    if args.limit:
        command.extend(["--limit", args.limit])
    command.extend(inspect_args)
    return subprocess.run(command, env=env, cwd=REPO_ROOT).returncode


def _copy_evaluation_data(source: Path, target: Path) -> None:
    target.mkdir()
    for name in ("config", ".agents", "memory"):
        path = source / name
        if path.exists():
            shutil.copytree(path, target / name)


def _provider_config(data_dir: Path, name: str) -> dict[str, Any]:
    path = data_dir / "config" / "providers.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    providers = data.get("providers") or {}
    provider = providers.get(name)
    if not isinstance(provider, dict):
        raise ValueError(f"Unknown provider {name!r} in {path}")
    return provider


def _provider_credentials(
    env: dict[str, str],
    provider_type: str,
    provider: dict[str, Any],
) -> None:
    source_name = provider.get("api_key_env")
    api_key = env.get(str(source_name), "") if source_name else provider.get("api_key")
    if not api_key:
        raise ValueError(f"Missing API key for {provider_type} provider")
    target = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(provider_type)
    if target is None:
        raise ValueError(
            f"Inspect Agent Bridge does not support provider {provider_type!r}"
        )
    env[target] = str(api_key)


if __name__ == "__main__":
    raise SystemExit(main())
