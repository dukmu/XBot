# 05 · XBotv2 → XCore 迁移计划（Step 3）

> 目标：把 XBotv2 的插件层迁移到 XCore 底座，使插件以 Cordis 方式编写（Context /
> 事件 / 服务 / 状态 / 生命周期），核心组件以服务形态注册，Hook 契约保留但走事件
> 总线，PluginStore 由 `ctx.state` 命名空间承接。迁移在 `dev-xcore` 分支进行，
> **全量测试绿 = 迁移完成的证据**。

## 1. 迁移后架构

```
Clients → Protocol → Engine/Session（构造注入，接口不变）
                          │ hook_manager.run(stage, hook_ctx)   [引擎面 API 不变]
                          ▼
                 HookManager（bus-backed）────────┐
                          │ 监听器 = 总线事件       │
                          ▼                       ▼
              xcore Context（每 session 运行时一个）
                 ├── ctx.on(HookStage.X.value, fn)   ← 插件注册 Hook
                 ├── ctx.set("tools", ToolRegistry)  ← 核心组件 = 服务
                 ├── ctx.state.namespace(name)       ← PluginStore
                 └── ctx.plugin(adapter)             ← 插件挂载（XCore Registry/Fiber）
```

- **引擎不动**：Engine 仍通过构造注入拿 `hook_manager/tool_registry/state_store/...`，
  调用面不变。
- **HookManager 是桥**：监听器存储在 XCore 事件总线（注册序 + prepend + 归属清理），
  `run()` 保留 41 阶段契约（transform 键校验 / guard 短路 / strict 聚合 / 短路集）。
- **核心组件 = 服务**：tools / commands / prompts / agents / jobs / variables /
  state / workspace / session / runtime / agent_runtime / paths。
- **插件生命周期交给 XCore Fiber**：apply 即插件体；注册即 fiber effect（卸载自动
  逆序清理，取代 loader 的手工 rollback 表）。

## 2. 迁移映射

| 现状（XBotv2） | 迁移后（XCore） |
| --- | --- |
| `PluginSetupContext.register_hook(stage, fn)` | `ctx.on(stage.value, fn)`（loader 在 `run()` 侧按 hook 归属注入 `plugin_runtime`） |
| `register_tool(tool, options)` | `ctx.tools.register(tool, options)`（fiber effect 注册，卸载自动注销） |
| `register_command(cmd)` | `ctx.commands.register(cmd)` |
| `add_prompt_fragment(stage, text, source)` | `ctx.prompts.add(stage, text, source)` |
| `register_agent(def)` | `ctx.agents.register(def, owner=...)` |
| `PluginStore`（`plugin/store.py`） | `ctx.state.namespace(manifest.name)`（API：async get/set/delete/all/clear，一致） |
| `PluginBase.setup(ctx)` | `PluginBase.apply(ctx)`（xcore fiber 体）；`on_load(config)` 由 loader 先调；`on_unload` 经 `ctx.dispose` 注册 |
| 构造器注入核心组件 | 核心组件注册为服务（`ctx.set`） |
| loader 手工 rollback（hook_refs/tool_names/...） | XCore effect 机制（fiber 卸载逆序清理） |
| `HookManager` 内部 dict | HookManager 监听器存于事件总线（`EventBus.hooks_for` 供 `run()` 收集） |
| 插件状态文件（CoreStateStore plugin_states） | StateService JSON（会话 state 目录 `state.json` + 命名空间） |

**Hook 契约不变**：41 个 `HookStage`、`HookContext`、`HookDecision`、transform 键表、
`SHORT_CIRCUIT_STAGES`、`STRICT_FAILURE_STAGES`、`HookManager.run` 返回值语义 ——
全部保留（`xbotv2/api/hooks.py` 不动）。

**plugin_runtime 机制保留**：`run()` 收集总线 hook 时按 `hook.owner.fiber` 解析插件
运行时上下文（`RuntimePluginContext`），skills/MCP 在 ON_SESSION_INIT 的动态工具
注册照常工作，且注册变为 fiber effect（卸载自动清理）。

## 3. 组件服务清单（bootstrap 注册）

| 服务名 | 类型 | 说明 |
| --- | --- | --- |
| `tools` | ToolRegistry 门面 | `register(tool, options)` / `unregister(name)`（fiber effect） |
| `commands` | CommandRegistry | `register(cmd)` / `unregister(name)` / `get(name)` / 遍历 |
| `prompts` | PromptRegistry | `add(stage, text, source)` / `remove(stage, name)` |
| `agents` | AgentRegistry 门面 | `register(def, owner)` / `unregister(name, owner)` |
| `jobs` | JobRegistry | 现有实例 |
| `variables` | RuntimeVariables | 现有实例 |
| `state` | StateService | 会话可恢复状态（root 服务，XCore 内建语义） |
| `workspace` | str | workspace_root |
| `session` | SessionInfo | session_id/thread_id/provider/workspace_root |
| `runtime` | RuntimeConfig | agent 配置 |
| `agent_runtime` | EngineAgentRuntime \| None | 子代理工厂 |
| `paths` | RuntimePaths | 路径 |
| `hooks` | HookManager | 桥本身（插件可读，一般用 `ctx.on/off`） |

保留名注意：`config`/`state`/`registry`/`fiber`/事件方法等不可用作服务名（XCore
§保留名）。

## 4. 实施阶段（本分支，随轮推进）

1. **xcore 访问器**：`EventBus.hooks_for(event)`（活监听器，注册序，含 owner）。
2. **HookManager 桥**：`hooks/manager.py` 重写（ctx 可选；有 ctx 走总线，无 ctx 本地
   存储供隔离测试）。
3. **xbot↔xcore 桥**：`xbotv2/plugin/bridge.py` —— `register_core_services(ctx, ...)`、
   `RuntimePluginContext`（fiber effect 注册）、`PluginAdapter`（ctx/store 绑定 +
   on_unload 注册）。
4. **PluginBase v2 + loader**：`api/plugins.py`（PluginBase.apply 模型、manifest 保留、
   PluginStore 变 Protocol）、`plugin/loader.py`（发现/依赖/校验 + `ctx.plugin` 挂载）。
5. **bootstrap 接线**：创建 xcore Context（`data_dir` = 会话 state 目录）、注册服务、
   装载插件、失败清理走 `ctx.destroy()`。
6. **10 个内置插件迁移**（机械改写）。
7. **api 导出与测试修正**；全量 798 测试回归。
8. **文档**：本文档 + `docsv2/plugins/plugins.md` 更新 + 开发日志。

## 5. 风险与验证

- **行为等价**：hook 注册顺序、short-circuit、strict 聚合、卸载清理顺序、插件状态
  持久化（同一会话重启恢复）——由现有测试断言。
- **重点测试**：test_hooks（阶段矩阵）、test_plugin_loader、test_plugin_store、
  test_goal/test_todolist（状态）、test_bootstrap、test_skills/test_mcp（动态
  plugin_runtime 注册）、test_compact/test_agents/test_browser/test_token_manager/
  test_workspace_instructions。
- **已知差异（文档化）**：插件状态存储位置从 `plugin_states/` 改为会话
  `state.json`（旧会话数据不复用，文档注明）；`PluginStore` 类移除，插件改用
  `ctx.state.namespace`。
