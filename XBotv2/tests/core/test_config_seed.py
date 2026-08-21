from pathlib import Path

from XBotv2.config.seed import ensure_initial_config
from XBotv2.core.paths import RuntimePaths


def test_initial_config_materializes_packaged_plugin_skill(tmp_path: Path) -> None:
    paths = RuntimePaths.from_data_dir(tmp_path / "data")

    ensure_initial_config(paths)

    skill = paths.data_dir / ".agents" / "skills" / "xbot-plugin-development" / "SKILL.md"
    assert skill.is_file()
    assert "xbot-plugin-development" in skill.read_text(encoding="utf-8")


def test_initial_config_preserves_unrelated_global_skills(tmp_path: Path) -> None:
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    custom = paths.data_dir / ".agents" / "skills" / "my-skill" / "SKILL.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("custom", encoding="utf-8")

    ensure_initial_config(paths)

    assert custom.read_text(encoding="utf-8") == "custom"
