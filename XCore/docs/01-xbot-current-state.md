# 01 · XBot 现状与设计分析（Step 1 交付物）

> 目标：在新分支（`dev-xcore`）上实现一个 Python 的、类似 Cordis 的、以插件为核心的
> XBot。第一步先如实理解 XBot 现状与设计，作为 XCore 设计与后续迁移的输入。
> 本文档记录对 `XBotv2`（仓库主实现）现状的调研结论。调研基于当前 `main` 分支的
> 代码、`PROJECT_RESUME.md`、`README.md` 与 `docsv2/` 文档，信息截至 2026-08。

## 1. 一句话现状

`XBotv2` 是一个从零重写的通用 C/S 架构 AI Agent 运行时：极简 ReAct 核心循环 +
类型化 HTTP/SSE 协议 + 41 阶段 Hook 契约 + 统一文件系统/权限/沙箱 + 事务式插件体系
（Goal / TodoList / Skills / MCP / Compact 等内置插件），由 HarnessBench 104 任务
评估闭环持续驱动迭代。约 798 项测试，Python 3.11+（当前环境 3.12），`uv` 管理依赖，
核心零框架依赖（无 LangChain）。

## 2. 目录结构与分层

```
XBotv2/
  main.py                  # 入口：python main.py terminal|serve|once|web|acp
  xbotv2/
    __main__.py            # CLI 分发
    api/                   # ★ 插件唯一稳定扩展面（public API 契约）
      __init__.py          #   导出 PluginBase/PluginSetupContext/Tool/... 
      plugins.py           #   PluginManifest / PluginBase / PluginStore(Protocol)
      hooks.py             #   HookStage / HookDecision / HookContext
      tools.py             #   Tool / ToolResult / ToolRegistry 契约
      commands.py          #   Command（人机 slash 命令契约）
      agents.py            #   AgentDefinition / AgentRuntime
      context.py           #   PromptFragmentStage（prompt 展开契约）
      jobs/                #   JobRegistry（后台任务统一实体）
      paths.py messages.py providers.py variables.py tokens.py
    core/                  # 核心运行时（不依赖插件）
      engine.py            #   _run_turn_impl 约 155 行编排器
      session.py context.py agents.py inbox.py interactions.py
      bootstrap.py         #   组装运行时、安装插件
      internal_messages.py content_cache.py operations.py logging_config.py
      builtin_tools/       #   shell / filesystem / content / interaction
    hooks/manager.py       # HookManager：注册、执行、短路、严格失败
    plugin/loader.py       # 插件发现、依赖解析、加载/卸载/重载
    plugin/store.py        # PluginStore：per-plugin 持久 KV
    persistence/store.py   # CoreStateStore：messages.jsonl + plugin_states/ + artifacts/
    protocol/              # HTTP/SSE/UDS 传输、SessionManager、v3 资源模型
    tools/                 # ToolRegistry / PermissionSystem / Sandbox / filesystem_ops
    providers/             # OpenAI / Anthropic / LM Studio / Mock 适配器
    config/                # 全局/workspace/会话三层 YAML 配置
    tui/ web_dist/ web_server.py acp/ llm/ client.py
  builtin_plugins/         # goal/ todolist/ skills/ mcp/ compact/ agents/
                           # browser/ token_manager/ workspace_instructions/
  docsv2/                  # 架构文档（按设计边界组织，见下文）
  tests/                   # core/ integration/ bench/ acp/
```

分层边界（AGENTS.md 与 architecture.md 明确定义）：

```
Clients(TUI/Web) → Protocol(HTTP/SSE/UDS) → Core(SessionRuntime/Engine/Context/…)
   → Tools(Registry/权限/沙箱) → Providers(OpenAI/Anthropic/Mock)
Plugins ──只 import xbotv2.api──▶ Core
```

核心原则：稳定公共 API 是契约不是实现；`xbotv2.api` 符号清单由 `test_public_api.py`
对照 `api_inventory.md` 校验；插件永不触达 HookManager/Engine 内部。

## 3. 现状的插件体系（与 Cordis 对照的出发点）

现状插件体系是 **“声明式清单 + 事务式注册”**，与 Cordis 的“Context 即一切、服务即
插件、事件即总线”模型有本质差异。逐项对照：

| 维度 | XBotv2 现状 | 与 Cordis 的差距 |
| --- | --- | --- |
| 插件声明 | `plugin.yaml`（name/version/api_version/depends_on/hooks/tools/prompt_fragments/config_schema），Pydantic 校验 | 无运行时内联声明；config 用 JSON Schema 而非 schema DSL |
| 插件实例 | `PluginBase`（`on_load`/`on_unload`/`setup(ctx)`/`diagnostics`/`status_slots`） | 无函数式/对象式插件；无 `apply(ctx, config)` 模型 |
| 注册方式 | `setup(PluginSetupContext)` 一次性注册 hook/tool/command/fragment/agent，失败整体 `rollback()` | 无增量注册、无运行期动态注册/注销（除 tool/command） |
| 依赖 | `depends_on` 拓扑排序，缺依赖/环即报错 | 无 `required`/`optional` 语义，无运行时服务注入 |
| 生命周期 | `on_load`（验证配置/初始化外部资源）→ `setup`（注册）→ `on_unload`（释放） | 无 start/stop/restart 可恢复生命周期；无插件状态机（preparing/running/error/failure） |
| 状态持久化 | `PluginStore`（Protocol）：per-plugin 立即持久 KV（YAML 兼容），`get/set/delete/all/clear` | 无服务化状态（koishi `ctx.state`），无恢复语义 |
| 事件 | `HookManager` + 41 个 `HookStage`（observer/transform/guard 三类契约） | 无任意事件名/通配符/`emit/parallel/bail/serial/chain` 语义；Hook 是阶段化的固定管线而非通用事件总线 |
| 服务 | 无服务注册表；核心组件以构造器注入传给 `_PluginSetupContext` | 无 `ctx.set/get/unset`、无 Proxy 属性访问、无选择器隔离 |
| 中间件 | 无 | 无 `ctx.middleware`/`ctx.filter` 链 |
| 配置 schema | JSON Schema（jsonschema 校验，无默认值应用） | 无 schema DSL（S.object/S.string/…）、无默认值合并 |
| 清理 | 卸载时按注册逆序注销；异常聚合为 ExceptionGroup | 无 `ctx.dispose(cb)` 资源句柄模型 |

### 3.1 现状插件生命周期细节（loader.py）

1. `discover()`：扫描插件目录下 `plugin.yaml` → `PluginManifest`（校验 config_schema 为合法 JSON Schema）。
2. `resolve_dependencies()`：按 `depends_on` 拓扑排序；重复清单、缺失依赖、环均报错。
3. `load()`：逐插件 `manifest.validate_config(config)` → 保证 importable → 建 `PluginStore` → `instantiate_plugin()` → `plugin.on_load(config)` → `setup(ctx)` 注册，任一失败则 `on_unload` + `unload_all()` 回滚后抛错。
4. `unload(name)` / `reload(name)` / `unload_all()`：逆序注销 hook/tool/command/fragment/agent；卸载异常聚合 `ExceptionGroup`。
5. `_PluginSetupContext`：把 hook/tool/command/fragment/agent 注册适配到核心服务并记录资源清单（`hook_refs`/`tool_names`/…），供回滚与卸载。

### 3.2 现状 Hook 体系要点（迁移时的事件系统对照）

- `HookManager.register(stage, fn)` / `unregister` / `run(stage, ctx, short_circuit?)`。
- 三类契约：Observer（必须返回 None，全部运行，失败记日志）；Transform（返回阶段专属文档化 dict，键校验，首个非 None 短路）；Guard（返回 `HookDecision`，`CONTINUE`/`DENY`/`STOP`/`ALLOW`，guard 能收窄/拒绝/改写调用但不能授予权限）。
- 严格失败阶段（生命周期/持久化）跑完所有回调后聚合 `ExceptionGroup` 抛出；guard/transform 失败立即传播。
- 持久化 Hook 的“检查点”语义：`save_messages()` 先比较归一化快照指纹，未变化不触发 before/after 持久化 Hook。

### 3.3 现状持久化（迁移时“可恢复状态”的对照）

- `CoreStateStore`（persistence/store.py）：`messages.jsonl`（append-only 消息日志 + 指纹增量）、`plugin_states/`（per-plugin 不透明状态文件，核心不解释内容）、`artifacts/`（大工具输出缓存）。
- `PluginStore`（plugin/store.py）：per-plugin KV，立即落盘，YAML 兼容；`get/set/delete/all/clear`。
- 会话恢复：`resume` 从持久历史重建运行时；SSE 断连不等于 session 销毁；交互请求显式声明不可恢复。

### 3.4 文档体系（XCore 文档要对齐的惯例）

`docsv2/` 按设计边界组织：`architecture.md`（总览）、`core/`、`api/`（public_api.md +
api_inventory.md）、`protocol/`、`tools/`、`hooks/`（hooks.md + hook_stage_matrix.md，
矩阵由测试反向校验）、`plugins/`、`clients/`、`project/`（behavior.md +
iteration_backlog.md）、`verification/`（testing.md）。规则：README.md 是索引；实现
文档描述当前实现；plan 文档不算规格；契约（api_inventory/typed contracts）为准。
项目级纪律见 `AGENTS.md`：dev-* 分支开发、文档与行为同步、测试即证据。

## 4. 对 XCore 的输入结论

1. **XCore 必须自洽**：作为独立、零依赖（stdlib-only）的 Python 包，提供 Cordis 式
   核心能力（可恢复状态 / 插件化 / 事件系统 / 生命周期 / 服务系统），并被后续迁移
   （Step 3）承接，届时 XBotv2 的 api/plugin/hooks 层以 XCore 为底座重写。
2. **事件系统**：XBotv2 的 41 阶段 Hook 是固定管线；XCore 需要通用事件总线
   （任意事件名、`/` 命名空间与 `*` 通配、emit/parallel/bail/serial/chain），同时
   保留“阶段化、短路、严格失败”语义作为其上的一层约定（迁移时 Hook 可映射为
   命名事件 + guard/transform 约定）。
3. **服务系统**：XBotv2 用构造器注入传核心组件；XCore 需要 `ctx.set/get/unset` 服务
   注册表与 Proxy 属性访问，迁移后核心组件（ToolRegistry、HookManager、StateStore…）
   均以服务形态注册。
4. **插件模型**：XBotv2 的 `PluginBase + manifest + 事务式 setup` 与 Cordis 的
   `ctx.plugin(plugin, config)`（函数/对象/类插件 + dispose）需要融合：XCore 提供
   Cordis 式核心，迁移层保留 manifest 声明（作为插件元数据）与事务回滚语义。
5. **可恢复状态**：以 `ctx.state` 服务（per-plugin 命名空间、立即持久、重启恢复）
   承接 XBotv2 的 `PluginStore` 语义。
6. **生命周期**：XCore 提供 `start → stop → restart` 可恢复生命周期与插件状态机；
   XBotv2 的 `on_load/on_unload/setup` 在迁移层映射为生命周期钩子。
