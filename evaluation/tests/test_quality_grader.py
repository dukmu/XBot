from __future__ import annotations

from pathlib import Path

from harnessbench.grading.oracle_quality_llm import run_oracle_quality_llm
from harnessbench.grading.rubric_llm import build_workspace_image_attachment


def test_official_quality_grader_skips_via_env(monkeypatch, tmp_path):
    image = tmp_path / "cat.png"
    image.write_bytes(b"png")

    user = build_workspace_image_attachment(
        tmp_path,
        ["cat.png", "missing.png"],
        "rubric text",
    )
    assert isinstance(user, list)
    assert any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in user
    )

    monkeypatch.setenv("HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM", "1")
    quality, metadata = run_oracle_quality_llm(system="system", user=user)

    assert quality is None
    assert metadata["skipped"] is True
