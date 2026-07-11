# XBotv2 Plugin System

## Architecture

```
Core -> never imports -> Plugins (builtin_plugins)
Plugins -> import -> Core (xbotv2)
```

Plugins extend the engine via hooks, tools, and prompt fragments.
They live in `builtin_plugins/` and are loaded by `PluginLoader` during bootstrap.
Disable all plugins with `plugin_dirs=[]` or `--no-plugins`.

## PluginBase

```python
class PluginBase(ABC):
    async def on_load(self, config): ...     # Called at load time
    async def on_unload(self): ...           # Cleanup
    def setup(self, ctx): ...                # Register all extensions
```

Manifest-only plugins declare hooks, tools, and prompt fragments in
`plugin.yaml`. Python plugins override `setup(ctx)` and use
`ctx.register_hook`, `ctx.register_tool`, and `ctx.add_prompt_fragment`.
The setup transaction records every resource for rollback and unload.

## plugin.yaml Manifest

```yaml
name: my-plugin
version: "1.0.0"
description: What this plugin does
hooks:
  - stage: on_session_init
    handler: my_plugin.hooks:on_init
tools:
  - handler: my_plugin.tools:my_tool
    sandbox_mode: host
    execution_mode: sequential
prompt_fragments:
  - stage: system_instructions
    file: prompts/system.md
```

## Built-in Plugins

### SkillsPlugin (`builtin_plugins/skills/`)

Discovers SKILL.md files (agentskills.io format) and registers them as tools.

**Files:**
- `plugin.yaml`: manifest
- `plugin.py`: SkillsPlugin class
- `registry.py`: SkillRegistry — YAML frontmatter parsing, directory scanning
- `skill_tool.py`: `load_skill()` with `` !`cmd` `` shell injection preprocessing
- `permission_scope.py`: per-turn tool permission overrides

**Hooks:**
- `ON_SESSION_INIT`: discover SKILL.md files from 6 paths
- `BEFORE_USER_MESSAGE_ACCEPT`: detect `/skill-name` prefix, expand content
- `AFTER_CONTEXT`: inject active skill content into context
- `ON_TURN_END`: clear active skills and permission scopes
- `BEFORE_TOOL_CALL`: apply allowed-tools/disallowed-tools overrides

**Tools:**
- `skill` (namespace `plugin:skills:skill`): load a skill by name
- Each discovered skill registered as ToolRegistry entry (namespace `skills:<scope>:<name>`)

**Features:**
- Shell injection: `` !`command` `` placeholders run only through the enabled
  session sandbox. There is no host subprocess fallback.
- allowed-tools / disallowed-tools frontmatter fields
- disable-model-invocation for manual-only skills

### MCPPlugin (`builtin_plugins/mcp/`)

Connects to MCP (Model Context Protocol) servers and registers their tools.

**Files:**
- `plugin.yaml`: manifest
- `plugin.py`: MCPPlugin class
- `client.py`: MCPClient with StdioTransport and HttpTransport
- `tool.py`: MCPTool wrapper (XBotTool-compatible callable)

**Hooks:**
- `ON_SESSION_INIT`: connect to enabled MCP servers, fetch tools, register
- `ON_SESSION_CLOSE`: disconnect all servers

**Transport types:**
- `local` (stdio): subprocess with JSON-RPC over stdin/stdout
- `remote` (HTTP): HTTP POST with JSON-RPC body

**Configuration** (in `system.yaml` plugins section):
```yaml
plugins:
  mcp:
    servers:
      github:
        type: local
        command: ["npx", "-y", "@modelcontextprotocol/server-github"]
        enabled: true
```

## Tool Namespace Convention

Tools follow `source:name:tool` naming:

| Source | Name | Key | Slash |
|---|---|---|---|
| `builtin` | `core` | `builtin:core:shell` | `/shell` |
| `plugin` | plugin-name | `plugin:skills:skill` | `/skill` |
| `skills` | scope | `skills:global:find-skills` | `/find-skills` |
| `mcp` | server-name | `mcp:github:search` | `/search` |

Plugins declare namespace when registering tools:
```python
ctx.tools.register(tool, namespace="plugin:skills")
```
