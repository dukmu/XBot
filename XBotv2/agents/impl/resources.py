"""Agent-owned editable definition resources."""
from __future__ import annotations

import re
from pathlib import Path
from tempfile import TemporaryDirectory
import yaml
from XBotv2.agentloop.api.contracts import AGENT_LOOP
from XBotv2.agents.api.contracts import AGENT_CATALOG
from XBotv2.agents.impl.loader import load_definition
from XBotv2.config.api.resources import ConfigurationDocument, ConfigurationResource
from XBotv2.persistence.api.files import write_text_atomic


def agent_path(ctx, scope, name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("Invalid Agent name")
    directory = Path(ctx.data_root if scope == "global" else ctx.workspace_root) / ".agents"
    return directory / f"{name}.md"


def _file_revision(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return "missing"
    return f"{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"


def _source(ctx, catalog, scope, name):
    target = agent_path(ctx, scope, name)
    source = target
    if not source.exists() and scope == "workspace":
        source = agent_path(ctx, "global", name)
    if not source.exists() and scope == "global" and catalog.get_base(name) is None:
        source = agent_path(ctx, "workspace", name)
    return target, source


class AgentResources:
    def __init__(self, sessions):
        self.sessions = sessions

    async def catalog(self, ctx):
        return tuple(
            ConfigurationResource(id=f"agent:{definition.name}", title=definition.name,
                                  scopes=("global", "workspace"), format="markdown",
                                  description="Changes update the catalog; select the Agent again to apply its definition.")
            for definition in ctx.require(AGENT_CATALOG).definitions()
        )

    async def read(self, ctx, resource_id, scope):
        name = resource_id.removeprefix("agent:")
        catalog = ctx.require(AGENT_CATALOG)
        target, source = _source(ctx, catalog, scope, name)
        if source.exists():
            content = source.read_text(encoding="utf-8")
        else:
            definition = catalog.get_base(name)
            if definition is None:
                raise ValueError(f"Unknown Agent: {name}")
            metadata = definition.model_dump(mode="json", exclude={"name", "prompt"})
            content = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + definition.prompt + "\n"
        token = f"{_file_revision(target)}|{_file_revision(source)}"
        return ConfigurationDocument(id=resource_id, scope=scope, revision=token, content=content)

    async def save(self, ctx, document):
        if not isinstance(document.content, str):
            raise ValueError("Agent definition must be Markdown text")
        name = document.id.removeprefix("agent:")
        async with ctx.require(AGENT_LOOP).idle("edit Agent definition"):
            targets = await self.sessions.scopes(ctx.thread_info.session_id)
            catalogs = [target.require(AGENT_CATALOG) for target in targets]
            path, source = _source(ctx, ctx.require(AGENT_CATALOG), document.scope, name)
            if document.revision != f"{_file_revision(path)}|{_file_revision(source)}":
                raise FileExistsError("Agent definition changed on disk; reload before saving")
            with TemporaryDirectory() as directory:
                candidate = Path(directory) / f"{name}.md"
                candidate.write_text(document.content, encoding="utf-8")
                try:
                    definitions = [load_definition(candidate, target.variables) for target in targets]
                except yaml.YAMLError as exc:
                    raise ValueError(f"Invalid Agent frontmatter: {exc}") from exc
            write_text_atomic(path, document.content)
            for catalog, definition in zip(catalogs, definitions, strict=True):
                catalog.replace_file_definition(definition, overlay=document.scope == "workspace")
        return await self.read(ctx, document.id, document.scope)
