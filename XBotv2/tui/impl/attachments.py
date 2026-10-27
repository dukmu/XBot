"""Turning a local file into something the server accepts as an image.

The wire contract is the repository's own ``ImageInput``; reading a file and
encoding it is the only thing that happens here. Deciding *whether* a path is an
image is a pure function so it can be tested without touching the filesystem.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from XBotv2.session.contracts import ImageInput


def image_media_type(path: Path) -> str | None:
    """The media type of an image file, or ``None`` when it is not one.

    Deliberately open: anything the platform calls ``image/*`` is accepted, and
    the wire model (``ImageInput``) is what enforces the shape.
    """
    media_type, _encoding = mimetypes.guess_type(path.name)
    if not media_type or not media_type.startswith("image/"):
        return None
    return media_type


def load_image(path: Path) -> ImageInput:
    """Read an image file into the shape the client sends."""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    media_type = image_media_type(path)
    if media_type is None:
        raise ValueError(f"{path} is not an image")
    return ImageInput(
        data=base64.b64encode(path.read_bytes()).decode("ascii"),
        media_type=media_type,
    )


__all__ = [
    "image_media_type",
    "load_image",
]
