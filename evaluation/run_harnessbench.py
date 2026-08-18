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
CONTAINER_ROOT = Path("/opt/xbot")
CONTAINER_MARKER = "XBOT_EVAL_CONTAINER"
DEFAULT_IMAGE = "xbot-evaluation:local"


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
        default=EVALUATION_ROOT / "templates",
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
    args.name = args.name or time.strftime(
        f"harnessbench-{args.adapter}-%Y%m%d-%H%M%S"
    )
    if Path(args.name).name != args.name or args.name in {".", ".."}:
        parser.error("--name must be a single directory name")
    if os.environ.get(CONTAINER_MARKER) != "1":
        return _run_in_container(args, inspect_args, data_dir, provider)

    return _run_evaluation(args, inspect_args, data_dir, provider)


def _run_evaluation(
    args: argparse.Namespace,
    inspect_args: list[str],
    data_dir: Path,
    provider: dict[str, Any],
) -> int:
    provider_type = str(provider["provider"])
    model = str(provider["model"])
    base_url = provider.get("base_url")

    run_root = EVALUATION_ROOT / "results" / args.name
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
    env.update(setup.environment)
    env["XBOT_EVAL_ADAPTER"] = adapter.name
    env.setdefault("HARNESSBENCH_PUBLIC_URL_TEMPLATE", "{local_url}")
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


def _run_in_container(
    args: argparse.Namespace,
    inspect_args: list[str],
    data_dir: Path,
    provider: dict[str, Any],
) -> int:
    container_data = _container_path(data_dir, "--data-dir")
    container_command = _container_command(args.agent_command)
    run_root = EVALUATION_ROOT / "results" / args.name
    run_root.mkdir(parents=True, exist_ok=True)

    image = DEFAULT_IMAGE
    inspected = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        text=True,
        capture_output=True,
    )
    if inspected.returncode:
        raise RuntimeError(
            f"Evaluation image {image!r} is not available; build it with "
            "evaluation/docker/Dockerfile"
        )
    image_id = inspected.stdout.strip()
    container_run_root = CONTAINER_ROOT / "evaluation" / "results" / args.name
    interactive = (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and sys.stderr.isatty()
    )
    command = ["docker", "run"]
    if interactive:
        command.extend(["--interactive", "--tty"])
    command.extend([
        "--rm",
        "--init",
        "--name",
        _container_name(args.name),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--security-opt",
        "seccomp=unconfined",
        "--pids-limit",
        "4096",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=4g",
        "--mount",
        "type=volume,dst=/home/xbot",
        "--mount",
        f"type=bind,src={REPO_ROOT},dst={CONTAINER_ROOT}",
        "--mount",
        f"type=bind,src={run_root},dst={container_run_root}",
        "--env",
        f"{CONTAINER_MARKER}=1",
        "--env",
        "SHELL=/bin/bash",
        "--env",
        f"XBOT_EVAL_GIT_COMMIT={_git_output('rev-parse', 'HEAD')}",
        "--env",
        "XBOT_EVAL_GIT_TRACKED_DIRTY=" + (
            "1" if _git_output("status", "--short", "--untracked-files=no") else "0"
        ),
        "--env",
        f"XBOT_EVAL_CONTAINER_IMAGE={image}",
        "--env",
        f"XBOT_EVAL_CONTAINER_IMAGE_ID={image_id}",
    ])
    if interactive:
        for name in ("TERM", "COLORTERM"):
            if name in os.environ:
                command.extend(["--env", name])
    source_name = provider.get("api_key_env")
    if source_name:
        command.extend(["--env", str(source_name)])
    for name in (
        "RUBRIC_API_KEY",
        "RUBRIC_BASE_URL",
        "RUBRIC_MODEL",
        "HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM",
        "HARNESSBENCH_SKIP_PROCESS_GRADE",
        "HARNESSBENCH_PUBLIC_URL_TEMPLATE",
    ):
        if name in os.environ:
            command.extend(["--env", name])
    if args.adapter == "opencode":
        opencode_runtime = run_root / "data" / "opencode" / "runtime"
        opencode_runtime.mkdir(parents=True, exist_ok=True)
        command.extend([
            "--mount",
            "type=bind,"
            f"src={opencode_runtime},dst=/home/xbot/.local/share/opencode",
        ])
    command.extend([
        image,
        *_inner_arguments(
            args,
            inspect_args,
            data_dir=container_data,
            agent_command=container_command,
        ),
    ])
    return subprocess.run(command, cwd=REPO_ROOT).returncode


def _inner_arguments(
    args: argparse.Namespace,
    inspect_args: list[str],
    *,
    data_dir: Path,
    agent_command: str | None,
) -> list[str]:
    result = [
        "--adapter",
        args.adapter,
        "--name",
        args.name,
        "--provider",
        args.provider,
        "--data-dir",
        str(data_dir),
        "--jobs",
        str(args.jobs),
        "--provider-max-retries",
        str(args.provider_max_retries),
        "--sample-retries",
        str(args.sample_retries),
        "--retry-attempts",
        str(args.retry_attempts),
        "--retry-wait",
        str(args.retry_wait),
    ]
    for option, value in (
        ("--agent-command", agent_command),
        ("--limit", args.limit),
        ("--sample-id", args.sample_id),
    ):
        if value is not None:
            result.extend([option, str(value)])
    return [*result, *inspect_args]


def _container_path(path: Path, option: str) -> Path:
    try:
        relative = path.resolve().relative_to(REPO_ROOT)
    except ValueError as exc:
        raise ValueError(
            f"{option} must be inside {REPO_ROOT} for Docker evaluation"
        ) from exc
    return CONTAINER_ROOT / relative


def _container_command(command: str | None) -> str | None:
    if command is None:
        return None
    path = Path(command)
    if not path.is_absolute():
        return command
    return str(_container_path(path, "--agent-command"))


def _container_name(run_name: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in "_.-" else "-"
        for character in run_name
    ).strip("-.")
    return f"xbot-eval-{safe[:48]}-{os.getpid()}"


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
        "git_tracked_dirty": _git_tracked_dirty(),
        "runtime": (
            "docker" if os.environ.get(CONTAINER_MARKER) == "1" else "local"
        ),
        "container_image": os.environ.get("XBOT_EVAL_CONTAINER_IMAGE"),
        "container_image_id": os.environ.get("XBOT_EVAL_CONTAINER_IMAGE_ID"),
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
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    # Latest state stays at the canonical name for consumers; each invocation
    # also gets its own file so multi-segment runs never overwrite earlier
    # evidence (report_20260810 run-manifest lesson).
    (run_root / "run-manifest.json").write_text(manifest_text, encoding="utf-8")
    stamp = started_at.replace(":", "-").replace("+00:00", "Z").replace("+", "-")
    (run_root / f"run-manifest-{stamp}.json").write_text(
        manifest_text,
        encoding="utf-8",
    )


def _provider_config(data_dir: Path, name: str) -> dict[str, Any]:
    """Read one provider definition from the llm plugin tree entry.

    Providers live in the ``llm`` plugin's tree config (the bundled
    ``xcore.yaml`` merged with the data dir's ``plugins.yaml`` overlay) —
    there is no separate ``providers.yaml`` document.
    """
    from XBotv2.loader import PluginTree

    tree = PluginTree.from_yaml(REPO_ROOT / "XBotv2" / "xcore.yaml")
    overlay_path = data_dir / "config" / "plugins.yaml"
    if overlay_path.is_file():
        tree = tree.merged_with(PluginTree.from_yaml(overlay_path))
    llm_entry = next(
        (entry for entry in tree.entries if entry.id == "llm"),
        None,
    )
    if llm_entry is None:
        raise ValueError("no llm plugin entry in merged plugin tree")
    providers = (llm_entry.config or {}).get("providers") or {}
    provider = providers.get(name)
    if not isinstance(provider, dict):
        raise ValueError(f"Unknown provider {name!r} in {data_dir}")
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


def _git_output(*args: str) -> str:
    if args == ("rev-parse", "HEAD"):
        value = os.environ.get("XBOT_EVAL_GIT_COMMIT")
        if value:
            return value
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _git_tracked_dirty() -> bool:
    value = os.environ.get("XBOT_EVAL_GIT_TRACKED_DIRTY")
    if value is not None:
        return value == "1"
    return bool(_git_output("status", "--short", "--untracked-files=no"))


if __name__ == "__main__":
    raise SystemExit(main())
