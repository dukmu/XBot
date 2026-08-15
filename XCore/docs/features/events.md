# 事件系统（Events）

> 实现对照：`xcore/events.py`、`xcore/context.py`。语义规格见
> `../03-xcore-design.md` §4。

## 注册与注销

```python
disposer = ctx.on("message", handler)                 # 返回 disposer
disposer = ctx.on("message", handler, prepend=True)   # 插到队首
disposer = ctx.on("message", handler, global_=True)   # 跳过过滤器
disposer = ctx.once("message", handler)               # 最多触发一次
ctx.off("message", handler)                           # 按回调身份移除
```

- 监听器属于注册它的 fiber：fiber 卸载时自动移除（无需手动清理）。
- disposer 幂等（第二次调用返回 `False`）。
- `once` 并发安全：同步检查-标记，任何 await 之前置位，并发派发只触发一次
  （比 Cordis v3 更强的保证，§14.4）。
- `ctx.before(name, cb, append=False)` = `on("before-"+name, cb, prepend=not append)`
  （koishi 约定，默认 prepend）。

## 事件命名

- 非空字符串，`/` 分隔层级（`"message/created"`）。
- 精确匹配为主（Cordis core 语义）；**通配为 XCore 扩展**：监听 `"foo/*"` 收到
  `foo/bar`；监听 `"*"` 收到一切。`*` 只能作为完整段（`"fo*o"` 非法）。
- 多监听器按注册序执行（通配与精确混合时按全局注册序）。
- `internal/` 前缀保留给框架内部事件（同步派发，见下）。

## 六种派发原语

| 原语 | 语义 | 返回 |
| --- | --- | --- |
| `await ctx.emit(name, *args)` | 串行 await 全部监听器 | `None` |
| `await ctx.parallel(name, *args)` | 并发 await 全部 | `None`（失败聚合 `ExceptionGroup`） |
| `await ctx.serial(name, *args)` | 串行，遇首个 bail 值停止 | 首个 bail 值 |
| `await ctx.bail(name, *args)` | 同 serial（Python 皆 await） | 同 |
| `await ctx.chain(name, value, *args)` | 值管道：上者返回作为下者首参 | 最终值 |
| `await ctx.waterfall(name, *args, next=...)` | 环绕中间件链 | 最外层返回值 |

**bail 判定**：`value is not None and value is not False`（`0/""/[]/{}` 均算 bail；
只有 `None`/`False` 继续）。

**waterfall 用法**：

```python
async def policy(session, next_fn):
    if not allowed(session):
        return "denied"          # 不调 next() 即否决整条链
    return await next_fn()       # 委托给下一环

ctx.on("check", policy)
result = await ctx.waterfall("check", session, next=builtin_behaviour)
```

监听器签名：`(session_or_args..., next_fn)`；`next_fn` 为 `async () -> Any`。
`next` 在派发侧是 keyword-only（`ctx.waterfall("evt", s, next=f)`），监听器侧是
**位置参数**（与 Cordis 一致）。

**错误语义**：`emit`/`serial`/`bail`/`chain`/`waterfall` 中监听器抛错向上传播；
`parallel` 聚合所有失败为 `ExceptionGroup`（v4 风格），`CancelledError` 立即重抛。

## 过滤器与作用域

```python
ctx.filter(lambda s: s.platform == "qq")        # 之后注册的监听器受过滤
scoped = ctx.select("platform", "qq")           # 便捷子上下文（XCore 扩展）
scoped.on("message", handler)                   # 只在 platform=="qq" 时触发
```

- 过滤器在**注册时快照**：`filter()` 之后的 `on()` 快照当前 Context 的过滤链
  （含祖先）；过滤器随后撤销不影响已注册监听器。
- 过滤对象是派发的**首个参数**（session）；无参数派发不过滤；`global_=True` 跳过。
- `select(field, value)` 等价于带 `session[field] == value` 过滤器的子 Context。

## 内部事件（internal/*）

从同步代码路径发出（`on()` 拦截、fiber 状态迁移、服务变化），用同步派发，监听器
须为同步函数；失败记日志不传播：

| 事件 | 参数 | 用途 |
| --- | --- | --- |
| `internal/status` | `(fiber, old_state)` | fiber 状态迁移 |
| `internal/service` | `(name, value)` | 服务注册/注销/变化 |
| `internal/dispatch` | `(mode, name, args)` | 非 internal 事件派发诊断 |
| `internal/listener` | `(name, listener, options)` | 注册拦截（bail；返回非 None 即替代注册，如 active 时 ready 立即执行） |
| `internal/error` | `(fiber, error)` | 插件失败/effect 失败通告 |

## 公共事件

| 事件 | 触发时机 |
| --- | --- |
| `ready` | `start()` 完成插件加载后；active 期间新注册的 ready 监听器**调度为任务**，在下一个事件循环轮次执行（与 Cordis 一致；观测前需让出事件循环，如 `await asyncio.sleep(0)`） |
| `dispose` | `stop()`/`destroy()` 在卸载 fiber **之前**发出（监听器仍存活） |
