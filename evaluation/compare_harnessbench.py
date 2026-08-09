from __future__ import annotations

import argparse
import json
import math
import statistics
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
CASES = REPO_ROOT / "evaluation" / "cases" / "harnessbench"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare paired XBot and OpenCode HarnessBench results."
    )
    parser.add_argument("--xbot", type=Path, required=True)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    xbot = _load_result(args.xbot, "xbot")
    opencode = _load_result(args.opencode, "opencode")
    comparison = _compare(xbot, opencode)
    document = {
        "schema_version": 1,
        "xbot": xbot["summary"],
        "opencode": opencode["summary"],
        "comparison": comparison,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "COMPARISON.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output / "COMPARISON.md").write_text(
        _markdown(document),
        encoding="utf-8",
    )
    return 0


def _load_result(path: Path, adapter: str) -> dict[str, Any]:
    samples: dict[str, dict[str, Any]] = {}
    archives = sorted(path.glob("*.eval"), key=lambda item: item.stat().st_mtime)
    if not archives:
        raise ValueError(f"No Inspect .eval logs found in {path}")
    for archive in archives:
        with zipfile.ZipFile(archive) as bundle:
            if "summaries.json" not in bundle.namelist():
                continue
            summaries = {
                str(item["id"]): item
                for item in json.loads(bundle.read("summaries.json"))
            }
            sample_names = {
                name.rsplit("/", 1)[-1].rsplit("_epoch_", 1)[0]: name
                for name in bundle.namelist()
                if name.startswith("samples/") and name.endswith(".json")
            }
            for sample_id, summary in summaries.items():
                detail_name = sample_names.get(sample_id)
                detail = (
                    json.loads(bundle.read(detail_name))
                    if detail_name
                    else {}
                )
                summary["tool_events"] = _tool_events(detail, adapter)
                summary["category"] = _category(sample_id)
                summary["source_log"] = archive.name
                samples[sample_id] = summary
    if not samples:
        raise ValueError(f"No completed samples found in {path}")
    return {
        "path": str(path.resolve()),
        "samples": samples,
        "summary": _summarize(path, samples),
    }


def _tool_events(sample: dict[str, Any], adapter: str) -> dict[str, int]:
    metadata = sample.get("metadata") or {}
    events = (metadata.get(adapter) or {}).get("acp_events") or []
    counts = {"calls": 0, "completed": 0, "failed": 0}
    for event in events:
        update = event.get("session_update")
        status = event.get("status")
        if update == "tool_call":
            counts["calls"] += 1
        if update in {"tool_call", "tool_call_update"} and status in {
            "completed",
            "failed",
        }:
            counts[status] += 1
    return counts


def _category(sample_id: str) -> str:
    path = CASES / sample_id / "task.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return str(data.get("class") or "Unspecified")


def _score(sample: dict[str, Any]) -> float:
    value = ((sample.get("scores") or {}).get("workspace_oracle") or {}).get("value")
    if not isinstance(value, (int, float)):
        raise ValueError(f"Sample {sample.get('id')} has no numeric workspace score")
    return float(value)


def _summarize(path: Path, samples: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scores = [_score(sample) for sample in samples.values()]
    usage: defaultdict[str, int] = defaultdict(int)
    tools: defaultdict[str, int] = defaultdict(int)
    for sample in samples.values():
        for model_usage in (sample.get("model_usage") or {}).values():
            for key, value in model_usage.items():
                if isinstance(value, int):
                    usage[key] += value
        for key, value in sample["tool_events"].items():
            tools[key] += value
    terminal = tools["completed"] + tools["failed"]
    return {
        "result_dir": str(path.resolve()),
        "samples": len(samples),
        "mean_score": statistics.fmean(scores),
        "median_score": statistics.median(scores),
        "perfect_scores": sum(score == 1.0 for score in scores),
        "zero_scores": sum(score == 0.0 for score in scores),
        "completed_samples": sum(bool(item.get("completed")) for item in samples.values()),
        "retries": sum(int(item.get("retries") or 0) for item in samples.values()),
        "total_time_seconds": sum(float(item.get("total_time") or 0) for item in samples.values()),
        "usage": dict(usage),
        "tools": {
            **dict(tools),
            "terminal_completion_rate": (
                tools["completed"] / terminal if terminal else None
            ),
        },
    }


def _compare(xbot: dict[str, Any], opencode: dict[str, Any]) -> dict[str, Any]:
    common = sorted(set(xbot["samples"]) & set(opencode["samples"]))
    if not common:
        raise ValueError("The result directories have no common sample IDs")
    rows = []
    categories: defaultdict[str, list[tuple[float, float]]] = defaultdict(list)
    for sample_id in common:
        xbot_score = _score(xbot["samples"][sample_id])
        opencode_score = _score(opencode["samples"][sample_id])
        category = xbot["samples"][sample_id]["category"]
        categories[category].append((xbot_score, opencode_score))
        rows.append({
            "sample_id": sample_id,
            "category": category,
            "xbot": xbot_score,
            "opencode": opencode_score,
            "delta": opencode_score - xbot_score,
        })
    deltas = [row["delta"] for row in rows]
    stderr = statistics.stdev(deltas) / math.sqrt(len(deltas)) if len(deltas) > 1 else 0.0
    category_rows = []
    for category, values in sorted(categories.items()):
        xbot_mean = statistics.fmean(item[0] for item in values)
        opencode_mean = statistics.fmean(item[1] for item in values)
        category_rows.append({
            "category": category,
            "samples": len(values),
            "xbot": xbot_mean,
            "opencode": opencode_mean,
            "delta": opencode_mean - xbot_mean,
        })
    return {
        "paired_samples": len(rows),
        "xbot_wins": sum(row["delta"] < 0 for row in rows),
        "opencode_wins": sum(row["delta"] > 0 for row in rows),
        "ties": sum(row["delta"] == 0 for row in rows),
        "mean_delta_opencode_minus_xbot": statistics.fmean(deltas),
        "paired_stderr": stderr,
        "approximate_95_percent_interval": [
            statistics.fmean(deltas) - 1.96 * stderr,
            statistics.fmean(deltas) + 1.96 * stderr,
        ],
        "categories": category_rows,
        "largest_opencode_gains": sorted(
            rows, key=lambda item: item["delta"], reverse=True
        )[:10],
        "largest_xbot_gains": sorted(rows, key=lambda item: item["delta"])[:10],
        "samples": rows,
    }


def _markdown(document: dict[str, Any]) -> str:
    xbot = document["xbot"]
    opencode = document["opencode"]
    comparison = document["comparison"]

    def metric(label: str, key: str, digits: int = 4) -> str:
        return (
            f"| {label} | {xbot[key]:.{digits}f} | "
            f"{opencode[key]:.{digits}f} |\n"
        )

    lines = [
        "# XBot vs OpenCode HarnessBench Comparison\n\n",
        "Both frameworks use the same Inspect model bridge and provider. Tool ",
        "completion below is a transport status, not semantic task success.\n\n",
        "| Metric | XBot | OpenCode |\n",
        "| --- | ---: | ---: |\n",
        metric("Samples", "samples", 0),
        metric("Mean workspace score", "mean_score"),
        metric("Median workspace score", "median_score"),
        metric("Perfect scores", "perfect_scores", 0),
        metric("Retries", "retries", 0),
        metric("Total time (seconds)", "total_time_seconds", 1),
        "\n",
        "## Efficiency\n\n",
        "| Metric | XBot | OpenCode |\n",
        "| --- | ---: | ---: |\n",
        f"| Input tokens | {xbot['usage'].get('input_tokens', 0)} | "
        f"{opencode['usage'].get('input_tokens', 0)} |\n",
        f"| Cache-read tokens | {xbot['usage'].get('input_tokens_cache_read', 0)} | "
        f"{opencode['usage'].get('input_tokens_cache_read', 0)} |\n",
        f"| Output tokens | {xbot['usage'].get('output_tokens', 0)} | "
        f"{opencode['usage'].get('output_tokens', 0)} |\n",
        f"| ACP Tool calls | {xbot['tools'].get('calls', 0)} | "
        f"{opencode['tools'].get('calls', 0)} |\n",
        f"| ACP Tool failures | {xbot['tools'].get('failed', 0)} | "
        f"{opencode['tools'].get('failed', 0)} |\n",
        "\n",
        f"Paired samples: {comparison['paired_samples']}; ",
        f"XBot wins: {comparison['xbot_wins']}; ",
        f"OpenCode wins: {comparison['opencode_wins']}; ",
        f"ties: {comparison['ties']}.\n\n",
        "Mean delta (OpenCode - XBot): ",
        f"{comparison['mean_delta_opencode_minus_xbot']:.4f}; ",
        "approximate 95% interval: ",
        f"[{comparison['approximate_95_percent_interval'][0]:.4f}, ",
        f"{comparison['approximate_95_percent_interval'][1]:.4f}].\n\n",
        "## Categories\n\n",
        "| Category | Samples | XBot | OpenCode | Delta |\n",
        "| --- | ---: | ---: | ---: | ---: |\n",
    ]
    for row in comparison["categories"]:
        lines.append(
            f"| {row['category']} | {row['samples']} | {row['xbot']:.4f} | "
            f"{row['opencode']:.4f} | {row['delta']:+.4f} |\n"
        )
    lines.extend([
        "\n## Largest Paired Differences\n\n",
        "| Sample | XBot | OpenCode | Delta |\n",
        "| --- | ---: | ---: | ---: |\n",
    ])
    differences = (
        comparison["largest_opencode_gains"][:5]
        + comparison["largest_xbot_gains"][:5]
    )
    seen: set[str] = set()
    for row in differences:
        if row["sample_id"] in seen:
            continue
        seen.add(row["sample_id"])
        lines.append(
            f"| {row['sample_id']} | {row['xbot']:.4f} | "
            f"{row['opencode']:.4f} | {row['delta']:+.4f} |\n"
        )
    return "".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
