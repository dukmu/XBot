# XBot Built-In Components

Primary sources in a checkout: `XBotv2/xcore.yaml` and
`XBotv2/docs/plugins/`. With pip/uv, inspect the installed package's bundled
`xcore.yaml` (when present) and use the version-matched XBot documentation.
Reuse these capabilities instead of reimplementing a parallel path.

| Component | Use it for | Useful public surface |
|---|---|---|
| `coretools` | filesystem and Shell operations | `ctx.tools`, session-bound `read`/`edit`/`path`/`search` and Shell factories |
| `permissions` | allow/deny/ask policy | permission port, guards, typed permission events |
| `sandbox` | filesystem/process/network capability policy | sandbox service; never assume host access |
| `interactions` | `ask_user` and client input | interaction service and `user_input_required` flow |
| `commands` | slash commands for humans | `ctx.commands.register(Command(...))` |
| `context_builder` / `prompts` | system/context fragments | typed context components and prompt APIs |
| `persistence` / `usage` | message state and usage projection | storage/usage services; keep runtime state separate |
| `jobs` | background Shell/subagent lifecycle | JobRegistry port and runtime event path |
| `skills` | SKILL.md discovery and activation | typed initialization event, namespaced skill Tools |
| `browser` | public Web and isolated Chromium | normal Tools, network policy, session artifacts |
| `compact` | history compaction | compact Tool/command and typed history events |
| `goal` / `todolist` | durable objective/checklist UX | state namespaces plus standard Tools/commands |
| `mcp` | MCP server tools/resources/prompts | MCP plugin registration and client adapter |

The permission plugin owns policy decisions; a new Tool must not skip the
guard pipeline. The sandbox plugin owns capability checks; a plugin should not
call host subprocess/file APIs when a built-in capability is the requirement.
The interaction plugin owns waiting and cancellation; do not create a private
client event or waiter.

For component-specific behavior, read the corresponding page in
`XBotv2/docs/plugins/` before depending on it. Check the current tree because
profiles and disabled entries can change what is mounted in a given process.
