"""Test construction through the same process and session APIs as the CLI."""
from pathlib import Path
import yaml
from XBotv2.main import open_runtime
from XBotv2.loader.api.contracts import PluginOverlay
from XBotv2.session.api.contracts import OpenSession, OpenThread, SESSIONS


async def thread_scope(*, paths, provider_name=None, session_id=None, thread_id="main",
                       workspace_root=None, no_plugins=False, plugin_dirs=(),
                       llm_override=None, selected_agent=None, agent_definition=None,
                       extra_plugins=None, interactive=True, mode="new",
                       parent_thread_id=""):
    workspace = Path(workspace_root or paths.data_dir.parent / "workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if agent_definition is not None:
        directory = workspace / ".agents"
        directory.mkdir(exist_ok=True)
        metadata = agent_definition.model_dump(mode="json", exclude={"name", "prompt"})
        (directory / (agent_definition.name + ".md")).write_text("---\n" + yaml.safe_dump(metadata) + "---\n" + agent_definition.prompt)
        selected_agent = agent_definition.name
    if extra_plugins:
        directory = workspace / ".xbot"
        directory.mkdir(exist_ok=True)
        target = directory / "plugins.yaml"
        existing = yaml.safe_load(target.read_text()) if target.exists() else []
        from XBotv2.loader.api.tree import DEFAULT_TREE
        from XBotv2.loader.api.contracts import PluginTree
        tree = PluginTree.from_yaml(DEFAULT_TREE).patched_with(PluginOverlay.parse(existing or []))
        tree = tree.patched_with(PluginOverlay.parse(extra_plugins))
        extra_plugins = [{"id": e.id, "name": e.name, "config": e.config, "disabled": e.disabled, "optional": e.optional} for e in tree.entries]
        target.write_text(yaml.safe_dump(extra_plugins))
    root = await open_runtime(paths=paths, plugin_dirs=plugin_dirs)
    try:
        manager = root.require(SESSIONS)
        if parent_thread_id:
            return await manager.open_thread(OpenThread(
                session_id=session_id, thread_id=thread_id, parent_thread_id=parent_thread_id,
                workspace_root=str(workspace) if workspace_root is not None else None,
                provider_name=provider_name, selected_agent=selected_agent, mode=mode,
                no_plugins=no_plugins, model_override=llm_override,
            ))
        return await manager.open(OpenSession(
            session_id=session_id, thread_id=thread_id,
            workspace_root=str(workspace) if mode == "new" or workspace_root is not None else None,
            provider_name=provider_name, selected_agent=selected_agent,
            no_plugins=no_plugins, model_override=llm_override, mode=mode,
            interactive=interactive,
        ))
    except BaseException:
        await root.close()
        raise


def engine_of(scope):
    """Concrete algorithm under test; public lifecycle tests use AGENT_LOOP."""
    from XBotv2.agentloop.api.contracts import AGENT_LOOP
    return scope.require(AGENT_LOOP)._engine
