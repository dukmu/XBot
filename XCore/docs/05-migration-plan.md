# 05 · XBot 全插件架构（XBotv2.<pkg> + xcore.yaml 声明式启动）

> 状态：**已实施**。XBot 完全以 XCore 插件/事件/服务构建：没有独立的 hook 系统
> （HookStage/HookManager 已删除）、没有兼容层（bridge.py 已删除）、没有
> "组件包 vs 插件"的两层概念 —— 运行时的一切能力都是插件树
> （`XBotv2/xcore.yaml`，cordis.yaml 式）中的条目。XCore 保持干净契约，
> 并原生提供"当前 fiber"（`xcore.current_fiber()`）供服务绑定卸载。

## 1. 架构原则（最终版）

1. **全插件，每能力一个包**：配置、持久化、会话、任务、LLM、工具注册表、
   slash 命令、提示词、沙箱、权限、上下文构建、基础工具、agent、引擎 ——
   每个能力都是一个 `XBotv2.<pkg>/plugin.py` 插件（提供 XCore 服务
   `ctx.set(...)` 和/或监听事件 `ctx.on(...)`），挂载在同一个 XCore Context 上。
2. **XBotv2 是唯一包根**：`XBotv2.<pkg>` 平级挂载，无嵌套子包；插件契约
   （Events/Tool/Command/AgentDefinition…）收敛在 `XBotv2.core`，jobs 契约在
   `XBotv2.jobs`；导入一律 `from XBotv2.<pkg> import ...`。
3. **声明式启动（xcore.yaml）**：默认插件树在 `XBotv2/xcore.yaml`（类似
   DeepSeek Harness 的 `cordis.patch.yml`），条目含 id/name/config/disabled；
   会话动态值（paths/workspace/state_store/provider/engine_factory…）以
   `${name}` 引用（`${env:VAR}` 读环境变量），由组合根（bootstrap）提供运行时
   值后由 loader 解析 —— 不在 Python 里硬编码插件列表或逐条注入配置。
4. **服务即能力，XCore 原生卸载**：`ctx.tools` / `ctx.llm` / `ctx.session` /
   `ctx.jobs` / `ctx.state` … 全部由插件注册；插件注册工具/命令/提示词即
   fiber effect —— XCore 在 apply 期间跟踪当前 fiber（`current_fiber()`），
   服务自行把清理绑定到该 fiber，插件卸载时自动撤销（无 loader 侧 caller-tracking）。
5. **层次即领域**：会话（`Session`）= 活动会话，持有会话级 runtime 与
   agent 层次 —— 一个主 agent 实例（`ctx.engine`）+ 若干 subagent 实例
   （`Session.spawn_subagent`，递归同构）；没有抽象中间层（无 runtime/、
   agent_runtime/ 包）。
6. **无兼容层**：不保留旧机制的别名、包装或迁移垫片（ToolRegistrationOptions、
   PluginStore/RuntimePluginContext 已删除；工具注册直接
   `ctx.tools.register(tool, sandbox_mode=...)`）。

## 2. 目录结构（XBotv2.<pkg>）

```
XBotv2/                      # 包根（__init__.py）
  xcore.yaml                 # 默认插件树（bundled，DSH cordis.patch.yml 等价物）
  main.py  client.py  web_server.py  bootstrap.py   # 应用入口 + 组合根（非插件）
  api/                       # ✗ 已删除 —— 契约并入 core/
  core/                      # 纯契约：Events/Tool/Command/AgentDefinition/BaseProvider/…（含 llm 契约）
  agentloop/                 # agent loop 引擎插件（DSH dsh-agent-loop 对应）：engine/operations/session/
                             #   inbox/interactions/internal_messages/content_cache/logging_config/plugin.py
                             #   （plugin.py → ctx.engine）+ apply_agent_* 装配助手
  config/                    # 插件：ctx.settings（配置解析服务）；解析函数组装期供 bootstrap
  persistence/               # 插件：ctx.state_store（会话持久化，jsonl 后端，可换）
  session/                   # 插件：ctx.session（活动会话：主 agent + subagents + 会话级 runtime）
  jobs/                      # 插件：ctx.jobs（后台任务）+ jobs 服务契约
  llm/                       # provider 实现包 + 插件：ctx.llm（LlmService provider 路由目录，
                             #   DSH dsh-llm 对齐；内置 openai/anthropic/mock 适配器注册路由）
  tools/                     # 插件：ctx.tools + ctx.agents（注册表；AgentRegistry 实现在此）
  commands/                  # 插件：ctx.commands（用户 slash 命令）
  prompts/                   # 插件：ctx.prompts（提示词片段）
  sandbox/                   # 插件：ctx.sandbox（沙箱策略 + bwrap + filesystem_ops）
  permissions/               # 插件：ctx.permissions（权限系统）
  context_builder/           # 插件：ctx.context_builder（上下文构建）
  coretools/                 # 插件：基础工具注册 + 核心事件监听（吸收原 core/builtin_tools 平铺）
  agents/                    # 插件：agent 定义加载 + subagent 工具
  goal/ todolist/ skills/ mcp/ compact/ browser/ token_manager/ workspace_instructions/   # 内置插件
  loader/                    # 容器机制：PluginTree/Loader（ctx.loader），bootstrap 挂载
  protocol/ tui/ acp/        # 应用层（serve/TUI/ACP，非插件）
  data/ docs/ tests/
```

删除：`api/`（并入 core/）、`runtime/`（并入 session/）、`agent_runtime/`
（并入 session/）、`core/builtin_tools/`（并入 coretools/）、引擎实现移出
`core/`（→ agentloop/，core 只剩契约）、`xbotv2/` 与 `builtin_plugins/`
包壳、mcp/acp 的 SDK 合并 shim（XBotv2 命名空间下冲突消失）、
`ToolRegistrationOptions`/`PluginStore`/`RuntimePluginContext`。

## 3. 事件目录（core/events.py）

`Events` 常量（`session/init`、`turn/start`、`before/context`、
`before/model-request`、`before/tool-call`、`after/tools` …），
`SHORT_CIRCUIT_EVENTS`（serial 派发），`EventContext`（事件载荷，
替代 HookContext），`ToolDecision/ToolAction`（before/tool-call 的
ALLOW/CONTINUE/DENY/STOP，替代 HookDecision）。

## 4. 引擎 = 事件驱动插件（agentloop/plugin.py → ctx.engine）

- `Engine` 构造改收 `plugin_ctx`（XCore Context），不再有 hook_manager。
- 引擎在 `plugin_ctx` 上派发事件：短路事件 `await ctx.serial(...)`、观察事件
  `await ctx.emit(...)`。
- LLM 客户端经 `ctx.llm`（llm 插件）创建；provider 配置经 `ctx.settings`
  （config 插件）读取。
- 动态工具注册（skills/MCP 在 session/init 时）：插件自己跟踪注册名并
  `ctx.dispose(...)` 清理 —— 没有 plugin_runtime 注入机制。

## 5. 插件树（xcore.yaml，cordis.yaml 机制，服务可用性驱动）

`XBotv2/xcore.yaml` 是**唯一配置文档**（插件树 + 每插件配置：权限/沙箱/
任务限制/钩子/工具/agent 指令），动态值以 `${name}` 引用（未知引用保留字面，
如 `${workspace}` 由权限服务运行时展开）；`loader/` 的
`PluginTree.from_yaml(path, values=...)` 解析引用，`Loader` 导入模块 → 解析
`plugin` 导出 → 挂载（可 isolate），`LoaderComponent` 提供 `ctx.loader`。

**组合根（bootstrap）只做组装**：会话身份 + 运行时值 + 合并外部插件目录与
全局 `~/.xbot/config/plugins.yaml`（`merged_with` 对 config 深度合并，覆盖
单字段无需重写动态值）。**运行时初始化与工作区扩展归插件**：persistence
插件自建 `ctx.state_store`；agentloop 插件读取线程 metadata、恢复/校验
Agent、解析 user_context 与 provider 默认；**workspace_instructions 插件
负责工作区一切扩展**——AGENTS.md 注入 + 应用工作区 `.xbot/plugins.yaml`
树覆盖（经 `loader.apply_patch` 重载受影响条目 / 挂载新条目，也支持工作区
禁用自身）。`config.yaml` 已取消（内容并入 xcore.yaml 对应条目）；
运行时数据默认 `~/.xbot/`（sessions/memory/config）。

**首次运行配置播种（DSH 同款）**：bootstrap 在首次运行时把全局用户树
`~/.xbot/config/plugins.yaml` 写入（缺失才写），用户编辑该文件而非捆绑树。
仓库 `XBotv2/data/` 已删除：两个默认子代理定义（`default`/`Explorer`）内建
为代码（`agents/builtins.py`，同名 Markdown 覆盖内建）。

**Provider 定义与用户上下文都是插件树配置**：`providers.yaml` 与
`user.yaml` 文档已取消——provider 定义是 `llm` 插件条目的 `config`
（`default: minimax` + `providers` 映射），用户上下文是 `config` 插件条目的
`config.user`。`LlmService.configure(default, providers)` 存储原始定义，
`provider_config(name, require_key=...)` 按需解析（只有被选中的 provider
才要求 `api_key_env` 已设置；`require_key=False` 用于 `/providers` 列表），
`default` 名字是默认 provider 的别名。`main.py` 启动校验与服务器根
`/providers` 通过 `resolve_llm_config(paths)` 从合并树解析 llm 条目。

**行序无语义**（DSH cordis.patch.yml 同款）：每个插件声明 `inject` 依赖
（它消费的 ctx.* 服务名），XCore 在所需服务可用时自动激活 fiber——条目顺序
只是可读性分组，任何顺序都能正确启动（有乱序回归测试）。`loader.load()`
挂载全部条目后按轮收敛等待，任一插件 FAILED 或依赖未满足时抛 `LoadError`
（指出插件名与缺失依赖）。引擎（agentloop）inject 全部服务，保证最后就绪。

## 6. 服务绑定与卸载（XCore 原生）

- XCore `Fiber._load` 在 apply 期间设置 `xcore.current_fiber()`；
  `ctx.set()` 的服务归属当前 fiber，fiber 卸载自动释放。
- 能力服务（tools/commands/prompts/agents）把注册清理绑定到
  `current_fiber()` 的 effect 上 —— 插件卸载即撤销，无 loader 侧 contextvar。
- 非 apply 期间（事件监听器、运行时）`current_fiber()` 为 None：
  动态注册由插件自行 `ctx.dispose(...)` 管理。

## 7. 验证

- XBotv2 全量测试绿（含真实 ACP 用例，MINIMAX_API_TOKEN 来自 ~/env.sh）。
- XCore 104 测试全绿（新增 `current_fiber` 契约测试）。
- Minimax 真实端到端冒烟（bootstrap → ctx.engine → 一轮 turn）通过。
