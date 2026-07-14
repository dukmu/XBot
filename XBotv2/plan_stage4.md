# Stage 4: Skills + MCP as Plugin Extensions (Historical)

> Archived implementation plan. This is not a current specification; see
> `docsv2/README.md`.

## 核心原则

Skills 和 MCP 作为 XBotv2 的**内置插件**实现，通过 `PluginBase` + `HookManager` 注入。**引擎核心和 bootstrap 代码零改动**。

---

## 1. SkillsPlugin

### 1.1 文件清单

| 文件 | 用途 |
|---|---|
| `builtin_plugins/skills/manifest.yaml` | 插件元数据 |
| `builtin_plugins/skills/plugin.py` | `SkillsPlugin(PluginBase)` |
| `builtin_plugins/skills/registry.py` | `SkillRegistry` — 发现、解析、缓存 |
| `builtin_plugins/skills/skill_tool.py` | `load_skill` + `!` 注入预处理 |
| `builtin_plugins/skills/permission_scope.py` | 临时权限覆盖管理器 |

### 1.2 Frontmatter — 完整字段支持

```yaml
---
name: git-release              # required, 1-64 chars, 小写+连字符
description: Create releases   # required, 1-1024 chars
license: MIT                    # optional
metadata:                       # optional string→string map
  audience: maintainers
allowed-tools: Bash(git *) Bash(npm *)   # 临时权限提升
disallowed-tools: AskUserQuestion        # 临时工具禁止
disable-model-invocation: true  # 仅用户手动调用（/skill-name）
---
## Instructions

Current branch: !`git branch --show-current`
上次发布: !`git tag --sort=-creatordate | head -1`

Check changed files: !`git diff --name-only HEAD~1`
```

### 1.3 plugin.py — `SkillsPlugin`

```python
class SkillsPlugin(PluginBase):

    async def on_load(self, config: dict) -> None:
        self._registry = SkillRegistry()
        self._permission_scope = SkillPermissionScope()
        self._active_skills: dict[str, Skill] = {}

    def hooks(self) -> list[HookRegistration]:
        return [
            HookRegistration(HookStage.ON_SESSION_INIT, self._on_session_init),
            HookRegistration(HookStage.AFTER_CONTEXT, self._on_after_context),
            HookRegistration(HookStage.ON_TURN_END, self._on_turn_end),
            HookRegistration(HookStage.BEFORE_TOOL_CALL, self._on_before_tool),
        ]

    def tools(self) -> list[XBotTool]:
        return [XBotTool.from_function(load_skill, name="skill")]

    async def _on_session_init(self, ctx):
        self._registry.discover(Path(ctx.session.workspace_root))

    async def _on_after_context(self, ctx):
        """注入活跃 skill 内容到上下文。"""
        if not self._active_skills:
            return
        msgs = list(ctx.context_messages)
        content = self._build_active_context()
        msgs.insert(1, Message(role="system", content=content))
        return {"context_messages": msgs}

    async def _on_turn_end(self, ctx):
        """清除本回合的活跃 skill。"""
        self._active_skills.clear()
        self._permission_scope.clear()

    async def _on_before_tool(self, ctx):
        """应用 active skill 的 allowed-tools / disallowed-tools。"""
        if not self._active_skills:
            return
        tool_name = ctx.tool_call["name"]
        decision = self._permission_scope.check(tool_name)
        if decision == "deny":
            return {"deny_reason": f"Tool '{tool_name}' disallowed by active skill"}
        if decision == "allow":
            return None  # 通过，允许执行
```

### 1.4 `!` shell 注入 — 利用现有沙箱

当 `load_skill` 加载 SKILL.md 时，预处理 `` !`command` `` 占位符：

```python
_SHELL_INJECT = re.compile(r"!`([^`]+)`")

async def _preprocess_skill(content: str, sandbox) -> str:
    """Expand !`cmd` placeholders using sandboxed shell execution."""
    async def _replace(match):
        cmd = match.group(1).strip()
        if sandbox and sandbox.enabled:
            result = await sandbox.run_shell(cmd)
            return result
        # Fallback: use subprocess directly (sandbox disabled)
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=PIPE, stderr=PIPE, cwd=os.getcwd()
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="replace").strip()
    
    expanded = content
    for match in _SHELL_INJECT.finditer(content):
        replacement = await _replace(match)
        expanded = expanded.replace(match.group(0), replacement)
    return expanded
```

为什么安全：
- Skill 文件是**用户信任的文件**（放在项目的 `.claude/skills/` 或全局 `~/.claude/skills/`）
- `!` 命令通过 `SandboxPolicy.run_shell()` 执行 → BubblewrapBackend 隔离
- 沙箱默认 enabled，未安装 bwrap 时回退到 subprocess（但拒绝）
- 与 Claude Code 的行为一致：skill 作者控制 `!` 命令内容

### 1.5 `allowed-tools` / `disallowed-tools` — 利用现有 PermissionSystem

`SkillPermissionScope` 管理活跃 skill 的工具权限覆盖：

```python
class SkillPermissionScope:
    """Per-turn tool permission overrides from active skills."""

    def add(self, allowed: list[str], disallowed: list[str]) -> None:
        """Add allowed/disallowed tool patterns from a loaded skill."""
        for pattern in allowed:
            self._allowed.append(_compile_pattern(pattern))
        for pattern in disallowed:
            self._disallowed.append(_compile_pattern(pattern))

    def check(self, tool_name: str) -> str | None:
        """返回 'deny' / 'allow' / None（无覆盖）"""
        for pattern in reversed(self._disallowed):
            if pattern.match(tool_name):
                return "deny"
        for pattern in reversed(self._allowed):
            if pattern.match(tool_name):
                return "allow"
        return None

    def clear(self) -> None:
        self._allowed.clear()
        self._disallowed.clear()
```

模式语法——复用 PermissionSystem 的现有规则：
- `Bash` — 精确匹配工具名
- `Bash(git *)` — 匹配工具名 + 参数模式
- `mcp__*` — 通配符匹配所有 MCP 工具

为什么可行：
- XBotv2 已有 `PermissionSystem` 支持 `deny`/`allow`/`ask` + regex 模式匹配
- `BEFORE_TOOL_CALL` hook 可返回 `{"deny_reason": "..."}` 阻止工具执行
- 覆盖仅在 skill 活跃期间（当前 turn）生效，turn 结束自动清除

### 1.6 Context injection — 通过 `AFTER_CONTEXT` hook

```text
[system prefix]
[active skills content]    ← SkillsPlugin 注入
[runtime rules]
[message history]
```

注入格式：
```
## Active Skills

### git-release
<预处理后的 SKILL.md body（!` 已展开）>

### code-review
<...>
```

---

## 2. MCPPlugin

### 2.1 文件清单

| 文件 | 用途 |
|---|---|
| `builtin_plugins/mcp/manifest.yaml` | 插件元数据 |
| `builtin_plugins/mcp/plugin.py` | `MCPPlugin(PluginBase)` |
| `builtin_plugins/mcp/client.py` | `MCPClient` — JSON-RPC over stdio/HTTP |
| `builtin_plugins/mcp/tool.py` | `MCPTool` — XBotTool 兼容 callable |

### 2.2 plugin.py — `MCPPlugin`

```python
class MCPPlugin(PluginBase):

    async def on_load(self, config: dict) -> None:
        self._client = MCPClient()
        self._servers: dict[str, dict] = {}
        self._registered_tools: list[XBotTool] = []

    def hooks(self) -> list[HookRegistration]:
        return [
            HookRegistration(HookStage.ON_SESSION_INIT, self._on_session_init),
            HookRegistration(HookStage.ON_SESSION_CLOSE, self._on_session_close),
        ]

    async def _on_session_init(self, ctx):
        cfg = self._load_config(ctx.config)
        for name, server_cfg in cfg.get("servers", {}).items():
            if not server_cfg.get("enabled", True):
                continue
            try:
                tools = await self._client.connect_and_list(name, server_cfg)
                for tool_def in tools:
                    mcp_tool = MCPTool(self._client, name, tool_def)
                    xbot_tool = XBotTool.from_function(
                        mcp_tool, name=f"mcp__{name}__{tool_def['name']}"
                    )
                    ctx.tools.register(xbot_tool, sandbox_mode="host")
                    self._registered_tools.append(xbot_tool)
            except MCPConnectionError:
                logger.warning("MCP %s unavailable, skipping", name)

    async def _on_session_close(self, ctx):
        await self._client.disconnect_all()
```

### 2.3 MCPClient — JSON-RPC 传输

```python
class MCPClient:
    async def connect_and_list(self, name: str, cfg: dict) -> list[dict]:
        transport = StdioTransport(cfg["command"]) if cfg["type"] == "local" \
                else HttpTransport(cfg["url"])
        await transport.connect()
        result = await transport.call("tools/list", {})
        self._transports[name] = transport
        return result.get("tools", [])

    async def call_tool(self, server: str, tool: str, args: dict) -> str:
        transport = self._transports[server]
        result = await transport.call("tools/call", {"name": tool, "arguments": args})
        return _normalize_mcp_result(result)
```

### 2.4 MCPTool — XBotTool 兼容

```python
class MCPTool:
    def __init__(self, client, server, tool_def):
        self._client = client
        self._server = server
        self._name = tool_def["name"]
        self.__doc__ = tool_def.get("description", "")

    async def __call__(self, **kwargs) -> str:
        return await self._client.call_tool(self._server, self._name, kwargs)
```

工具名：`mcp__<server>__<tool>`（Claude Code 兼容）。

### 2.5 配置

```yaml
# data/config/system.yaml
plugins:
  mcp:
    servers:
      github:
        type: local
        command: ["npx", "-y", "@modelcontextprotocol/server-github"]
        enabled: true
      sentry:
        type: remote
        url: "https://mcp.sentry.dev/mcp"
        enabled: false
```

---

## 3. 主代码改动清单

**引擎核心零改动。** Skills 和 MCP 完全通过插件系统注入：

| 注入点 | SkillsPlugin | MCPPlugin |
|---|---|---|
| 工具注册 | `tools()` → `ToolRegistry.register("skill")` | `ON_SESSION_INIT` hook → `ctx.tools.register(mcp__*)` |
| 上下文 | `AFTER_CONTEXT` hook → 消息列表注入 | 无 |
| 工具权限 | `BEFORE_TOOL_CALL` hook → 临时覆盖 | 无（MCP 工具无特殊权限需求） |
| 生命周期 | `ON_SESSION_INIT` + `ON_TURN_END` | `ON_SESSION_INIT` + `ON_SESSION_CLOSE` |

**现有机制确认就绪**：

1. `ctx.tools` 在 hook context 中可用（`_make_hook_context` 传入 `tools=self.tool_registry`）
2. `AFTER_CONTEXT` 返回 `{"context_messages": [...]}` 被引擎消费（有现有测试）
3. `BEFORE_TOOL_CALL` 返回 `{"deny_reason": "..."}` 阻止工具执行（现有机制）
4. `XBotTool` KEYWORD_ONLY 参数注入传递额外依赖
5. `SandboxPolicy.run_shell()` 已通过 BubblewrapBackend 隔离

---

## 4. 实现顺序

### Phase A — Skills 基础

| # | 文件 | 行数 | 内容 |
|---|---|---|---|
| 1 | `registry.py` | ~100 | `Skill`, `SkillRegistry` — 目录扫描 + frontmatter 解析 |
| 2 | `skill_tool.py` | ~60 | `load_skill` + `_preprocess_skill`（`!` 注入） |
| 3 | `permission_scope.py` | ~50 | `SkillPermissionScope` — 临时权限覆盖 |
| 4 | `plugin.py` | ~80 | `SkillsPlugin` — hooks + tools |
| 5 | `manifest.yaml` | ~8 | 插件元数据 |

### Phase B — MCP 基础

| # | 文件 | 行数 | 内容 |
|---|---|---|---|
| 6 | `client.py` | ~120 | `MCPClient`, `StdioTransport`, JSON-RPC |
| 7 | `tool.py` | ~40 | `MCPTool` — XBotTool 包装 |
| 8 | `plugin.py` | ~60 | `MCPPlugin` — hook + 注册 |
| 9 | `manifest.yaml` | ~8 | 插件元数据 |

### Phase C — 打磨

| # | 文件 | 行数 | 内容 |
|---|---|---|---|
| 10 | `client.py` (扩展) | ~60 | `HttpTransport` — 远程 MCP |
| 11 | 测试 | ~300 | Skills 发现/解析/注入/权限、MCP tools/list/call/错误 |

**总计 ~900 行新代码 + ~300 行测试，引擎 0 行改动。**

---

## 5. 兼容性

| 特性 | Claude Code | OpenCode | XBotv2 目标 |
|---|---|---|---|
| SKILL.md 发现 | `.claude/skills/` | `.opencode/skills/` | 两者都支持 + `.agents/skills/` |
| SKILL.md frontmatter | ✅ | ✅ | ✅ |
| `allowed-tools` / `disallowed-tools` | ✅ | ❌ | ✅ |
| `!` shell 注入 | ✅ | ❌ | ✅（沙箱隔离） |
| `context: fork` 子代理 | ✅ | ❌ | ❌（后续评估） |
| `skill` 工具 | ✅ | ✅ | ✅ `XBotTool` |
| MCP local (stdio) | ✅ | ✅ | ✅ |
| MCP remote (HTTP) | ✅ | ✅ | ✅ |
| MCP OAuth | ✅ | ✅ | ❌（后续评估） |
| 工具命名 | `mcp__<srv>__<tool>` | 同 | 同 |
| 插件化 | ❌（核心） | ✅ Plugin | ✅ Plugin |
