"""Behavioral tests for the core filesystem XBotv2.tools."""

from __future__ import annotations

import base64
from pathlib import Path

import json
import pytest

from XBotv2.coretools import filesystem as filesystem_module
from XBotv2.coretools.filesystem import (
    read,
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
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.core.filesystem.artifacts import ArtifactStore
from XBotv2.core.paths import RuntimePaths


def _artifact_store(tmp_path: Path) -> ArtifactStore:
    paths = RuntimePaths.from_data_dir(tmp_path / "data")
    return ArtifactStore(paths.session("test").thread("agent"))


class TestReadImage:
    @pytest.mark.asyncio
    async def test_read_image_path_returns_image_part(self, tmp_path):
        payload = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
            "AAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
        )
        path = tmp_path / "image.png"
        path.write_bytes(payload)

        class Sandbox:
            session_root = tmp_path / "session"
            enabled = False
            network = True

            def resolve_filesystem_args(self, _operation, args):
                return args

        artifacts = _artifact_store(tmp_path)
        result = await read(
            path=str(path), mode="media", sandbox=Sandbox(), artifacts=artifacts
        )

        assert result.status == "success"
        assert len(result.images) == 1
        image = result.images[0]
        assert image.media_type == "image/png"
        assert artifacts.read(image.path) == payload

    @pytest.mark.asyncio
    async def test_read_image_accepts_base64_and_data_url(self, tmp_path):
        payload = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
            "AAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
        )
        encoded = base64.b64encode(payload).decode("ascii")

        class Sandbox:
            session_root = tmp_path / "session"
            enabled = False
            network = True

        artifacts = _artifact_store(tmp_path)
        raw = await read(
            path="", mode="media", data=encoded,
            sandbox=Sandbox(), artifacts=artifacts,
        )
        data_url = await read(
            path="",
            mode="media",
            data=f"data:image/png;base64,{encoded}",
            sandbox=Sandbox(),
            artifacts=artifacts,
        )

        assert raw.status == "success"
        assert data_url.status == "success"
        assert raw.images[0].media_type == "image/png"
        assert data_url.images[0].size == len(payload)

    @pytest.mark.asyncio
    async def test_read_image_url_uses_http_response(self, tmp_path, monkeypatch):
        payload = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
            "AAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
        )

        class Response:
            status_code = 200
            headers = {"content-type": "image/png"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def aiter_bytes(self):
                yield payload

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, _method, _url, **_kwargs):
                return Response()

        monkeypatch.setattr(filesystem_module.httpx, "AsyncClient", Client)

        class Sandbox:
            session_root = tmp_path / "session"
            enabled = False
            network = True

        result = await read(
            path="",
            mode="media",
            url="https://example.com/cat.png",
            sandbox=Sandbox(),
            artifacts=_artifact_store(tmp_path),
        )

        assert result.status == "success"
        assert result.images[0].media_type == "image/png"

    @pytest.mark.asyncio
    async def test_read_image_rejects_unsupported_content(self, tmp_path):
        class Sandbox:
            session_root = tmp_path / "session"
            enabled = False
            network = True

        result = await read(
            path="",
            mode="media",
            data="bm90IGFuIGltYWdl",
            sandbox=Sandbox(),
        )

        assert result.status == "error"
        assert result.error.code == "unsupported_content"


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

    @pytest.mark.asyncio
    async def test_long_single_line_can_be_resumed_by_character(self, tmp_path):
        path = tmp_path / "single-line.json"
        path.write_text("x" * 100, encoding="utf-8")

        first = await read_file(str(path), max_chars=30)
        second = await read_file(str(path), offset=0, char_offset=30, max_chars=30)

        assert first.content == "x" * 30
        assert second.content == "x" * 30

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
        assert "Non-text file" in result.content

    @pytest.mark.asyncio
    async def test_non_text_read_does_not_attach_image_content(self, tmp_path):
        path = tmp_path / "pixel.png"
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
            + (2).to_bytes(4, "big") + (3).to_bytes(4, "big")
            + b"\x00" * 8
        )

        class Sandbox:
            session_root = tmp_path / "session"
            enabled = False

            def resolve_filesystem_args(self, _operation, args):
                return args

        result = await read_file(str(path), sandbox=Sandbox())

        assert result.status == "success"
        assert result.images == ()

    @pytest.mark.asyncio
    async def test_utf8_decodable_binary_returns_metadata_instead_of_controls(self, tmp_path):
        path = tmp_path / "controls.bin"
        path.write_bytes(b"header\x00payload")

        result = await read_file(str(path))

        assert result.status == "success"
        assert "\x00" not in result.content

    @pytest.mark.asyncio
    async def test_image_magic_overrides_an_uninformative_extension(self, tmp_path):
        path = tmp_path / "photo.bin"
        path.write_bytes(
            b"\xff\xd8\xff\xff\xc0\x00\x0b\x08\x00\x03\x00\x02" + b"\x00" * 6
        )

        result = await stat_path(str(path))
        data = __import__("json").loads(result.content)

        assert data["is_text"] is False
        assert data["media_type"] == "image/jpeg"
        assert data["image"] == {"format": "JPEG", "width": 2, "height": 3}

    @pytest.mark.asyncio
    async def test_stat_reports_symlink_without_following_it(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("target", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(target.name)

        result = await stat_path(str(link))
        data = __import__("json").loads(result.content)

        assert data["kind"] == "symlink"
        assert data["target"] == target.name

    @pytest.mark.asyncio
    async def test_list_is_bounded_and_marks_symlinks(self, tmp_path):
        (tmp_path / "dir").mkdir()
        (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "link").symlink_to("file.txt")

        complete = await list_files(str(tmp_path))
        bounded = await list_files(str(tmp_path), max_entries=2)
        complete_data = json.loads(complete.content)
        bounded_data = json.loads(bounded.content)

        kinds = {entry["name"]: entry["kind"] for entry in complete_data["entries"]}
        assert kinds["link"] == "symlink"
        assert bounded_data["returned_entries"] == 2
        assert bounded_data["truncated"] is True

    @pytest.mark.asyncio
    async def test_find_skips_generated_directories_and_stops_at_limit(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("pass", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "ignored.py").write_text("pass", encoding="utf-8")
        (tmp_path / "other.py").write_text("pass", encoding="utf-8")

        bounded = await find_files("*.py", str(tmp_path), max_results=1)
        complete = await find_files("*.py", str(tmp_path), max_results=10)
        bounded_data = json.loads(bounded.content)
        complete_data = json.loads(complete.content)

        assert bounded_data["truncated"] is True
        assert set(complete_data["files"]) == {"other.py", "src/app.py"}

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
        data = json.loads(result.content)

        assert data["returned_matches"] == 1
        match = data["matches"][0]
        assert match["path"] == "a.txt"
        assert match["line"] == 1
        assert match["column"] == 8
        assert match["text_truncated"] is True

    @pytest.mark.asyncio
    async def test_search_accepts_one_file(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("alpha\nbeta alpha\n", encoding="utf-8")

        result = await search_text("alpha", str(path), literal=True)
        data = json.loads(result.content)

        assert data["kind"] == "file"
        assert data["returned_matches"] == 2
        assert data["matches"][0]["path"] == str(path)
        assert data["matches"][1]["line"] == 2


class TestFilesystemMutation:
    @pytest.mark.asyncio
    async def test_runtime_guards_files_read_before_mutation(self, tmp_path):
        import inspect

        path = tmp_path / "code.py"
        path.write_text("value = 1\n", encoding="utf-8")
        policy = SandboxPolicy(workspace_root=tmp_path, enabled=False)

        assert "expected_sha256" not in inspect.signature(write_file).parameters
        await read_file("code.py", sandbox=policy)
        path.write_text("value = external\n", encoding="utf-8")

        rejected = await write_file(
            "code.py", "value = agent\n", sandbox=policy
        )
        assert rejected.status == "error"
        assert rejected.error.code == "content_changed"
        reread = await read_file("code.py", sandbox=policy)
        updated = await write_file(
            "code.py", "value = agent\n", sandbox=policy
        )
        assert reread.status == "success"
        assert reread.content.startswith("File changed since the previous read.")
        assert updated.status == "success"
        assert path.read_text(encoding="utf-8") == "value = agent\n"

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
        replaced_data = json.loads(replaced.content)
        assert replaced_data["replacements"] == 2
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

        assert made.status == "success"
        assert copy_result.status == "success"
        assert move_result.status == "success"
        assert delete_result.status == "success"
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

        assert isolated.status == "success"
        assert isolated.content == host.content

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
