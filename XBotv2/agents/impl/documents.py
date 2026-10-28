"""Editing the source documents owned by the Agent catalog."""
from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import yaml
from XBotv2.agents.impl.loader import _parse_source, load_definition
from XBotv2.persistence.api.files import write_text_atomic


def agent_path(ctx, scope: Literal["global", "workspace"], name: str) -> Path:
    # AgentDefinition validates names before they are used as path components.
    import re
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("Invalid Agent name")
    directory = Path(ctx.data_root if scope == "global" else ctx.workspace_root) / ".agents"
    return directory / f"{name}.md"


def revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def read_document(ctx, catalog, scope: Literal["global", "workspace"], name: str) -> dict:
    path = agent_path(ctx, scope, name)
    source = path
    if not source.exists() and scope == "workspace":
        source = agent_path(ctx, "global", name)
    if not source.exists() and scope == "global" and catalog.get_base(name) is None:
        source = agent_path(ctx, "workspace", name)
    if source.exists():
        metadata, prompt = _parse_source(source)
    else:
        definition = catalog.get_base(name)
        if definition is None:
            raise ValueError(f"Unknown Agent: {name}")
        metadata = definition.model_dump(mode="json", exclude={"name", "prompt"})
        prompt = definition.prompt
    return {"name": name, "scope": scope, "revision": revision(path), "metadata": metadata, "prompt": prompt}


def write_document(ctx, scope: Literal["global", "workspace"], name: str, expected: str, metadata: dict, prompt: str, *, targets):
    path = agent_path(ctx, scope, name)
    if revision(path) != expected:
        raise FileExistsError("Agent definition changed on disk; reload before saving")
    content = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + prompt + "\n"
    with TemporaryDirectory() as directory:
        candidate = Path(directory) / f"{name}.md"
        candidate.write_text(content, encoding="utf-8")
        definitions = [load_definition(candidate, target.variables) for target in targets]
    write_text_atomic(path, content)
    return definitions
