"""Behavioral tests for the core filesystem tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from xbotv2.core.builtin_tools.filesystem import (
    copy_path,
    delete_path,
    edit_file,
    find_files,
    list_files,
    make_directory,
    move_path,
    patch_file,
    read_file,
    search_text,
    stat_path,
    write_file,
)
from xbotv2.tools.sandbox import SandboxPolicy


class TestFilesystemRead:
    @pytest.mark.asyncio
    async def test_reads_bounded_lines_with_optional_numbers(self, tmp_path):
        path = tmp_path / "sample.txt"
        path.write_text("a\nb\nc\nd\n", encoding="utf-8")

        result = await read_file(
            str(path), offset=1, limit=2, line_numbers=True
        )

        assert result.status == "success"
        assert result.content == "2: b\n3: c\n"
        assert result.data["line_count"] == 4
        assert result.data["returned_lines"] == 2
        assert result.data["truncated_before"] is True
        assert result.data["truncated_after"] is True
        assert result.data["next_offset"] == 3
        assert result.data["sha256"]

    @pytest.mark.asyncio
    async def test_long_single_line_can_be_resumed_by_character(self, tmp_path):
        path = tmp_path / "single-line.json"
        path.write_text("x" * 100, encoding="utf-8")

        first = await read_file(str(path), max_chars=30)
        second = await read_file(
            str(path),
            offset=first.data["next_offset"],
            char_offset=first.data["next_char_offset"],
            max_chars=30,
        )

        assert first.content == "x" * 30
        assert second.content == "x" * 30
        assert first.data["next_offset"] == 0
        assert first.data["next_char_offset"] == 30
        assert second.data["next_char_offset"] == 60

    @pytest.mark.asyncio
    async def test_non_text_file_returns_metadata_and_image_dimensions(self, tmp_path):
        path = tmp_path / "pixel.png"
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
            + (2).to_bytes(4, "big") + (3).to_bytes(4, "big")
            + b"\x00" * 8
        )

        result = await read_file(str(path))

        assert result.status == "success"
        assert result.data["is_text"] is False
        assert result.data["media_type"] == "image/png"
        assert result.data["image"] == {"format": "PNG", "width": 2, "height": 3}
        assert result.data["sha256"]
        assert "Non-text file" in result.content

    @pytest.mark.asyncio
    async def test_utf8_decodable_binary_returns_metadata_instead_of_controls(self, tmp_path):
        path = tmp_path / "controls.bin"
        path.write_bytes(b"header\x00payload")

        result = await read_file(str(path))

        assert result.status == "success"
        assert result.data["is_text"] is False
        assert result.data["media_type"] == "application/octet-stream"
        assert "\x00" not in result.content

    @pytest.mark.asyncio
    async def test_image_magic_overrides_an_uninformative_extension(self, tmp_path):
        path = tmp_path / "photo.bin"
        path.write_bytes(
            b"\xff\xd8\xff\xff\xc0\x00\x0b\x08\x00\x03\x00\x02" + b"\x00" * 6
        )

        result = await stat_path(str(path))

        assert result.data["is_text"] is False
        assert result.data["media_type"] == "image/jpeg"
        assert result.data["image"] == {"format": "JPEG", "width": 2, "height": 3}

    @pytest.mark.asyncio
    async def test_stat_reports_symlink_without_following_it(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("target", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(target.name)

        result = await stat_path(str(link))

        assert result.data["kind"] == "symlink"
        assert result.data["target"] == target.name


class TestFilesystemDiscovery:
    @pytest.mark.asyncio
    async def test_list_is_bounded_and_marks_symlinks(self, tmp_path):
        (tmp_path / "dir").mkdir()
        (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "link").symlink_to("file.txt")

        result = await list_files(str(tmp_path), max_entries=2)

        assert result.data["returned_entries"] == 2
        assert result.data["truncated"] is True
        assert {entry["kind"] for entry in result.data["entries"]} <= {
            "directory", "file", "symlink"
        }

    @pytest.mark.asyncio
    async def test_find_skips_generated_directories_and_stops_at_limit(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("pass", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "ignored.py").write_text("pass", encoding="utf-8")
        (tmp_path / "other.py").write_text("pass", encoding="utf-8")

        result = await find_files("*.py", str(tmp_path), max_results=1)

        assert result.data["returned_files"] == 1
        assert result.data["truncated"] is True
        assert "node_modules" not in result.content

    @pytest.mark.asyncio
    async def test_search_returns_structured_locations_and_clips_lines(self, tmp_path):
        (tmp_path / "a.txt").write_text("prefix ALPHA " + "x" * 30, encoding="utf-8")
        (tmp_path / "binary.dat").write_bytes(b"\xffalpha")

        result = await search_text(
            "alpha",
            str(tmp_path),
            literal=True,
            case_sensitive=False,
            max_line_chars=12,
        )

        assert result.data["returned_matches"] == 1
        match = result.data["matches"][0]
        assert match["path"] == "a.txt"
        assert match["line"] == 1
        assert match["column"] == 8
        assert match["text_truncated"] is True


class TestFilesystemMutation:
    @pytest.mark.asyncio
    async def test_write_is_atomic_and_hash_guarded(self, tmp_path):
        path = tmp_path / "code.py"
        created = await write_file(str(path), "value = 1\n")

        rejected = await write_file(
            str(path), "value = 2\n", expected_sha256="stale"
        )
        updated = await write_file(
            str(path),
            "value = 2\n",
            expected_sha256=created.data["sha256"],
        )

        assert rejected.status == "error"
        assert rejected.error.code == "content_changed"
        assert updated.data["changed"] is True
        assert path.read_text(encoding="utf-8") == "value = 2\n"

    @pytest.mark.asyncio
    async def test_exact_edit_rejects_ambiguous_match(self, tmp_path):
        path = tmp_path / "code.py"
        path.write_text("name = 1\nname = 2\n", encoding="utf-8")

        ambiguous = await edit_file(str(path), "name", "value")
        replaced = await edit_file(
            str(path), "name", "value", replace_all=True
        )

        assert ambiguous.status == "error"
        assert ambiguous.error.code == "ambiguous_edit"
        assert replaced.data["replacements"] == 2
        assert path.read_text(encoding="utf-8") == "value = 1\nvalue = 2\n"

    @pytest.mark.asyncio
    async def test_patch_uses_system_parser_and_rejects_bad_hunks(self, tmp_path):
        path = tmp_path / "code.py"
        path.write_text("one\ntwo\n", encoding="utf-8")
        valid = (
            "--- a/code.py\n+++ b/code.py\n"
            "@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"
        )
        invalid = (
            "--- a/code.py\n+++ b/code.py\n"
            "@@ -1,9 +1,1 @@\n missing\n"
        )

        applied = await patch_file(str(path), valid)
        rejected = await patch_file(str(path), invalid)

        assert applied.status == "success"
        assert rejected.status == "error"
        assert rejected.error.code == "patch_failed"
        assert path.read_text(encoding="utf-8") == "one\nTWO\n"

    @pytest.mark.asyncio
    async def test_directory_lifecycle(self, tmp_path):
        source = tmp_path / "source"
        made = await make_directory(str(source))
        (source / "file.txt").write_text("content", encoding="utf-8")
        copied = tmp_path / "copied"
        moved = tmp_path / "moved"

        copy_result = await copy_path(str(source), str(copied))
        move_result = await move_path(str(copied), str(moved))
        delete_result = await delete_path(str(moved), recursive=True)

        assert made.data["created"] is True
        assert copy_result.data["copied"] is True
        assert move_result.data["moved"] is True
        assert delete_result.data["deleted"] is True
        assert source.exists()
        assert not moved.exists()


class TestFilesystemSandboxContract:
    @pytest.mark.asyncio
    async def test_host_and_bwrap_return_the_same_read_contract(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        policy = SandboxPolicy(workspace_root=workspace)
        if not policy.backend_available:
            pytest.skip("bubblewrap is not installed")

        host = await read_file("sample.txt", sandbox=SandboxPolicy(
            enabled=False, workspace_root=workspace
        ))
        isolated = await read_file("sample.txt", sandbox=policy)

        assert isolated.content == host.content
        for key in ("is_text", "line_count", "sha256", "truncated_after"):
            assert isolated.data[key] == host.data[key]

    @pytest.mark.asyncio
    async def test_real_bwrap_mutation_lifecycle(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        policy = SandboxPolicy(workspace_root=workspace)
        if not policy.backend_available:
            pytest.skip("bubblewrap is not installed")

        assert (await make_directory("tree", sandbox=policy)).status == "success"
        created = await write_file("tree/code.py", "one\n", sandbox=policy)
        edited = await edit_file("tree/code.py", "one", "two", sandbox=policy)
        patched = await patch_file(
            "tree/code.py",
            "--- a/code.py\n+++ b/code.py\n@@ -1 +1 @@\n-two\n+TWO\n",
            sandbox=policy,
        )
        copied = await copy_path("tree/code.py", "copy.py", sandbox=policy)
        moved = await move_path("copy.py", "moved.py", sandbox=policy)
        deleted_file = await delete_path("moved.py", sandbox=policy)
        deleted_tree = await delete_path("tree", recursive=True, sandbox=policy)

        assert all(result.status == "success" for result in (
            created, edited, patched, copied, moved, deleted_file, deleted_tree,
        ))
        assert not (workspace / "tree").exists()
        assert not (workspace / "moved.py").exists()

    @pytest.mark.asyncio
    async def test_workspace_write_deny_is_enforced_before_execution(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        policy = SandboxPolicy(
            config={"workspace_read": "allow", "workspace_write": "deny"},
            workspace_root=workspace,
        )

        issues = policy.check_tool_access(
            "filesystem_write", {"path": "blocked.txt", "content": "no"}
        )
        mounts = [
            mount for mount in policy._mount_specs()
            if mount.target == workspace.resolve()
        ]

        assert issues[0]["decision"] == "deny"
        assert mounts[0].access == "readonly"

    @pytest.mark.asyncio
    async def test_session_mount_rejects_writes(self, tmp_path):
        workspace = tmp_path / "workspace"
        session = tmp_path / "data" / "session"
        workspace.mkdir()
        session.mkdir(parents=True)
        policy = SandboxPolicy(workspace_root=workspace, session_root=session)

        issues = policy.check_tool_access(
            "filesystem_write", {"path": "session/state.txt", "content": "no"}
        )

        assert issues == [{
            "field": "path",
            "path": str((session / "state.txt").resolve()),
            "write": True,
            "decision": "deny",
        }]
