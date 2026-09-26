# Built-in plugins

This page is only the navigation map. The complete contracts, dependencies,
effects, state ownership, client/server facets, and examples are maintained in
the skill's [plugin index](../.agents/skills/xbot-plugin-development/references/plugins_list.md)
and [per-plugin references](../.agents/skills/xbot-plugin-development/references/plugins/README.md).

The bundled tree is `XBotv2/xcore.yaml`. `id` is the overlay identity and
`name` is the import name. Profiles determine which carrier can mount an
entry; activation itself is dependency-driven.

| id/name | profiles | primary responsibility | important injected services |
|---|---|---|---|
| config | agent, server | settings, policy and configuration routes | runtime paths, launch, plugin overrides/dirs, server/sessions |
| client_transport | client | HTTP API client bound to the resolved client launch | client launch |
| tui | client | Textual terminal client and local command presentation | client API, client launch, commands |
| persistence | agent, server, acp | history/state/artifact hydration and reader factory | loop state, thread persistence, runtime log |
| usage | agent | normalized usage snapshot and events | state, loop state, runtime log |
| agents | agent, server | Agent catalog, selection, engine creation | catalog, loop factory, LLM, tools, agent inbox, sessions |
| session | agent, server, acp | SessionManager, thread runtime, history routes | launch, paths, artifacts, commands, application factory |
| jobs | agent, server | shell/subagent job registry | commands, engine, sessions |
| commands | agent, server, client | human command catalog and dispatch | sessions/server where mounted |
| llm | agent, server | provider/model directory and selection | runtime log, agent runtime, sessions |
| agentloop | agent, server | Tool registry and AgentLoop factory | runtime log, session launch, sessions |
| context_builder | agent | provider-facing context compiler and prompt components | runtime log, artifacts |
| prompts | agent | prompt component registry | context builder |
| sandbox | agent | filesystem/process/network ceiling | paths, session, tools, settings |
| permissions | agent | regex Tool policy and approval flow | tools, interactions, state, settings |
| coretools | agent | filesystem and shell Tools; workspace extension hooks | tools, session, sandbox, artifacts, jobs, workspace root |
| subagents | agent | child Agent Tools and jobs | child applications, catalog, permissions, jobs, tools |
| goal | agent | durable objective and `/goal` | tools, loop state, commands, engine, model, state, usage |
| todolist | agent, server | atomic checklist snapshot | tools/state or sessions |
| skills | agent | SKILL.md discovery and prompt/tool activation | tools, commands, sandbox |
| mcp_plugin | agent | MCP server tool/resource/prompt bridges | tools, model, interactions, session, usage, loop state |
| content_cache | agent | lossless externalization of oversized current user input and Tool output text | artifacts |
| compact | agent | append-only semantic history replacement | tools, commands, model, loop state |
| browser | agent | web research and isolated browser Tools | tools, sandbox, artifacts |
| token_manager | agent | request/context observation | session |
| workspace_instructions | agent | AGENTS.md context contribution | variables, workspace root |
| interactions | agent | ask-user and client input waiters | tools, client events, session launch |
| caption | agent | session title Tool and automatic first-message caption | tools, model, loop state, session, agent options, usage |
| workspaces | server, acp | workspace/session catalog and directory API | sessions, state, workspace root |
| acp_plugin | acp | ACP carrier | sessions, ACP launch, runtime log |
| server | server | FastAPI carrier and health/hello | runtime log |

## Composition pattern

The root `plugin.py` is the only registration object. It can define named
dependency-gated mount functions for catalog, runtime, commands, and HTTP.
Routers are contributed through `server.contribute_router`; a route facet is
not a second plugin. A plugin's `apply` should be short and declarative:

```python
class ExamplePlugin:
    name = "example"

    async def apply(self, ctx: Context, config=None) -> None:
        await ctx.inject(["tools", "settings"], mount_runtime)
        await ctx.inject(["server"], mount_http)

plugin = ExamplePlugin()
```
