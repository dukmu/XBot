from __future__ import annotations

import importlib.util
import shutil
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


def test_008_oracle_uses_official_quality_grader(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM", "1")
    case_dir = (
        Path(__file__).resolve().parents[1]
        / "cases"
        / "harnessbench"
        / "008-image-recognize"
    )
    spec = importlib.util.spec_from_file_location(
        "xbot_oracle_008",
        case_dir / "oracle_grade.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    workspace = tmp_path / "workspace"
    (workspace / "image").mkdir(parents=True)
    (workspace / "out").mkdir()
    fixtures = case_dir / "fixtures" / "image"
    for name in ("target1.png", "target2.jpg"):
        shutil.copyfile(fixtures / name, workspace / "image" / name)
    (workspace / "out" / "image1_answer.txt").write_text("red square", encoding="utf-8")
    (workspace / "out" / "image2_answer.txt").write_text("cat on blanket", encoding="utf-8")

    result = module.score_workspace(workspace)

    assert result["outcome_score"] == 1.0
    assert result["quality_rubric_meta"]["skipped"] is True
    assert "error" not in result["quality_rubric_meta"]
