"""Tests for core filesystem tools."""

import json

from xbotv2.core.builtin_tools.filesystem import (
    filesystem_list,
    filesystem_read,
    filesystem_write,
)


def _payload(result) -> dict:
    return json.loads(result.content if hasattr(result, "content") else result)


class TestFilesystemReadList:
    def test_read_returns_metadata_and_truncation_flags(self, tmp_path):
        path = tmp_path / "sample.txt"
        path.write_text("a\nb\nc\nd\n", encoding="utf-8")

        data = _payload(filesystem_read.invoke({"path": str(path), "offset": 1, "limit": 2}))

        assert data["ok"] is True
        assert data["line_count"] == 4
        assert data["returned_lines"] == 2
        assert data["truncated_before"] is True
        assert data["truncated_after"] is True
        assert data["content"] == "b\nc"

    def test_list_returns_entry_metadata(self, tmp_path):
        (tmp_path / "dir").mkdir()
        (tmp_path / "file.txt").write_text("hello", encoding="utf-8")

        data = _payload(filesystem_list.invoke({"path": str(tmp_path)}))

        assert data["ok"] is True
        assert data["entry_count"] == 2
        assert {entry["name"] for entry in data["entries"]} == {"dir", "file.txt"}
        assert all("size_bytes" in entry for entry in data["entries"])


class TestFilesystemWriteModes:
    def test_append_prepend_insert_and_replace_lines(self, tmp_path):
        path = tmp_path / "notes.txt"

        _payload(filesystem_write.invoke({"path": str(path), "content": "b\n"}))
        _payload(filesystem_write.invoke({"path": str(path), "content": "a\n", "mode": "prepend"}))
        _payload(filesystem_write.invoke({
            "path": str(path),
            "content": "middle",
            "mode": "insert_line",
            "line": 2,
        }))
        data = _payload(filesystem_write.invoke({
            "path": str(path),
            "content": "z",
            "mode": "replace_lines",
            "start_line": 3,
            "end_line": 3,
        }))

        assert data["ok"] is True
        assert path.read_text(encoding="utf-8") == "a\nmiddle\nz\n"

    def test_regex_replace_reports_replacement_count(self, tmp_path):
        path = tmp_path / "code.py"
        path.write_text("alpha = 1\nalpha = 2\n", encoding="utf-8")

        data = _payload(filesystem_write.invoke({
            "path": str(path),
            "mode": "regex_replace",
            "pattern": "alpha",
            "replacement": "beta",
        }))

        assert data["ok"] is True
        assert data["replacements"] == 2
        assert path.read_text(encoding="utf-8") == "beta = 1\nbeta = 2\n"

    def test_apply_patch_uses_unified_diff(self, tmp_path):
        path = tmp_path / "patchme.txt"
        path.write_text("one\ntwo\nthree\n", encoding="utf-8")
        patch = (
            "--- a/patchme.txt\n"
            "+++ b/patchme.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " one\n"
            "-two\n"
            "+TWO\n"
            " three\n"
        )

        data = _payload(filesystem_write.invoke({
            "path": str(path),
            "mode": "apply_patch",
            "content": patch,
        }))

        assert data["ok"] is True
        assert path.read_text(encoding="utf-8") == "one\nTWO\nthree\n"
