"""Tests for core filesystem tools."""

import json

import pytest

from xbotv2.core.builtin_tools.filesystem import (
    filesystem_find,
    filesystem_list,
    filesystem_read,
    filesystem_search,
    filesystem_write,
    write_file,
)
from xbotv2.tools.sandbox import SandboxPolicy


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

    def test_find_files_filters_by_glob(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print('x')", encoding="utf-8")
        (tmp_path / "README.md").write_text("docs", encoding="utf-8")

        data = _payload(filesystem_find.invoke({
            "path": str(tmp_path), "pattern": "*.py"
        }))

        assert data["ok"] is True
        assert data["files"] == ["src/app.py"]

    def test_search_text_returns_line_matches(self, tmp_path):
        (tmp_path / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("alpha two\n", encoding="utf-8")

        data = _payload(filesystem_search.invoke({
            "path": str(tmp_path), "pattern": "alpha"
        }))

        assert data["ok"] is True
        assert data["match_count"] == 2
        assert all("alpha" in match for match in data["matches"])


class TestFilesystemWriteModes:
    @pytest.mark.asyncio
    async def test_relative_write_uses_workspace_when_sandbox_is_disabled(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        policy = SandboxPolicy(enabled=False, workspace_root=workspace)

        result = await write_file(
            "notes.txt",
            content="workspace content",
            sandbox=policy,
        )

        assert result.data["ok"] is True
        assert (workspace / "notes.txt").read_text(encoding="utf-8") == (
            "workspace content"
        )

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


class TestSandboxedFilesystemWrite:
    class FakeSandbox:
        enabled = True

        def __init__(self, read_data, write_data=None):
            self.read_data = read_data
            self.write_data = write_data
            self.writes = []

        async def read_file(self, path, offset=0, limit=2000):
            assert offset == 0
            assert limit == 0
            return json.dumps(self.read_data)

        async def write_file(self, path, content):
            self.writes.append((path, content))
            return json.dumps(self.write_data or {
                "ok": True,
                "path": path,
                "bytes_written": len(content.encode("utf-8")),
            })

    @pytest.mark.asyncio
    async def test_preserves_edit_metadata(self):
        sandbox = self.FakeSandbox({"ok": True, "content": "alpha = 1\n"})

        result = await write_file(
            "code.py",
            mode="regex_replace",
            pattern="alpha",
            replacement="beta",
            sandbox=sandbox,
        )

        assert sandbox.writes == [("code.py", "beta = 1\n")]
        assert result.data["mode"] == "regex_replace"
        assert result.data["changed"] is True
        assert result.data["replacements"] == 1

    @pytest.mark.asyncio
    async def test_append_creates_missing_file(self):
        sandbox = self.FakeSandbox({
            "ok": False,
            "error": {"code": "file_not_found", "message": "missing"},
        })

        result = await write_file(
            "new.txt",
            content="first\n",
            mode="append",
            sandbox=sandbox,
        )

        assert sandbox.writes == [("new.txt", "first\n")]
        assert result.data["mode"] == "append"
        assert result.data["changed"] is True

    @pytest.mark.asyncio
    async def test_read_error_remains_structured(self):
        sandbox = self.FakeSandbox({
            "ok": False,
            "error": {"code": "not_text", "message": "binary file"},
            "path": "binary.dat",
        })

        result = await write_file("binary.dat", content="replace", sandbox=sandbox)

        assert sandbox.writes == []
        assert result.status == "error"
        assert result.error.code == "not_text"
        assert result.data["path"] == "binary.dat"

    @pytest.mark.asyncio
    async def test_write_error_remains_structured(self):
        sandbox = self.FakeSandbox(
            {"ok": True, "content": "before"},
            {
                "ok": False,
                "error": {"code": "write_failed", "message": "read only"},
                "path": "notes.txt",
            },
        )

        result = await write_file("notes.txt", content="after", sandbox=sandbox)

        assert sandbox.writes == [("notes.txt", "after")]
        assert result.status == "error"
        assert result.error.code == "write_failed"
        assert result.data["path"] == "notes.txt"
