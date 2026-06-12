# Stage 5: Unified Command System & Namespace Protocol

## 命名空间协议

### 格式：`source:name:tool`

| 层级 | 含义 | 约束 |
|---|---|---|
| `source` | 来源类型 | `builtin` / `mcp` / `plugin` / `skills` / `custom` |
| `name` | 来源标识 | 该来源的具体实体名或路径 |
| `tool` | slash 命令名 | **全局唯一**，用户可见的 `/` 命令名 |

### slash 命令名规则

- `tool` 层级为首选命令名（如 `/shell`、`/find-skills`）
- 若 `tool` 全局唯一 → 直接用
- 若 `tool` 与其他来源重复 → 使用完整标识符 `source:name:tool`

### 各来源约定

| source | name 约定 | 示例 |
|---|---|---|
| `builtin` | 固定 `core` | `builtin:core:shell` → `/shell` |
| `mcp` | MCP server 名 | `mcp:github:search_repos` → `/search_repos` |
| `plugin` | 插件名 | `plugin:skills:skill` → `/skill` |
| `skills` | skill 来源路径 | `skills:~/.agents/skills/find-skills:find-skills` → `/find-skills` |
| `custom` | 用户自定义 | `custom:~/bin:deploy` → `/deploy` |

### 重复示例

```
mcp:github:search     → /mcp:github:search      (与 mcp:sentry:search 冲突)
mcp:sentry:search     → /mcp:sentry:search      (与 mcp:github:search 冲突)
plugin:exp:skill      → /plugin:exp:skill        (与 plugin:skills:skill 冲突)
```

### `restrict` 选择器

| 选择器 | 匹配 |
|---|---|
| `shell` | `builtin:core:shell`（bare 默认 builtin） |
| `filesystem*` | `builtin:core:filesystem_*`（prefix 匹配 tool 层） |
| `builtin:*:*` | 所有内置工具 |
| `mcp:*:*` | 所有 MCP 工具 |
| `plugin:skills:*` | SkillsPlugin 提供的工具 |
| `skills:*:*` | 所有 SKILL.md skills |
| `skills:bundled:*` | 内置 skills |
| `*:*:*` 或空 | 全部工具 |

### `system.yaml`

```yaml
tools:
  - shell
  - filesystem
  - skills:*:*
  - mcp:*:*
  - plugin:skills:*
```


## 架构总览

```
┌─ TUI ──────────────────────────────────────────────────────────┐
│  /help, /exit, /clear  → 本地处理                              │
│  其他所有 command      → POST /sessions/{id}/commands           │
│                          {command, raw, kind}                   │
└──────────────────────────┬─────────────────────────────────────┘
                           │
                           ▼
┌─ HTTP Server ──────────────────────────────────────────────────┐
│  GET /sessions/{id}/commands                                   │
│    → list_commands() + _tool_commands(tool_registry)           │
│    → 枚举 ToolRegistry 所有 entry → 按 namespace 映射 kind     │
│                                                                 │
│  POST /sessions/{id}/commands                                  │
│    → execute_command(ctx, command, args, kind)                 │
│    → kind=server: status/provider/permission/sandbox           │
│    → kind=skill:   tool_registry.get(name).invoke() → 返回内容  │
│    → kind=tool/mcp: 返回工具描述                                │
└─────────────────────────────────────────────────────────────────┘
```

### Command 来源

| 来源 | kind 标签 | 注册方式 |
|---|---|---|
| Client 常量 | `[client cmd]` | `tui/command.py` `_COMMANDS` 硬编码 |
| Server 常量 | `[server cmd]` | `protocol/commands.py` `COMMANDS` 硬编码 |
| ToolRegistry(builtin) | `[tool]` | bootstrap 注册时 namespace=`builtin` |
| ToolRegistry(plugin) | `[tool]` | 插件 `register_tools(namespace="skills")` |
| ToolRegistry(skills) | `[skill]` | SkillsPlugin 将 SKILL.md 注册为 ToolRegistry entry, namespace=`skills` |
| ToolRegistry(mcp) | `[mcp]` | MCPPlugin 注册时 namespace=`mcp.<server>` |

### `_tool_commands(reg)` — 枚举 ToolRegistry 为 command 描述符

```python
def _tool_commands(reg):
    ns_to_kind = {"builtin": "tool", "skills": "skill"}
    for full_name in reg.registered_names():
        entry = reg._entries.get(full_name)
        ns = entry.namespace
        kind = ns_to_kind.get(ns, "mcp" if ns.startswith("mcp.") else "tool")
        display = full_name.split(":", 1)[-1] if ":" in full_name else full_name
        desc = getattr(entry.tool, "description", "") or display
        yield {"name": display, "kind": kind, "description": desc}
```


## 数据流

### Skill 调用

```
用户输入 /find-skills 找一个github技能
        │
        ▼ TUI: parse_slash_command → kind="skill", name="find-skills", args="找一个github技能"
        │
        ▼ POST /sessions/{id}/commands {command:"find-skills", kind:"skill", raw:"/find-skills 找一个github技能"}
        │
        ▼ Server: execute_command(kind="skill")
            → ctx.engine.tool_registry.get("find-skills") → ToolEntry
            → entry.tool.invoke({}) → SKILL.md 完整 Markdown 内容
            → 拼接指令 "## Instructions\n找一个github技能"
            → 返回 {status:"ok", message: "## find-skills\n\n<SKILL.md body>\n\n## Instructions\n找一个github技能"}
        │
        ▼ TUI: display as notice — 用户看到 skill 内容
```

### 工具调用

```
用户输入 /shell ls -la
        │
        ▼ TUI: kind="tool" → POST /sessions/{id}/commands
        │
        ▼ Server: execute_command(kind="tool")
            → 返回工具描述，用户可将结果用于下一轮对话
```


## CommandSpec

```python
CommandKind = Literal["client", "server", "skill", "tool", "mcp"]

@dataclass(frozen=True)
class CommandSpec:
    name: str                      # slash 命令名（不含 /）
    kind: CommandKind              # 命令类型
    description: str               # 补全时显示的描述
    args: str = ""                 # / 之后的原始参数
    raw: str = ""                  # 原始输入文本
    display_label: str = ""        # 完整显示标签
    short_label: str = ""          # 补全弹窗短标签
    parameters: dict[str, str] = field(default_factory=dict)
```

补全显示：
```
/shell            [tool]        Execute a shell command
/find-skills      [skill]       Helps users discover and install agent skills
/search_repos     [mcp]         Search GitHub repositories
/status           [server cmd]  显示服务器和当前 session 状态
/help             [client cmd]  显示帮助信息
```


## `/help [name]` 增强

无参数：列出所有 commands 及其类型标签。
有参数：显示指定 command 的 `description` + `parameters` + `kind` 标签。


## 实现要素

| 组件 | 文件 | 职责 |
|---|---|---|
| `CommandSpec` + `CommandKind` | `tui/command.py` | 统一命令描述符，kind 字段 |
| `register_server_commands()` | `tui/command.py` | 从服务器 JSON 注册命令（读取 kind） |
| `_handle_slash_command` | `tui/textual_client.py` | 路由：client→本地, 其他→`_run_server_command` |
| `_cmd_help(name)` | `tui/textual_client.py` | 按名查询 help |
| `ServerCommand.to_dict()` | `protocol/commands.py` | 返回 `kind:"server"` |
| `execute_command(ctx,cmd,args,kind)` | `protocol/commands.py` | skill→加载内容, server→路由, tool→描述 |
| `list_commands(extra=)` | `protocol/commands.py` | 合并静态 + 动态命令 |
| `_tool_commands(reg)` | `protocol/http_server.py` | 枚举 ToolRegistry → command 描述符 |
| `registry.get(name)` fallback | `tools/registry.py` | 按 registry key + tool.name 双层匹配 |
| `restrict([])` silently ignores unmatched | `tools/registry.py` | 不抛 ValueError |
| SkillsPlugin `_on_session_init` | `builtin_plugins/skills/plugin.py` | 发现 skill → 注册为 ToolRegistry entry |
