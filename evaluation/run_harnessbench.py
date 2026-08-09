from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from xbot_eval.adapters import (
    AdapterContext,
    AdapterSetup,
    adapter_names,
    get_adapter,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run HarnessBench through the Inspect Agent Bridge."
    )
    parser.add_argument("-j", "--jobs", type=int, default=4)
    parser.add_argument("--adapter", choices=adapter_names(), default="xbot")
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
    parser.add_argument("--agent-command", help="override the adapter executable")
    parser.add_argument(
        "--limit",
        help="Inspect sample limit, for example 4 or 1:10",
    )
    parser.add_argument(
        "--sample-id",
        help="comma-separated sample IDs to run in the selected result directory",
    )
    parser.add_argument(
        "--provider-max-retries",
        type=int,
        default=8,
        help="maximum retries for one model API request",
    )
    parser.add_argument(
        "--sample-retries",
        type=int,
        default=2,
        help="automatic retries for a sample that ends with an error",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=10,
        help="automatic eval-set attempts",
    )
    parser.add_argument(
        "--retry-wait",
        type=int,
        default=30,
        help="base seconds between eval-set attempts; waiting is exponential",
    )
    args, inspect_args = parser.parse_known_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.provider_max_retries < 0:
        parser.error("--provider-max-retries cannot be negative")
    if args.sample_retries < 0:
        parser.error("--sample-retries cannot be negative")
    if args.retry_attempts < 0:
        parser.error("--retry-attempts cannot be negative")
    if args.retry_wait < 0:
        parser.error("--retry-wait cannot be negative")

    data_dir = args.data_dir.resolve()
    provider = _provider_config(data_dir, args.provider)
    provider_type = str(provider["provider"])
    model = str(provider["model"])
    base_url = provider.get("base_url")

    run_name = args.name or time.strftime(
        f"harnessbench-{args.adapter}-%Y%m%d-%H%M%S"
    )
    run_root = EVALUATION_ROOT / "results" / run_name
    run_root.mkdir(parents=True, exist_ok=True)
    run_data = run_root / "data"
    run_data.mkdir(exist_ok=True)
    adapter = get_adapter(args.adapter)
    setup = adapter.prepare(
        AdapterContext(
            repo_root=REPO_ROOT,
            run_data=run_data,
            source_data=data_dir,
            provider_name=args.provider,
            provider=provider,
        ),
        args.agent_command,
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(REPO_ROOT / "XBotv2"),
        str(EVALUATION_ROOT / "src"),
        env.get("PYTHONPATH"),
    ]))
    env.update(setup.environment)
    env["XBOT_EVAL_ADAPTER"] = adapter.name
    env.setdefault("HARNESSBENCH_PUBLIC_URL_TEMPLATE", "{local_url}")
    _set_inspect_runtime_environment(env, run_data / "inspect")
    _provider_credentials(env, provider_type, provider)

    command = [
        str(Path(sys.executable).parent / "inspect"),
        "eval" if args.sample_id else "eval-set",
        "evaluation/tasks/harnessbench_full.py",
        "--model",
        f"{provider_type}/{model}",
        "--max-samples",
        str(args.jobs),
        f"--max-retries={args.provider_max_retries}",
        f"--retry-on-error={args.sample_retries}",
        "--continue-on-fail",
        "--ctl-server",
        "false",
        "--log-dir",
        str(run_root),
    ]
    if args.sample_id:
        command.extend(["--sample-id", args.sample_id])
    else:
        command.extend([
            "--log-dir-allow-dirty",
            f"--retry-attempts={args.retry_attempts}",
            "--no-retry-immediate",
            f"--retry-wait={args.retry_wait}",
        ])
    if base_url:
        command.extend(["--model-base-url", str(base_url)])
    for option, value in (
        ("--temperature", provider.get("temperature")),
        ("--max-tokens", provider.get("max_output_tokens")),
        ("--reasoning-effort", provider.get("reasoning_effort")),
    ):
        if value is not None:
            command.extend([option, str(value)])
    if args.limit:
        command.extend(["--limit", args.limit])
    command.extend(inspect_args)
    started_at = datetime.now(timezone.utc).isoformat()
    _write_run_manifest(
        run_root,
        args=args,
        run_data=run_data,
        setup=setup,
        provider=provider,
        provider_type=provider_type,
        model=model,
        base_url=base_url,
        inspect_args=inspect_args,
        status="running",
        started_at=started_at,
    )
    returncode = subprocess.run(command, env=env, cwd=REPO_ROOT).returncode
    _write_run_manifest(
        run_root,
        args=args,
        run_data=run_data,
        setup=setup,
        provider=provider,
        provider_type=provider_type,
        model=model,
        base_url=base_url,
        inspect_args=inspect_args,
        status="completed" if returncode == 0 else f"failed:{returncode}",
        started_at=started_at,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    return returncode


def _write_run_manifest(
    run_root: Path,
    *,
    args: argparse.Namespace,
    run_data: Path,
    setup: AdapterSetup,
    provider: dict[str, Any],
    provider_type: str,
    model: str,
    base_url: str | None,
    inspect_args: list[str],
    status: str,
    started_at: str,
    completed_at: str | None = None,
) -> None:
    manifest = {
        "schema_version": 1,
        "run_name": run_root.name,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "adapter": args.adapter,
        "provider": args.provider,
        "provider_type": provider_type,
        "model": model,
        "base_url": base_url,
        "jobs": args.jobs,
        "limit": args.limit,
        "sample_id": args.sample_id,
        "provider_max_retries": args.provider_max_retries,
        "sample_retries": args.sample_retries,
        "retry_attempts": args.retry_attempts,
        "retry_wait": args.retry_wait,
        "agent_command": setup.command,
        "git_commit": _git_output("rev-parse", "HEAD"),
        "git_tracked_dirty": bool(
            _git_output("status", "--short", "--untracked-files=no")
        ),
        "data_dir": str(args.data_dir.resolve()),
        "run_data": str(run_data),
        "adapter_data": str(setup.data_dir),
        "provider_config": {
            key: value
            for key, value in provider.items()
            if key not in {"api_key", "api_key_env"}
        },
        "inspect_args": list(inspect_args),
    }
    path = run_root / "run-manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def _set_inspect_runtime_environment(
    env: dict[str, str],
    root: Path,
) -> None:
    directories = {
        "XDG_CONFIG_HOME": root / "config",
        "XDG_DATA_HOME": root / "data",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_STATE_HOME": root / "state",
        # Inspect appends Unix socket names below TMPDIR. Keep this path short.
        "TMPDIR": EVALUATION_ROOT / "results" / ".tmp",
    }
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)
    env.update({key: str(path) for key, path in directories.items()})


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
