"""Declarative plugin-tree values; scope activation is tested by loader."""
from pathlib import Path
import pytest
import yaml
from XBotv2.loader.api.contracts import PluginEntry, PluginTree, PluginOverlay

class TestPluginTree:
    def test_from_dict_list_and_nested(self):
        tree = PluginTree.parse([
            {"id": "a", "name": "mod.a"},
            {"id": "b", "name": "mod.b", "config": {"x": 1}, "disabled": True},
        ])
        assert [e.id for e in tree.entries] == ["a", "b"]
        assert tree.entries[1].config == {"x": 1}
        assert tree.entries[1].disabled is True

    def test_from_dict_plugins_key(self):
        tree = PluginTree.parse({"plugins": [{"id": "a", "name": "m"}]})
        assert tree.entries[0].id == "a"

    def test_from_yaml(self, tmp_path):
        path = tmp_path / "plugins.yaml"
        path.write_text(
            yaml.safe_dump([
                {"id": "goal", "name": "goal"},
            ]),
            encoding="utf-8",
        )
        tree = PluginTree.from_yaml(path)
        assert tree.entries[0].name == "goal"

    def test_duplicate_ids_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            PluginTree.parse([
                {"id": "a", "name": "m1"},
                {"id": "a", "name": "m2"},
            ])

    def test_overlay_preserves_omitted_fields_and_merges_config(self):
        base = PluginTree.parse([{
            "id": "a",
            "name": "m1",
            "config": {"policy": {"allow": ["base"], "ask": []}},
            "disabled": True,
            "isolate": {"tools": "private"},
            "profiles": ["agent"],
        }])
        overlay = PluginOverlay.parse([
            {
                "id": "a",
                "config": {"policy": {"ask": ["overlay"]}},
            },
            {"id": "b", "name": "m3"},
        ])
        merged = base.patched_with(overlay)
        ids = {e.id: e.name for e in merged.entries}
        assert ids == {"a": "m1", "b": "m3"}
        assert merged.entries[0].config == {
            "policy": {"allow": ["base"], "ask": ["overlay"]},
        }
        assert merged.entries[0].disabled is True
        assert merged.entries[0].isolate == {"tools": "private"}
        assert merged.entries[0].profiles == frozenset({"agent"})

    def test_overlay_new_entry_requires_name(self):
        with pytest.raises(ValueError, match="requires a name"):
            PluginTree([]).patched_with(PluginOverlay.parse([{"id": "new"}]))

    def test_session_style_overlay_rejects_unknown_id(self):
        with pytest.raises(ValueError, match="unknown plugin patch id"):
            PluginTree([]).patched_with(
                PluginOverlay.parse([{"id": "typo", "config": {}}]),
                allow_new=False,
            )

    @pytest.mark.parametrize(
        ("document", "error"),
        [
            ("not-a-tree", TypeError),
            ({"unknown": []}, ValueError),
            ({"plugins": [], "entries": []}, ValueError),
            ([{"id": "a", "name": "m", "unknown": True}], ValueError),
            ([{"id": "a", "name": "m", "disabled": "false"}], TypeError),
            ([{"id": "a", "name": "m", "config": []}], TypeError),
            ([{"id": "a", "name": "m", "isolate": []}], TypeError),
            ([{"id": "a", "name": "m", "profiles": []}], TypeError),
            ([{"id": "a"}], ValueError),
        ],
    )
    def test_complete_tree_rejects_malformed_documents(self, document, error):
        with pytest.raises(error):
            PluginTree.parse(document)

    def test_overlay_rejects_unresolved_non_boolean_disabled(self):
        overlay = [{"id": "a", "disabled": "${flag}"}]
        with pytest.raises(TypeError, match="boolean"):
            PluginOverlay.parse(overlay)

    def test_environment_references_resolve_without_runtime_values(self, monkeypatch):
        monkeypatch.setenv("XBOT_TEST_PLUGIN_DISABLED", "false")
        with pytest.raises(TypeError, match="boolean"):
            PluginOverlay.parse([{
                "id": "a",
                "disabled": "$${env:XBOT_TEST_PLUGIN_DISABLED}",
            }])

    def test_overlay_can_explicitly_clear_isolate_and_profiles(self):
        base = PluginTree.parse([{
            "id": "a",
            "name": "m",
            "isolate": {"tools": True},
            "profiles": ["agent"],
        }])
        patched = base.patched_with(PluginOverlay.parse([{
            "id": "a",
            "isolate": None,
            "profiles": None,
        }]))
        assert patched.entries[0].isolate is None
        assert patched.entries[0].profiles is None
