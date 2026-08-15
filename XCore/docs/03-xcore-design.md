# 03 · XCore 设计文档

> 状态：**设计稿 v1.1（审查后定稿）**。v1.0 经外部审查（`04-design-review.md`，
> 结论 redesign-required）与 Cordis 双版本调研（v3.18.1 = Koishi 实际运行版本；
> v4/vendored `@deepseek-ai/cordis`）修订。本文档为实现的唯一规格；与实现冲突时
> 以本文档修订为准（记入开发日志）。
>
> **版本说明**：XCore 主要遵循 **v4/fiber 模型**（vendored 源码可直接核对），并吸收
> v3/Koishi 的应用层约定（start/stop、required/optional inject、before- 事件糖）。
> 凡与任一版本语义有意的差异，均在 §14 决策记录中列明。

## 1. 目标与非目标

**目标**：`XCore` —— Python 3.11+、stdlib-only、零第三方依赖的以插件为核心的运行时
核心包，覆盖 Cordis 生态核心特性族：

1. **事件系统**：`on/once/off` + disposer、`prepend`/`global` 选项、六种派发原语
   `emit / parallel / serial / bail / chain / waterfall`、`before-` 事件糖、
   上下文过滤（session 谓词）、`internal/*` 同步内部事件、监听器随所属
   fiber/Context 自动清理。
2. **服务系统**：`ctx.set/get/unset/has/require`、`Service` 基类（构造即注册、
   归属 fiber）、`ctx.foo` 属性访问、服务作用域隔离（`isolate`）、服务注入
   （`inject`：required 门控 / optional 不门控，就绪即加载）。
3. **插件系统**：函数/类/对象三形态、`ctx.plugin(plugin, config?)` → `PluginHandle`
   （fiber 的 Python 视图，可 await）、Registry（按 callback 去重、map 式检查）、
   Fiber 状态机（pending/loading/running/failed/unloading/disposed）、加载失败
   不阻断、注册即逆序可逆（effect/dispose）。
4. **生命周期**：`start() / stop() / restart() / destroy()`、迭代不动点加载、
   `ready` / `dispose` 事件（active 期间注册 ready 立即执行）、stop 永不抛出、
   `ExceptionGroup` 仅用于调用方显式聚合（parallel）。
5. **可恢复状态**：`StateService`（`ctx.state`，JSON 原子写、共享缓存 + 锁、
   命名空间，崩溃/重启/stop→start 恢复）—— 注意：这是 **XCore 相对 Cordis 的一等
   公民扩展**（Cordis/Koishi 无持久 KV 状态服务，见 §14.7）。
6. **Schema 配置**：`S` DSL（any/string/number/boolean/array/object/union/enum/
   const + default/optional/strict/description），校验 + 默认值合并，
   `SchemaValidationError` 带路径。
7. **中间件**：`ctx.middleware(session, next)` 拦截链（基于 waterfall 语义）。

**非目标**：Minato 数据库、loader/配置树、HMR、tracing（`ctx.caller`）、accessor/
mixin、intercept 配置合并、reusable/fork 插件、callable service（`Service.invoke`）、
generator effect、`internal/config`/`internal/update`/`internal/get`/`internal/set`
waterfall —— 均在 §14 记录为后续项；迁移层（Step 3）所需的最小面已经齐备。

## 2. 模块划分

```
XCore/
  pyproject.toml
  xcore/
    __init__.py     # 公共 API 导出（§11）
    errors.py       # 异常层级
    events.py       # EventBus：Hook、匹配、六派发 + 同步内部派发、过滤
    service.py      # Service 基类 + ServiceStore（(label, name) 键控、通知）
    plugin.py       # 插件归一化、Registry、Fiber 状态机、effect、PluginHandle
    state.py        # StateService：JSON 原子写持久 KV + 命名空间
    schema.py       # S DSL
    context.py      # Context：组合上述一切 + 生命周期 + 中间件
  tests/            # test_events/services/plugins/lifecycle/state/schema/middleware/public_api
  docs/             # 本文档体系
```

**依赖方向（无环）**：`context.py → events/service/plugin/state/schema`；
`plugin.py → service/schema/errors`；`service.py → errors`；`events.py → errors`；
`state.py`、`schema.py` 独立。任何模块不得 import `context.py`（类型标注用
`TYPE_CHECKING`）。`Fiber` 通过 duck-typing 访问 `ctx._services`（ServiceStore）与
`ctx.fiber`，不 import Context。

## 3. 核心对象：Context

```python
class Context:
    def __init__(self, *, name="root", config=None, parent=None,
                 data_dir: Path | None = None) -> None
    # 事件（委托 root 的 EventBus；注册快照过滤链、归属当前 fiber）
    def on(self, event, listener, *, prepend=False, global_=False) -> Disposer
    def once(self, event, listener, *, prepend=False, global_=False) -> Disposer
    def off(self, event, listener) -> bool
    def before(self, event, listener, *, append=False) -> Disposer   # 糖：on("before-"+e, ..., prepend=not append)
    async def emit(self, event, *args) -> None
    async def parallel(self, event, *args) -> None
    async def serial(self, event, *args) -> Any
    async def bail(self, event, *args) -> Any
    async def chain(self, event, value, *args) -> Any
    async def waterfall(self, event, *args, next=...) -> Any
    # 过滤 / 作用域
    def filter(self, predicate, *, prepend=False) -> Disposer
    def select(self, field, value) -> Context          # 扩展：session[field]==value 子上下文
    def extend(self, **meta) -> Context
    def isolate(self, name, label=None) -> Context     # 服务作用域隔离
    # 中间件
    def middleware(self, callback, *, prepend=False) -> Disposer
    async def run_middleware(self, session) -> Any
    # 服务
    def set(self, name, value) -> Disposer             # 提供（同一作用域仅一次；None 释放）
    def get(self, name, *, strict=True) -> Any
    def require(self, name) -> Any
    def unset(self, name, value=None) -> bool
    def has(self, name) -> bool
    def __getattr__(self, name) -> Any                 # ctx.foo → get；缺失抛 AttributeError
    def __setattr__(self, name, value) -> None         # 公开名禁止直接赋值（用 ctx.set）
    # 插件
    def plugin(self, plugin, config=None) -> PluginHandle
    def inject(self, deps, callback) -> PluginHandle
    @property
    def registry(self) -> Registry
    # 效果 / 清理
    def effect(self, execute, *, label="anonymous") -> Disposer
    def dispose(self, callback) -> Disposer            # 只接受回调；无参调用抛 TypeError
    # 生命周期
    async def start(self) -> None
    async def stop(self) -> None
    async def destroy(self) -> None                    # 永久拆除本 Context 子树
    @property
    def is_active(self) -> bool
    # 结构
    @property
    def root(self) -> Context
    @property
    def parent(self) -> Context | None
    @property
    def fiber(self) -> Fiber
    @property
    def name(self) -> str
    # 状态
    @property
    def state(self) -> StateService                    # root 单例服务 "state"
```

### 3.1 共享结构（一个 EventBus / 一个 ServiceStore / 一个 Registry）

- **EventBus 是 root 单例**：所有 Context 的 `on/emit/...` 委托同一个总线；
  `Hook` 记录 `(owner_ctx, callback, options, filters快照, seq)`。监听器随所属
  fiber 卸载而移除（effect 机制）；子 Context 上的监听器照常接收 root 派发的事件
  （与 Cordis 一致：过滤是唯一 per-ctx 状态）。
- **ServiceStore 是 root 单例**：键 `(label, name)` → `Impl(value, owner_fiber)`。
- **Registry 是 root 单例**：`ctx.plugin()` 在其上注册；fiber 的父 ctx 即调用
  `plugin()` 的 ctx。

### 3.2 生命周期模型（v3 start 门控 + v4 inject 驱动）

- `ctx.plugin()` 只注册（Fiber 进入 `pending`）。加载触发路径：
  1. `await ctx.start()`（root）：置 active → **迭代不动点**：反复扫描 pending
     fiber，加载所有 inject（required）已满足者，直到无进展；等待中的保持 pending
     （不报错）；最后 `emit("ready")`。
  2. 运行期服务变化（`set`/`unset`/提供方 fiber 进入/离开 running）：通知依赖者，
     满足的 pending fiber 立即加载（**仅在 ctx.is_active 时**；未 start 前 set 服务
     不触发加载）。
  3. active 期间 `ctx.plugin()` 注册：依赖满足立即加载。
- `stop()`：置 inactive；逆序卸载全部 running fiber（卸载 → pending，注册保留）；
  `emit("dispose")`；**永不抛出**（disposer 失败逐条记日志 + `internal/error`）。
- `restart()` = `stop()` + `start()`；`ctx.state` 保留 → 可恢复。
- `destroy()`：永久拆除（stop + 全部 fiber 置 disposed + 清 Registry + 级联子
  Context + 从父移除 + 运行 root fiber 的 disposer）；之后不可再 start。
- 并发保护：root 持有 `asyncio.Lock`，`start/stop/destroy` 互斥；重复 start/stop
  为 no-op（记 warning）。
- `ready` 监听器抛错：记日志不阻断 start（`emit("ready")` 包 try/log）。
- **await handle 语义**：pending（无在途迁移）时立即返回 handle（Cordis 一致）；
  failed 时重抛 `_error`；loading 时等待完成。

### 3.3 过滤器（注册时快照，session 谓词）

- `ctx.filter(predicate)`：注册过滤器（返回 disposer）；**注册监听器/中间件时快照**
  当前 Context 的过滤链（含祖先）；派发时若首个参数（session）非 None，对快照链逐个
  求值，全真才放行；`global_=True` 跳过过滤。
- `ctx.select(field, value)`：带过滤器 `s is not None and getattr(s, field, None) ==
  value` 的子 Context。
- 语义与 Cordis 的差异（v3/v4 用 `thisArg[filter](hook.ctx)` 每次派发求值）：XCore
  用注册时快照 + session 谓词，文档注明（§14.3）。

### 3.4 效果与清理（effect 模型）

- `ctx.effect(execute, label)`：`execute` **同步**执行，必须返回 disposer（可 async
  调用）或 None；`fiber` 卸载时**逆序**执行全部 disposer（逐个 await，失败记日志
  不阻断）。async/generator effect 体 v1 不支持（抛 TypeError，§14.10）。
- `ctx.dispose(callback)`：`effect(lambda: callback)` 简写；**无参调用抛 TypeError**
  （破坏性拆除用 `destroy()`，避免误触）。
- `on/set/middleware/filter/plugin` 全部基于 effect：fiber 卸载自动清理。
- fiber 已 disposed 后注册 → `InactiveEffectError`。

## 4. 事件系统（events.py）

### 4.1 命名与匹配

- 事件名：非空字符串，`/` 分隔；`internal/` 前缀保留（框架内部，同步派发）。
- **精确匹配为主**；`*` 通配为 **XCore 扩展**（监听模式 `foo/*` 匹配 `foo/bar`；
  `*` 匹配一切；段级匹配）。派发收集精确 + 通配命中，按全局序号排序。
- 全局序号：递增计数器；`prepend` 用递减计数器（负数），保证插入队首。
- `ctx.before(name, cb, append=False)` = `on("before-"+name, cb, prepend=not append)`
  （koishi 约定）。

### 4.2 派发原语（公开 API 全部 async）

| 原语 | 语义 | 返回 | 错误 |
| --- | --- | --- | --- |
| `emit` | 串行 await 全部监听器 | `None` | 失败即传播（余下不执行） |
| `parallel` | 并发 await 全部 | `None` | `ExceptionGroup` 聚合所有失败（v4 风格）；`CancelledError` 立即重抛 |
| `serial` | 串行，遇首个 bail 值停止 | 首个 bail 值 | 传播 |
| `bail` | 同 `serial`（Python 中皆 await；§14.2） | 同 | 同 |
| `chain` | 串行值管道：上者返回值作为下者首个实参 | 最终值 | 传播 |
| `waterfall` | 环绕链：监听器 `(*args, next)`；调 `next()` 委托，不调即否决 | 最外层返回值 | 传播 |

- bail 判定：`value is not None and value is not False`（`0/""/[]/{}` 均算 bail）。
- `waterfall` 的 `next` 为 **keyword-only**（`next=_UNSET`，缺省抛 TypeError），
  监听器收到的续体为 `async () -> Any`。
- `emit` 与 Cordis 的差异：v3/v4 `emit` 是同步 fire-and-forget；Python 无法安全
  不等待 async 监听器，故为 async 串行 await（§14.1）。

### 4.3 同步内部派发（internal/*）

`internal/*` 事件从同步代码路径发出（`on()`、状态迁移、`set/unset`），用
`EventBus._emit_sync / _bail_sync`（不 await 监听器，调用 try/log 守护，镜像
vendored `emitPluginDisposed`）。内部监听器须为同步函数。

| 事件 | 参数 | 模式 | 用途 |
| --- | --- | --- | --- |
| `internal/status` | `(fiber, old_state)` | emit(同步) | fiber 状态迁移 |
| `internal/service` | `(name, value)` | emit(同步) | 服务变化（作用域过滤在 notify 路径按 label 做，不走总线） |
| `internal/dispatch` | `(mode, name, args, this_arg)` | emit(同步) | 非 internal 事件派发诊断（v4 签名） |
| `internal/listener` | `(name, listener, options)` | bail(同步) | 注册拦截：返回非 None 即替代注册（如 active 时 ready 立即执行） |
| `internal/error` | `(fiber, error)` | emit(同步) | 插件失败/effect 失败通告；默认由框架记日志 |

**明确省略**（§14.8）：`internal/config`、`internal/update`、`internal/get`、
`internal/set`、`internal/plugin`、`internal/before-service`（v3）—— 对应能力
（配置瀑布、代理读写拦截）v1 不做。

### 4.4 once / off / 并发

- `once`：包装监听器，**同步检查-标记**（触发前 `fired=True`，任何 await 之前），
  并发下只触发一次（v3 可重复触发，此为 XCore 改进，§14.4）。
- `off(event, listener)`：按回调身份精确移除（v3 core 未实现，XCore 补齐）。
- `on/once` 返回 disposer（幂等）。

## 5. 服务系统（service.py）

### 5.1 ServiceStore（root 单例，键 (label, name)）

```python
class ServiceStore:
    def __init__(self) -> None
    def set(self, label, name, value, owner_fiber) -> None   # 重复提供抛 ServiceConflictError
    def get(self, label, name, *, strict=True) -> Any        # strict: owner running
    def unset(self, label, name, value=None) -> bool
    def has(self, label, name) -> bool
    def notify(self, names, ctx) -> list[Fiber]              # 重检依赖者（见 §5.4）
```

- `ctx.set(name, value)` 语义（v3 对齐）：当前 ctx 的 isolate label 下已提供非 None
  值 → 抛 `ServiceConflictError`（**任何 fiber 皆不可重复提供**；先 `set(name,
  None)` 释放再提供）；`value is None` = 释放。返回 disposer（fiber 卸载自动释放）。
- `ctx.get(name, strict=True)`：label = 本 ctx 的 isolate 标签；未提供返回 None；
  strict 且提供方非 running 返回 None。
- `ctx.unset(name, value=None)`：移除；传 value 时校验身份；触发通知。
- 服务归属：Impl 记录 owner_fiber；fiber 卸载先释放其提供的全部服务并**等待依赖者
  安定**，再执行 disposers（§7 卸载顺序）。

### 5.2 Service 基类

```python
class Service:
    name: str = ""                     # 子类覆盖；缺省 = 类名 snake_case
    def __init__(self, ctx, *, name=None) -> None:
        self.ctx = ctx
        self.name = name or self._default_name()
        ctx.set(self.name, self)       # 构造即注册，归属当前 fiber
```

不提供 `setup`/`invoke`（类插件用 `__init__` 或 `apply`；callable service 为后续项）。

### 5.3 作用域隔离（isolate）

- `ctx.isolate(name, label=None)`：子 Context，`_isolate` 表遮蔽 `name` → 新标签；
  缺省标签为**每次调用新建的 `object()`**（同名两次 isolate 不合并；显式传同一
  label 才合并，对齐 v3 fresh Symbol 语义）。
- 服务读写按「本 ctx 的 isolate 标签」解析；隔离子树内提供的服务对父不可见，反之
  亦然。

### 5.4 注入等待（inject）

- 插件声明：`inject = ["foo"]`（全 required）或 `inject = {"required": [...],
  "optional": [...]}`（koishi 形式）。归一化为 `{name: bool(required)}`（拷贝，防
  类属性污染）。
- pending 判定：全部 required 服务的 `get(name, strict=True)` 非 None。optional 不
  门控。
- 唤醒触发点（A1 修复）：(a) `set`/`unset`；(b) 提供方 fiber 状态迁移
  loading→running / running→卸载（其提供的名字通知）。通知 → 重检依赖者 →
  满足者加载、失满足者卸载回 pending。

## 6. 插件系统（plugin.py）

### 6.1 三形态（resolve_plugin）

```python
# 函数：(ctx, config) -> Any | Disposer；属性 name/Config/inject 挂函数上
# 对象：{name?, Config?, inject?, apply(ctx, config)}；apply 须为方法
# 类：__init__(self, ctx, config)；静态字段 name/Config/inject
```

归一化 `PluginDef(name, key, callback, config_schema, inject, provide)`：
- `key` = Registry 身份：函数→函数对象；类→类；对象→对象实例（**不是**绑定方法，
  绑定方法每次访问新建对象，不可作键）。
- `callback`：函数本身 / 类 / 绑定的 `apply`。
- `config_schema`：S schema 或 dict（宽松：浅合并默认值）或 None。
- `inject`：拷贝后的依赖表。`provide`：预留（本版仅记录）。

### 6.2 Registry 与 Fiber 状态机

```
pending → loading → running ──依赖丢失/stop──→ unloading → pending（注册保留）
             │ 失败                             │ handle.dispose()/destroy()
             ↓                                  ↓
          failed（可重试）                   disposed（不可重启）
```

| 状态 | 含义 |
| --- | --- |
| `pending` | 已注册，等待 start 或等待 required 服务 |
| `loading` | callback 正在执行 |
| `running` | 已加载生效 |
| `failed` | 配置校验/callback 抛错（`_error` 保存；依赖变化或 restart 可重试） |
| `unloading` | disposer 逆序执行中 |
| `disposed` | uid 清空（handle.dispose()/destroy），不可重启 |

- Registry API：`plugin(plugin, config)` / `inject(deps, cb)` /
  `get/has/delete(plugin)` / `keys()/values()/entries()/forEach()/__len__`（v3 齐全）。
- 去重：同 `key` 的 runtime 共享（每 `plugin()` 一个新 fiber，v4 模型；无 reusable，
  §14.9）。
- `PluginHandle`：`state` / `name` / `config` / `await`（等在途迁移；pending 立即
  返回；failed 重抛）/ `dispose()`（永久卸载）/ `restart()`。

### 6.3 加载 / 卸载实现要点

- **单在途迁移**（inertia，B4）：fiber 持 `_transition: Task | None`；load/unload
  调度在途时排队（记为待办状态）；`stop()`/`dispose()` await 在途任务后再清理。
- `_load()`：校验配置（S schema → 默认值合并；失败 → failed + `internal/error` +
  日志）→ 执行 callback（类：构造 + 记录实例；函数/对象：调用）→ callback 返回
  可调用则收集为 disposer → running + `internal/status` + **通知其提供服务的依赖者**。
- `_unload()`：先释放本 fiber 提供的服务（通知 + **await 依赖者安定**），再逆序执行
  disposers（逐个 try/log）；→ pending（保留）或 disposed（显式拆除）。
- `_error`：failed 时保存；`restart()`/依赖恢复时清除并重试（A6）。
- 失败隔离：单插件失败不阻断 start/其它插件（对齐 Cordis；错误经 `internal/error`
  可观察，默认记日志）。

## 7. 生命周期细节（context.py）

- `start()`：见 §3.2（不动点加载 + ready）。
- `stop()`：置 inactive → 按**加载逆序**（逆拓扑）卸载 running fiber（逐个 await；
  失败记日志 + `internal/error`，**永不抛出**）→ `emit("dispose")`（监听器失败
  记日志）→ 完成。
- `destroy()`：`stop()` + 全部 fiber 置 disposed + Registry 清空 + 子 Context 级联
  + 从父移除 + root fiber disposers。
- 卸载顺序 = 加载逆序（记录每个 fiber 的 load 序号，stop 按序号降序）。
- `ready` 语义：start 完成后 emit；active 期间新注册 `ready` 监听器立即调度执行
  （`internal/listener` 拦截实现）。
- 可恢复性：stop→start 重建 fiber（apply 重跑），状态从 `ctx.state` 恢复；插件
  内存态本就不应依赖持久（与 Cordis 契约一致，§14.7）。

## 8. 状态服务（state.py）

```python
class StateService:
    def __init__(self, *, path: Path) -> None          # path 必填（生产显式传 data_dir/state.json）
    async def get(self, key, default=None) -> Any
    async def set(self, key, value) -> None            # 校验 JSON 可序列化；立即原子落盘
    async def delete(self, key) -> None
    async def clear(self) -> None
    async def keys(self) -> list[str]
    async def all(self) -> dict[str, Any]
    def namespace(self, prefix: str) -> StateService   # 前缀视图，共享缓存与锁
```

- **共享缓存 + 锁**（E1）：`_shared = (path, data, asyncio.Lock)`；全部视图（含
  namespace）共享同一 `data` 与锁；`set/delete` 持锁做 read-modify-write 后
  `_atomic_write`（临时文件 + `os.replace`，UTF-8）；**并发写不丢键**。
- 惰性读盘（首次访问）；文件损坏 → 抛 `RuntimeError`（不静默恢复，XBot 纪律）。
- 崩溃恢复：原子写保证无半写文件；测试用「残留临时文件」模拟。
- **注册为服务**（E2）：root Context 首次访问 `ctx.state` 时创建并以 root fiber
  `set("state", svc)` —— 于是 `inject: ["state"]` 可用、`ctx.state` 与 `ctx.get`
  一致。
- 命名空间：`namespace("goal")` → 键 `"goal.<key>"`，per-plugin 隔离（对应 XBotv2
  `PluginStore` 迁移映射）。

## 9. Schema（schema.py）

```python
S.any() | S.string() | S.number() | S.boolean() | S.array(item)
S.object({...}) | S.union([...]) | S.enum([...]) | S.const(v)
# 修饰：.default(v)  .optional()  .strict()  .description(text)
schema.validate(config) -> validated（带默认值副本）
```

- 语义（对齐 schemastery 核心）：`object` **默认保留未知键**；`.strict()` 丢弃未知
  键；缺键：有 `.default` → 深拷贝默认值；`.optional()` → 省略；否则抛
  `SchemaValidationError(path, message)`。`array/object/union` 递归，错误带
  `$.a.b[0]` 路径。`union` 按序尝试。`number` 拒绝 bool。
- 插件 `Config`：S schema → 自动校验+默认值；普通 dict → 浅合并默认值（宽松）；
  None → 不校验。
- 与 pydantic 的关系：不用 pydantic（stdlib-only）；S DSL 是 schemastery 风格的自
  定义实现。

## 10. 中间件（context.py）

- `ctx.middleware(callback, *, prepend=False)`：`callback(session, next)`；注册在
  root 中间件表（记录 owner/filters 快照/序号）。
- `await ctx.run_middleware(session)`：**每次调用重建链快照**（可运行期增删）、
  过滤链求值、waterfall 组合（不调 next 即短路）；返回短路值或 None。
- 错误传播；disposer 可撤。koishi 的 `next(cb)` 临时压栈与深度上限为后续项。

## 11. 公共 API 与模块树（errors.py 纳入）

```python
# xcore/errors.py
XCoreError(Exception)                      # 基类
InactiveEffectError(XCoreError)            # 已销毁 fiber 上创建效果
ServiceNotFoundError(XCoreError)           # require 未找到
ServiceConflictError(XCoreError)           # 重复提供
SchemaValidationError(XCoreError)          # 唯一校验错误类型（schema.py 定义，errors 导出）

# xcore/__init__.py
from xcore.context import Context
from xcore.service import Service
from xcore.plugin import Registry, PluginHandle, PluginDef, FiberState
from xcore.state import StateService
from xcore.schema import S
from xcore.events import EventBus, Disposer
from xcore.errors import (XCoreError, InactiveEffectError, ServiceNotFoundError,
                          ServiceConflictError, SchemaValidationError)
__version__ = "0.1.0"
```

- 不再导出 `PluginLoadError`/`PluginValidationError`：`handle.await()` 重抛原始
  错误（对齐 Cordis）；`Registry.plugin` 对非法形态抛 `TypeError`。
- 内部事件表见 §4.3；公共事件：`ready`、`dispose`。
- `test_public_api.py` 对照 `docs/features/api.md` 校验导出清单。

## 12. 迁移兼容性备注（Step 3 输入）

| XBotv2 现状 | XCore 承接 |
| --- | --- |
| `PluginBase + plugin.yaml` | 对象插件（apply 承载 setup；manifest 作元数据） |
| `PluginSetupContext.register_hook(stage, fn)` | `ctx.on(stage.value, fn)` + 迁移层阶段契约 |
| `PluginStore` | `ctx.state.namespace(plugin_name)` |
| `HookManager` 41 阶段 | 命名事件 + 迁移层封装 |
| 构造器注入核心组件 | 核心组件注册为服务（`ctx.set("tools", ...)` 等） |
| `on_load/on_unload/setup` | apply / effect disposer / `ctx.on("ready")` |
| logger | stdlib `logging`（`xcore` logger）；callable logger 服务为后续项 |

## 13. 测试策略

- 每模块一测试文件，pytest-asyncio `asyncio_mode=auto`；临时目录；不触碰真实数据。
- 关键场景（含审查新增）：五/六派发原语与 bail 判定边界；once 并发单触发；通配
  匹配矩阵；过滤器快照与 select；isolate 标签隔离；inject 等待/唤醒（含
  **服务在插件 apply 内注册 → 依赖者随后加载**的 A1 场景）；required 成环保持
  pending 不挂死；Registry 去重（对象插件身份）；start 不动点（注册序无关）；
  **stop 期间 async apply 在途**（B4）；**同 tick 双 set**；ready 监听器抛错不
  楔死；state 并发 namespace 写不丢键 + 崩溃恢复；schema 默认值/未知键/路径错误；
  middleware 短路；公共 API 清单。

## 14. 设计决策记录（含与 Cordis 的差异）

1. `emit` async 串行 await（v3/v4 为同步 fire-and-forget）——Python 语义安全。
2. `bail`/`serial` 皆 await（v3 bail 不 await，async 监听器返回 Promise 即 bail；
   XCore 检查解析后的值）——改进，文档注明。
3. 过滤器 = 注册时快照 + session 谓词（Cordis = 派发时 `thisArg[filter](hook.ctx)`）
   ——面向 bot 场景的便捷语义；`select` 为 XCore 扩展（Cordis 无此 API）。
4. `once` 并发单触发（Cordis 可双触发）——改进。
5. `parallel` 聚合 `ExceptionGroup`（v4 风格；v3 首错即抛）。
6. 生命周期 start 门控 + inject 驱动（v3 + v4 混合）；stop 永不抛出。
7. `StateService` 为 XCore 一等公民扩展（Cordis/Koishi 无持久 KV；其持久化靠
   Minato 数据库与配置文件）。
8. 省略 `internal/config/update/get/set/plugin` 事件（v1 无对应能力）。
9. 无 reusable/fork 插件（v4 模型：每次 plugin() 一个 fiber）。
10. effect 仅支持同步体返回 disposer（v4 支持 async/generator 体）——v1 简化。
11. 通配符为监听侧扩展（Cordis 精确匹配 + `internal/dispatch` 拦截）。
12. `ctx.off`、`ctx.before`、`ctx.chain` 为补充便捷 API（v3 core 部分未实现）。
13. `ctx.set` 重复提供抛错（v3 语义）；释放用 `set(name, None)` 或 `unset`。
14. `ctx.foo` 缺失抛 AttributeError（v4 语义；v3 警告返回 undefined）——fail loud。
15. `S.object` 默认保留未知键、`.strict()` 丢弃（schemastery 语义）。
