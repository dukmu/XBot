"""Attaching a local image to the next message.

Reading a file and encoding it is IO; deciding whether a path is an image is not.
The pure part is tested directly and the IO part against real temporary files.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from XBotv2.session.contracts import ImageInput
from XBotv2.tests.tui.factories import PNG_BYTES
from XBotv2.tui.attachments import image_media_type, load_image


def png(tmp_path: Path, name: str = "shot.png") -> Path:
    path = tmp_path / name
    path.write_bytes(PNG_BYTES)
    return path


# --- deciding what a file is ---------------------------------------------


def test_a_png_is_an_image() -> None:
    assert image_media_type(Path("shot.png")) == "image/png"


def test_a_jpeg_is_an_image() -> None:
    assert image_media_type(Path("photo.jpeg")) == "image/jpeg"
    assert image_media_type(Path("photo.JPG")) == "image/jpeg"


def test_a_text_file_is_not_an_image() -> None:
    assert image_media_type(Path("notes.txt")) is None


def test_a_file_with_no_extension_is_not_an_image() -> None:
    assert image_media_type(Path("Makefile")) is None


# --- loading --------------------------------------------------------------


def test_loading_an_image_produces_wire_input(tmp_path: Path) -> None:
    image = load_image(png(tmp_path))
    assert isinstance(image, ImageInput)
    assert image.media_type == "image/png"
    assert base64.b64decode(image.data) == PNG_BYTES


def test_loading_a_missing_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_image(tmp_path / "nope.png")


def test_loading_a_non_image_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="not an image"):
        load_image(path)
