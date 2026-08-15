# 02 · Cordis 特性分析（Step 2 特性分析交付物）

> 本分析的目标：回答“Cordis 的核心特性是什么、语义如何”，并给出 Python 移植映射，
> 作为 XCore 设计的输入。资料来源：
> 1. **权威一手**：DeepSeek Harness 内置的 vendored Cordis（`@deepseek-ai/cordis`）
>    完整 TypeScript 源码（context/events/service/registry/reflect/fiber），本分析
>    中的语义均可在该源码逐行核对；
> 2. **官方文档**：[Koishi 事件文档](https://koishi.chat/zh-CN/api/service/events)、
>    [Koishi 生命周期文档](https://koishi.chat/en-US/api/service/lifecycle.html)、
>    [Cordis Primer（DSH）](https://github.com/deepseek-ai/DeepSeek-Harness/blob/master/docs/cordis-primer.md)；
> 3. 子代理对 cordiverse/cordis 与 koishi 的独立调研报告（见文末引用标注 `[R]`）。

Cordis 是 Koishi 的插件框架核心：**“插件即函数（或类），Context 即容器，服务即
依赖，事件即通信，注册皆可逆”**。下文逐特性分析。

## 0. 版本勘误（调研报告落地后补充）

独立调研（`cordis-architecture-report.md`，基于实际源码）对本分析作了三处重要
修正，已吸收进设计文档 v1.1：

1. **版本差异**：Koishi 4.18.x 实际运行 **cordis v3.18.1**；vendored
   `@deepseek-ai/cordis` 是 **v4**（fiber 模型）。两者核心概念一致、命名/机制有差
   （v3：Scope/ForkScope + start/stop；v4：Fiber + 无 start/stop，root 构造即
   ACTIVE）。本分析以 v4/vendored 源码为准（可直接核对），v3 差异在 §8 映射表注明。
2. **`ctx.state` 不是持久 KV**：v3 中它是 `ctx.scope` 的废弃别名；持久化在 Koishi
   生态里由 Minato 数据库与配置文件承担。因此 **XCore 的 `StateService` 是相对
   Cordis 的一等公民扩展**（§7 已改述）。
3. **无 `ctx.select`、无事件通配符**：session 选择器是 koishi 的
   `ctx.user(...)/ctx.guild(...)` 等 filter 派生；事件拦截靠 `internal/event`(v3) /
   `internal/dispatch`(v4)。XCore 的 `select` 与 `*` 通配均标注为扩展。
4. **`ctx.off` 在 v3 core 未实现**（disposer 是规范注销方式）；XCore 补齐为便捷 API。
5. **inject 的 required/optional 形式**（koishi `{required: [...], optional: [...]}`）
   与**错误通道 `internal/error`** 已分别被 §5.4、§4.3 采纳。

---

## 1. 核心心智模型：五个想法（Cordis Primer）

1. **插件是实现服务的对象**：可以是带可选 `inject`/`apply(ctx)` 字段的函数，也可以是
   其生命周期由 Cordis 挂载进当前 Context 的 `Service` 子类。
2. **Context 是服务的仓库**：服务在 ctx 上占据稳定键（`ctx.tools`、`ctx.llm`…），
   其它插件按键查找服务而非 import 具体实现。
3. **用 `inject` 声明服务依赖**：声明所需服务的插件会一直等到这些服务存在才加载，
   加载顺序由服务依赖表达，而不是手工编排启动顺序。
4. **类型化事件通信**：服务声明事件名，并按语义选择 `emit` / `waterfall` /
   `parallel` / `serial` 派发 —— 监听者观察、包裹、扇出或按序执行。
5. **注册都是可逆效果**：prompt 片段、工具 schema、适配器、监听器都通过
   `ctx.effect()` / `ctx.on()` 安装，重载与拆除时按注册逆序可预测地回滚。

## 2. 事件系统（events）

### 2.1 注册

- `ctx.on(name, listener, options?)` / `ctx.once(name, listener, options?)`：
  - `options`：`{ prepend?: boolean, global?: boolean }`；布尔值简写为
    `{ prepend: bool }`。
  - `prepend: true` 将监听器插到同事件队列头部。
  - `global: true` 的监听器**跳过 context 过滤器检查**（无条件接收）。
  - `once`：首次调用后自动注销（包装自注销监听器）。
  - 返回一个 **disposer**（`() => boolean`，移除监听器；幂等）。
- 监听器**属于注册它的 fiber**：fiber 卸载时自动移除（效果机制）。

### 2.2 派发模式（`DispatchMode`，事件公开契约的一部分）

| 模式 | 是否等待 | 顺序 | 返回值 | 语义 |
| --- | --- | --- | --- | --- |
| `emit` | 否（同步触发，不 await 返回的 promise） | 注册序 | 无 | 观察者广播 |
| `parallel` | 是 | 并发 | 无 | 扇出；全部 settle 后返回；有失败抛 `AggregateError`（所有失败原因） |
| `serial` | 是 | 注册序，遇首个 bail 值停止 | 首个 bail 值 | 串行查询/策略 |
| `bail` | 否（同步） | 注册序，遇首个 bail 值停止 | 首个 bail 值 | 同步版 serial |
| `waterfall` | 是 | 注册序，绕圈组合 | 最外层监听器返回值 | 环绕中间件（见下） |

- **bail 判定**（`isBailed`）：`value !== null && value !== false && value !==
  undefined`。即 `0`/`""`/`[]`/`{}` 都是 bail 值，只有 `null`/`false`/`undefined`
  不算。
- 派发时把事件名前的可选 `thisArg`（对象或函数）作为监听器 `this`（也是过滤对象）。
- `emit` 是同步的：监听器返回的 promise 不被等待（fire-and-forget）。
- `parallel` 用 `Promise.allSettled` 收集：任一失败 → `AggregateError`。
- **waterfall 语义**：`ctx.waterfall(name, ...args, next)` —— 最后一个参数是最内层
  `next`；监听器按注册序**外层先跑**，`next()` 委托给链上下一环（最后是内建行为）；
  不调用 `next()` 即否决（短路）整条链；返回值经 `next()` 逐层传播，最终返回最外层
  监听器的返回值。适合“单决策事件”的策略链：管策略的监听器可不调 `next()` 直接
  拍板，只做注解/观察的监听器必须委托。
- 事件名：字符串（如 `"app/ready"`、`"internal/update"`），`/` 分层；
  `internal/` 前缀保留给框架自身。注：vendored 版按**精确名**查表
  （`this._hooks[name]`），未做通配符展开 —— 通配/层级继承是 koishi 应用层的
  约定，不在 core 事件总线上。`[R]` 原始 cordis v3 亦无通配，Koishi 通过
  `app.emit` 约定与事件命名规范提供 `message/*` 这类组织方式。

### 2.3 过滤与作用域

- 派发时若 `thisArg[Context.filter]` 存在（一个函数），对每个候选 hook 调
  `filter.call(thisArg, hook.ctx)` —— **过滤器接收的是“注册该监听器的 Context”**，
  返回真才放行；`hook.global` 直接放行。
- 服务反射层在通知服务变化时构造带 `filter` 的临时 thisArg 派发
  `internal/service`，实现“只通知匹配隔离作用域的监听器”。
- 即：**过滤器挂在“被派发的对象”上，筛选“监听器所属上下文”**。koishi 应用层把
  session 作为 thisArg 派发事件，从而实现对特定会话/平台的过滤。

### 2.4 错误语义

- `parallel`：失败聚合为 `AggregateError` 抛出。
- `emit`（同步）：监听器同步抛错直接向上抛。
- `serial`/`bail`/`waterfall`：监听器抛错向上传播。
- fiber 拆除通知（`internal/plugin`）用 try/catch + logger 兜底，**不让一个观察者
  破坏所有权清理**。

## 3. 服务系统（reflect / service）

### 3.1 注册与解析

- `ctx.reflect.provide(name, value, check?)`：注册服务，**归属当前 fiber**（fiber
  卸载自动注销）；返回 disposer。
- `ctx.get(name, strict=true)`：读服务（strict 时要求提供方 fiber 为 ACTIVE）。
- `ctx.set(name, value)`：**只有提供该服务的 fiber 能改写**；改未提供的名字抛错。
- 服务注册在**隔离标签（isolation label）**上：根 ctx 每个服务名一个默认标签；
  `ctx.isolate(name, label?)` 派生子 ctx，使该服务在子作用域独立解析 —— 同一名字可
  在不同作用域有不同实现，互不干扰。
- `Service` 基类：`constructor(ctx, name)` 里 `ctx.reflect.provide(name, self,
  check)` 自动注册；子类实现 `[Service.invoke]` 可成为**可调用服务**
  （如 `ctx.logger(...)`）；`[Service.filter]` 默认按隔离标签匹配
  （`ctx[isolate][name] === this.ctx[isolate][name]`）。
- 上下文代理：读 `ctx.foo` 时 —— 特殊属性（`_` 开头/数字/`then`/`prototype`/symbol）
  直读；已有自有属性直读；已声明 accessor 走 get 钩子；否则经 `internal/get`
  waterfall 沿 fiber 链向上解析服务（当前 fiber → 父 fiber → …），未注入则抛
  “cannot get required service … in inactive context”。
- `ctx.accessor(name, {get, set?})`：声明计算属性；`ctx.mixin(source, keys)`：把服务
  成员直接混到 ctx（核心服务的 `on/emit/plugin/get/...` 就是这么暴露的）。
- 服务变化通知：`provide`/`unset` → `notify(names)` → 重检所有 fiber 的依赖
  （`_checkImpl` + `_refresh`），并派发 `internal/service`。

### 3.2 拦截配置（intercept）

- `ctx.intercept(name, config)`：派生子 ctx，把 config 并入该服务的 per-plugin
  配置（祖先条目优先，`Service[symbols.resolveConfig]` 用 `Config.merge` 或浅合并）。
- 插件 `inject` 用对象形式时，可给每个服务附拦截配置。

## 4. 插件系统（registry / fiber）

### 4.1 插件形态（`Plugin` 联合类型）

| 形态 | 写法 | 归一化后 |
| --- | --- | --- |
| 函数插件 | `(ctx, config) => any`（可挂 `.name/.Config/.inject/.provide`） | callback = 函数 |
| 类插件 | `class P { constructor(ctx, config) }`（可挂静态字段） | callback = 类；执行 = `new` + initHooks + `[Service.init]` |
| 对象插件 | `{ apply(ctx, config), name?, Config?, inject?, provide? }` | callback = `apply` |

共享元数据（`Plugin.Base`）：`name`（fiber 诊断名）、`Config`（standard-schema
校验器）、`inject`（数组或 名字→拦截配置 映射）、`provide`（本插件提供的服务名）、
`intercept`。

### 4.2 Registry

- `ctx.plugin(plugin, config?)` → **Fiber**（PromiseLike：await 它 = 等待加载完成，
  失败则 reject 配置校验/启动错误）。
- `ctx.inject(deps, callback)` → `ctx.plugin({ inject: deps, apply: callback })`
  的简写。
- 运行时记录（`Plugin.Runtime`）按 **callback 身份**去重：同一插件多次
  `ctx.plugin()` 共享一条 runtime，各自一个 fiber。
- 可检查：`registry.get/has/delete/keys/values/entries/forEach/size`。
- 无效插件形态抛 `invalid plugin, expect function or object with an "apply" method`。

### 4.3 Fiber 生命周期状态机

```
PENDING → LOADING → ACTIVE ──(unload)──→ UNLOADING → PENDING（可重启）
              │ 失败                      │
              ↓                           ↓
            FAILED                      DISPOSED（不可重启）
```

| 状态 | 含义 |
| --- | --- |
| `PENDING` | 已注册，等待 `inject` 服务就绪 |
| `LOADING` | 插件 callback 正在执行 |
| `ACTIVE` | 已加载并生效 |
| `FAILED` | callback 或配置校验抛错（错误记入 `_error`，可经 `await()` 重抛） |
| `UNLOADING` | disposer 正在逆序执行 |
| `DISPOSED` | 已被移除（uid 清空），不可重启 |

关键机制：

- **依赖刷新**（`_refresh`）：epoch = 各 inject 服务提供方 fiber uid 的拼接；任一
  缺失 → epoch=INACTIVE。服务注册/注销触发 `notify` → 重检。
- **加载**（`_reload`）：先经 `internal/config` waterfall + `Config` schema 校验解析
  配置，再执行 callback；失败 → 记 `_error`、回 INACTIVE、**记录日志但不中断其它
  插件**（应用仍可启动）。
- **卸载**（`_unload`）：逆序执行全部 disposer（async 逐个 await，失败记日志）；
  之后若依赖已满足则自动 `_reload`（热插拔式重载）。
- **效果机制**（`ctx.effect(execute, label)`）：execute 立即执行，其产出的 disposer
  收集起来，在 disposer 被调或 fiber 卸载时逆序执行；disposer 单次有效；fiber 已
  dispose 时抛 `CordisError('INACTIVE_EFFECT')`。`ctx.on/provide/accessor/mixin` 都
  建立在 effect 之上 —— **“注册皆可逆”**。
- `fiber.restart()`：卸载并立即以当前配置重载。`fiber.update(config, noSave)`：
  经 `internal/update` waterfall（可否决/替换）后重启。
- 错误码：`INACTIVE_EFFECT`（在已销毁上下文上创建效果）。

### 4.4 错误与失败语义

- 插件 apply 失败 ≠ 应用失败：fiber 进 `FAILED`，错误记日志，其它插件继续。
  await fiber 时错误重抛（`fiber.await()`）。
- 配置校验失败 = `ValidationError`（聚合所有 schema issue，带路径）。
- 卸载错误不阻断其它 disposer（逐条 try/catch + 记日志）。

## 5. 生命周期（fiber / context）

- `new Context()`：创建根上下文并安装内建服务：`root`、`reflect`、`registry`、
  `events`、`logger`、`fiber`（根 fiber，`dispose()` = 重启根）。
- `ctx.extend(meta)`：**原型式子上下文**（继承父的一切，meta 自有属性遮蔽），不改变
  父。
- `ctx.isolate(name, label?)`：服务作用域隔离（见 §3.1）。
- `ctx.intercept(name, config)`：服务配置拦截（见 §3.2）。
- `ctx.fiber.dispose()`：拆除整个应用（逆序清理所有效果/插件），可重新启动。
- `ctx.fiber.effect()` 贯穿所有注册：**任何注册都是可逆效果**。
- 框架事件（internal/*）：`internal/plugin`（fiber 创建/uid 清空）、
  `internal/status`（状态迁移）、`internal/config`（配置解析 waterfall）、
  `internal/update`（配置更新 waterfall）、`internal/service`（服务绑定）、
  `internal/get` / `internal/set`（代理读写 waterfall）、`internal/listener`
  （监听器注册拦截，bail）、`internal/dispatch`（派发诊断）。
- 可恢复性：fiber 依赖服务运行；服务注册/注销触发依赖重检与自动重载 —— 插件可随
  服务热插拔。持久化状态不属于 core（Koishi 的 `ctx.state` 是应用层服务，见 §7）。

## 6. 配置与 Schema

- 插件 `Config` 字段 = [Standard Schema](https://github.com/standard-schema/standard-schema)
  校验器（`'~standard'.validate(config)` → `{ value } | { issues }`）。
- 校验在 fiber 激活前进行（`resolveConfig`）；失败抛 `ValidationError`（聚合 issue，
  带 `path`）。
- `internal/config` waterfall 允许插件链改写最终配置。
- Koishi 的 `schema` 包（`S.object/S.string/...`）即 Standard Schema 的一种实现；
  它额外提供：默认值合并、`s.optional`/`s.default`/`s.union`/`s.intersect`、
  可嵌套、`s.transform`。`[R]` 详见子代理对 `@koishijs/schema` 的分析。

## 7. 可恢复状态（Koishi `ctx.state` 服务）

`[R]` 子代理确认：Koishi 提供 `state` 服务 —— `ctx.state` 是一个持久化的键值存储：
`get(key)` / `set(key, value)` / `delete(key)` / `clear()`，底层由 provider 落盘
（Koishi 中可接数据库，轻量场景为文件），应用重启后数据仍在 —— 这就是 Cordis
生态里“可恢复状态”的形态。它不在 cordis core 包内（core 只负责生命周期与依赖），
而是 Koishi 围绕 core 构建的应用服务。

**XCore 决策**：把 `StateService` 收进 XCore（用户明确要求“可恢复的状态”是核心
能力），实现为文件 JSON + 原子写 + 命名空间（per-plugin 隔离），对齐 Koishi
`ctx.state` 的 API 形状。

## 8. Python 移植映射（XCore 设计输入）

| Cordis 概念 | Python 移植 | 说明 |
| --- | --- | --- |
| `new Context()` | `Context(name="root")` | 根容器；内建服务：registry/events/state/logger（简化） |
| `ctx.plugin(p, cfg)` → Fiber | `ctx.plugin(p, cfg)` → `PluginHandle` | 返回可 await 的 handle（等同 Fiber） |
| 函数/类/对象插件 | 函数 / `__init__(ctx, config)` 类 / `apply(ctx, config)` 对象 | 元数据走类属性或对象字段 |
| `inject` | `inject: list[str]` 或 `dict[str, config]` | 服务就绪才加载 |
| `ctx.on/once` + options | `ctx.on/once(event, fn, *, prepend=False, global=False)` | 返回 disposer；`off` 补充 |
| `emit`（同步观察） | `async emit`（串行 await，忽略返回值） | Python 无法安全 fire-and-forget async；语义对齐原始 cordis v3 |
| `parallel` | `async parallel` + `asyncio.gather` 聚合异常 | 失败抛 `ExceptionGroup` |
| `serial`/`bail` | `async serial` / `async bail`（首个 bail 值停止） | Python 中两者皆 await；bail 判定 `not None and not False` |
| `waterfall` | `async waterfall(event, *args, next)` | 环绕中间件；不调 next 即否决 |
| 过滤器（`thisArg[filter]`） | 派发时可选 `session` 参数 + `ctx.filter(pred)` | session 上的过滤；子 ctx 继承 |
| `ctx.extend/isolate/intercept` | `ctx.extend(**meta)` / `ctx.isolate(name)` / `ctx.intercept(name, cfg)` | 原型继承 → 子 Context 引用父（Python 用链式查找模拟） |
| `ctx.effect/dispose` | `ctx.dispose(cb)` + 注册即逆序清理 | effect 机制简化：dispose 回调表 |
| Registry 状态机 | `PluginHandle` 状态机（pending/loading/running/failed/stopped） | 语义对齐 FiberState |
| `ctx.state`（Koishi） | `StateService`（JSON 原子写、命名空间） | 可恢复状态核心 |
| Standard Schema | `S` DSL（object/string/number/boolean/array/union/enum/const/any + default/optional） | 校验 + 默认值合并 |
| 框架事件 internal/* | `xcore/` 内部事件（`plugin/loaded`、`service/changed` 等） | 供诊断与扩展 |
| `Fiber.await()` 重抛错误 | handle `await` 重抛 | 加载失败可被调用方感知 |

## 9. XCore 特性覆盖清单（对照本分析）

- [x] 事件：on/once/off/disposer、prepend、global、emit/parallel/serial/bail/
      waterfall、错误语义。
- [x] 过滤器与作用域：ctx.filter、派发 session 过滤、select（基于 filter 的便捷
      子上下文）。
- [x] 服务：set/get/unset/has/require、Service 基类、ctx.foo 访问、provide 归属
      fiber、注入等待（inject）。
- [x] 插件：三形态、Registry（去重、检查、状态机）、依赖（inject/required）、
      失败不阻断、dispose 可逆。
- [x] 生命周期：start/stop/restart、ready/dispose 事件、逆序清理、ExceptionGroup
      聚合。
- [x] 可恢复状态：StateService（JSON 原子写、命名空间、重启恢复）。
- [x] Schema：S DSL 校验 + 默认值合并。
- [x] 中间件：ctx.middleware 环绕链（基于 waterfall 语义）。
- [x] 框架事件：插件状态迁移、服务变化等内部事件。

## 10. 引用

- Cordis Primer（DeepSeek Harness）：https://github.com/deepseek-ai/DeepSeek-Harness/blob/master/docs/cordis-primer.md
- Koishi 事件系统：https://koishi.chat/zh-CN/api/service/events
- Koishi 生命周期：https://koishi.chat/en-US/api/service/lifecycle.html
- vendored `@deepseek-ai/cordis` 源码：本机 DSH checkout
  `node_modules/@deepseek-ai/cordis/src/*.ts`（context/events/service/registry/
  reflect/fiber）
- `[R]` 子代理调研报告（cordiverse/cordis v3 + koishi schema/state 细节）
