# Agents

The agents plugin owns Agent definitions, the definition catalog and each thread's selected Agent and resolved runtime configuration. `AGENT_CATALOG` and `AGENT_RUNTIME` in `api/contracts.py` expose those services; `AGENT_CONFIGURED` in `api/events.py` announces resolved selection. `api/protocol.py` defines HTTP transport models.

It depends on configuration, LLM selection, permissions, Agent loop and core domain contracts. Catalog construction reads built-in and workspace Agent definitions; selections may construct providers and update runtime state. The mounted catalog holds loaded definitions; selection is thread scoped. The runtime application root and loop factory remain outside this migration. Agent definitions describe prompt, model and tool policies, execution limits and permissions. Built-in definitions are followed by data-root `.agents` definitions and workspace `.agents` overlays at catalog mount. An `AgentDefinition` defaults to subagent mode, inherited model routing and generation, all tools enabled, and an allow-default Agent permission policy; configured policies and selection are resolved when the runtime is created or changed.

Commands and HTTP routes call the same runtime operations; `/agent` lists or selects a definition.

```python
from XBotv2.agents.api.contracts import AGENT_CATALOG, AGENT_RUNTIME

definition = ctx.require(AGENT_CATALOG).get("default")
selection = await ctx.require(AGENT_RUNTIME).select("default")
```
