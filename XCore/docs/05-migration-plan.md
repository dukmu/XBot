# 05 · XBot 组件化架构（全插件 + 事件驱动）

> 状态：**已实施**。XBot 完全以 XCore 插件/事件/服务构建：**没有**独立的 hook
> 系统（HookStage/HookManager 已删除）、**没有**兼容层（bridge.py 已删除）、
> 引擎本身是插件，通过 XCore 事件对外扩展。XCore 保持干净契约，零内部暴露。

## 1. 架构原则（最终版）

1. **全插件**：运行时的一切能力 —— 引擎（核心 loop）、工具、LLM、配置、状态、
   基础工具、内置插件 —— 都是插件树（cordis.yaml 式）中的条目，挂载在同一个
   XCore Context 上。
2. **事件即扩展点**：引擎不再有 41 阶段 Hook 契约；turn/session 生命周期、
   上下文构建、模型调用、工具调用都是 XCore 事件（`session/init`、
   `before/model-request`、`before/tool-call` …）。插件用 `ctx.on(event, fn)`
   监听/拦截；短路事件用 `ctx.serial`（首个非 None 结果由引擎解释），观察事件用
   `ctx.emit`。
3. **服务即能力**：`ctx.tools` / `ctx.llm` / `ctx.engine` / `ctx.state` /
   `ctx.hooks`（不再有）… 全部由插件注册；插件注册即 fiber effect（卸载自动清理）。
4. **无兼容层**：不保留旧机制的别名、包装或迁移垫片。
5. **依赖用插件发现**：树条目按序挂载，服务依赖由条目顺序与 `inject` 表达。

## 2. 目录结构（全插件，扁平布局）

```
XBotv2/
  main.py                    # 入口：xbot = main:main（CLI 分发）
  api/                       # ★ 插件唯一稳定扩展面（public API 契约）
  core/                      # 核心运行时 + engine 插件（core/plugin.py）
  tools/                     # ToolRegistry/sandbox/permissions + tools 插件（tools/plugin.py）
  runtime/                   # runtime 插件（xbot.runtime：paths/session/variables/config/state_store）
  coretools/                 # coretools 插件（基础工具 + result-cache 监听 + 配置钩子）
  agent_runtime/             # 子代理工厂插件
  loader/                    # 插件树 loader（cordis.yaml 机制）+ PluginTree
  config/  persistence/  llm/  protocol/  tui/  acp/
  goal/  todolist/  skills/  mcp/  compact/  agents/  browser/
    token_manager/  workspace_instructions/   # 内置插件（纯 XCore 插件）
  data/                      # 运行时数据
  docs/                      # 文档（原 docsv2/）
  tests/                     # core/ integration/ bench/ acp/
```

删除：`xbotv2/hooks/`（HookManager）、`xbotv2/api/hooks.py`（HookStage/HookContext/
HookDecision）、`xbotv2/plugin/`（bridge.py/loader.py）、`components/`（并入
`plugins/`）、各 `plugin.yaml`（配置走插件 Config S schema）。

## 3. 事件目录（api/events.py）

`Events` 常量（`session/init`、`turn/start`、`before/context`、
`before/model-request`、`before/tool-call`、`after/tools` …），
`SHORT_CIRCUIT_EVENTS`（serial 派发），`EventContext`（事件载荷，
替代 HookContext），`ToolDecision/ToolAction`（before/tool-call 的
ALLOW/CONTINUE/DENY/STOP，替代 HookDecision）。

## 4. 引擎 = 事件驱动插件

- `Engine` 构造改收 `plugin_ctx`（XCore Context），不再有 hook_manager。
- 引擎在 `plugin_ctx` 上派发事件：短路事件 `await ctx.serial(...)`、观察事件
  `await ctx.emit(...)`；结果由引擎按事件语义解释（与旧契约的行为一致，
  但没有独立的校验层）。
- 动态工具注册（skills/MCP 在 session/init 时）：插件自己跟踪注册名并
  `ctx.dispose(...)` 清理 —— 没有 plugin_runtime 注入机制。

## 5. 插件树（cordis.yaml 机制）

`loader/`：`PluginTree`（id/name/config/disabled/inject/isolate 条目，YAML 或
dict）、`Loader`（导入模块 → 解析 `plugin` 导出 → 按序挂载，可 isolate）、
`LoaderComponent`（把 loader 作为插件提供 `ctx.loader`）。`.xbot/plugins.yaml`
可追加/覆盖条目。默认树由启动器组装：hooks?（无）→ runtime → tools →
coretools → agent_runtime → 内置插件 → core（引擎，最后）。

## 6. 与早期版本的差异（全部消除）

| 早期 | 现在 |
| --- | --- |
| HookStage 枚举 + HookManager 契约层 | XCore 事件（Events 常量 + serial/emit） |
| HookContext/HookDecision | EventContext + ToolDecision |
| bridge.py（服务适配 + plugin_runtime + caller-tracking） | 服务类移入 plugins/tools.py；caller-tracking 由 loader 设置 |
| PluginBase + plugin.yaml + 自研 loader | 纯 XCore 插件（模块导出 `plugin`）+ 插件树 loader |
| 引擎在 XCore 之外 + hook_manager 注入 | 引擎是插件（ctx.engine），事件驱动 |

## 7. 验证

- XBotv2 全量测试绿（除既有环境依赖项 `MINIMAX_API_TOKEN`）。
- XCore 103 测试全绿（零改动）。
- Minimax 真实端到端冒烟（bootstrap → ctx.engine → 一轮 turn）通过；
  ACP adapter 测试以 Minimax 跑通。
