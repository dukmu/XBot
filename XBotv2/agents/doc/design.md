# Agents

The agents plugin owns Agent definitions, the definition catalog and each thread's selected Agent and resolved runtime configuration. `AGENT_CATALOG` and `AGENT_RUNTIME` in `api/contracts.py` expose those services; `AGENT_CONFIGURED` in `api/events.py` announces resolved selection. `api/protocol.py` defines HTTP transport models.

It depends on configuration, LLM selection, permissions, Agent loop and core domain contracts. Catalog construction reads built-in and workspace Agent definitions; selections may construct providers and update runtime state. The mounted catalog holds loaded definitions; selection is thread scoped. The process has one root; agentloop constructs Engine, while agents owns only definition binding and selection. Agent definitions describe prompt, model and tool policies, execution limits and permissions. Built-in definitions are followed by data-root `.agents` definitions and workspace `.agents` overlays at catalog mount. An `AgentDefinition` defaults to subagent mode, inherited model routing and generation, all tools enabled, and an allow-default Agent permission policy; configured policies and selection are resolved when the runtime is created or changed.

Commands and HTTP routes call the same runtime operations; `/agent` lists or selects a definition.

```python
from XBotv2.agents.api.contracts import AGENT_CATALOG, AGENT_RUNTIME

definition = ctx.require(AGENT_CATALOG).get("default")
selection = await ctx.require(AGENT_RUNTIME).select("default")
```

Agent Markdown parsing is shared by a process-owned cache; each thread expands runtime variables separately and keeps its own definitions. The cache has a fixed 4 MiB accounting budget and uses least-recently-used eviction. Its per-source charge is 64 times the source file's UTF-8 byte size plus the shallow Python sizes of the path, parsed result, metadata mapping and prompt. This conservative accounting is an estimate, not a measurement of the full Python heap. Files larger than the available budget are parsed without retention. A changed `(mtime_ns, size)` replaces the prior entry; a directory reload removes entries for deleted Markdown files, and process close clears the cache. Standalone loader calls without an explicitly supplied `AgentSources` do not cache. Focused tests exercise 100-byte, 7,000-byte and 100,000-byte budgets.
