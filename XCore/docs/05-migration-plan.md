# 05 · XBot 组件化架构（XCore 之上的组件包构建）

> 状态：**已实施**（commit 见开发日志）。早期"桥接式迁移"方案已废弃：XBot 不再
> 在 XCore 旁挂一个平行核心，而是**完全以组件包（XCore 插件/服务）构建**，包括
> 核心 loop。XCore 保持干净的 Cordis 契约，不做任何面向迁移的内部暴露
> （`hooks_for` 一类访问已移除）。

## 1. 架构原则

1. **XCore 是唯一核心**：Context/事件/服务/插件/生命周期/状态全部由 XCore 提供，
   XCore 公共 API 不因 XBot 需求而污染（不暴露 EventBus 内部记录）。
2. **一切能力都是服务**：工具、Hook、配置、状态、会话、引擎……均由组件在
   Context 上注册，插件与引擎只通过服务消费。
3. **Hook 即事件**：41 个 HookStage 是普通 XCore 事件；契约（observer/transform/
   guard 校验、短路、strict 聚合、`plugin_runtime` 注入）在注册侧包装器中执行，
   派发走公开原语（`emit`/`serial`）。
4. **核心 loop 是组件**：Engine 由 `xbot.core` 组件从服务组装，以 `ctx.engine`
   提供；turn 的 Hook 全部经 XCore 事件总线。
5. **无补丁式迁移**：不再有"把旧核心接到 XCore 上"的适配层；运行时的装配就是
   XCore 应用的装配。

## 2. 模块布局

```
xbotv2/
  components/                  # 运行时组件包（每个都是 XCore 对象插件）
    __init__.py
    runtime.py                 # RuntimeComponent：paths/session/variables/runtime/state_store
    hooks.py                   # HooksComponent：ctx.hooks（bus-backed HookManager）
    tools.py                   # ToolsComponent：tools/commands/prompts/sandbox/permissions/job_registry/agents
    core.py                    # EngineComponent：Agent 解析 → LLM → ON_SESSION_INIT →
                               #   工具过滤 → 组装 Engine → ctx.engine（apply 即装配）
  plugin/
    bridge.py                  # 插件能力服务适配（ToolsService 等）+ PluginAdapter + plugin_runtime
    loader.py                  # 发现/依赖/配置校验 + ctx.plugin(adapter) 挂载
  hooks/manager.py             # 41 阶段契约：internal/listener 注册包装 + emit/serial 派发
  core/bootstrap.py            # 装配器：建 Context → 挂组件 → 挂插件 → 挂 engine 组件
```

装配顺序（bootstrap）：

1. `xcore.Context(data_dir=会话 state 目录)`（`ctx.state` 由此获得持久路径）。
2. 挂载 `HooksComponent` + `ToolsComponent` + `RuntimeComponent`（pending）。
3. `await ctx.start()`：组件按注册序加载，注册全部服务。
4. 注册核心 Hook（result-cache、configured hooks）与核心工具（写入共享注册表）。
5. `PluginLoader(ctx=...)` 装载内置插件（XCore Fiber 生命周期）。
6. 挂载 `EngineComponent`（注册序最后）：`apply` 内完成 Agent 解析 / 线程元数据
   / LLM 创建 / `ON_SESSION_INIT` / 工具过滤 / Engine 组装，`ctx.set("engine",
   engine)`。

## 3. Hook 契约的公开原语实现（无 XCore 内部访问）

- 注册拦截：`internal/listener`（XCore 公共事件，Cordis 语义 —— 注册 ctx 作为
  首参传入）。HookManager 的拦截处理器对 HookStage 事件名返回**重新注册的包装
  监听器**（bail 替代注册），包装器带 `__hook_contract__` 标记防递归。
- 契约包装器（注册时闭包 stage/owner）：
  - `plugin_runtime` 注入（owner = 注册 ctx → bridge 解析）；
  - 返回校验（observer 必须 None；short-circuit 必须 dict/HookDecision，
    键表校验）；guard 的 ALLOW 记录、CONTINUE 放行、DENY/STOP bail；
  - 异常策略：short-circuit 传播；strict 收集到 HookContext 收集器；
    其余记日志放行。
- 派发（`HookManager.run`）：short-circuit 阶段 → `ctx.serial`（首个 bail 值）；
  observer 阶段 → `ctx.emit`（全部执行）+ 收集器聚合 `ExceptionGroup`。
- 卸载清理：监听器是 fiber effect，插件卸载自动移除（无需手工表）。

## 4. 与早期桥接方案的差异（已消除）

| 早期（已废弃） | 现在 |
| --- | --- |
| `EventBus.hooks_for` 暴露内部 Hook 记录 | 无；契约在注册侧包装 + 公开原语派发 |
| HookManager 从总线收集原始回调 + owner | 包装器闭包注册 ctx；run 只调 emit/serial |
| bootstrap 构造一切 + 构造器注入 | 组件包挂载 + 服务解析；Engine 组件自组装 |
| 引擎在 XCore 之外 | `ctx.engine` 服务（`xbot.core` 组件） |
| `register_core_services`（生产路径） | 组件取代；该函数仅保留为测试装配便利 |

## 5. 数据/服务清单

| 服务 | 提供方 | 说明 |
| --- | --- | --- |
| `paths` / `session` / `variables` / `runtime` / `state_store` / `workspace_root` / `data_root` | RuntimeComponent | 静态运行时信息 |
| `tools` / `commands` / `prompts` / `sandbox` / `permissions` / `job_registry` / `agents` | ToolsComponent | 工具层能力（插件面服务 + 原对象） |
| `hooks` | HooksComponent | bus-backed HookManager |
| `agent_runtime` | bootstrap（非子代理时） | 子代理工厂 |
| `state` | XCore 内建 | 可恢复状态（`data_dir/state.json`） |
| `engine` | EngineComponent | 核心 loop |
