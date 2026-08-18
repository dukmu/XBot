# 插件系统（Plugins）

> 实现对照：`xcore/plugin.py`。语义规格见 `../03-xcore-design.md` §6。

## 三种插件形态

```python
# 函数插件（最常用）
def my_plugin(ctx, config):
    ctx.on("message", handler)
    def cleanup():
        ...                    # 返回可调用即作为 disposer（async 也可）
    return cleanup
my_plugin.inject = ["database"]          # 可选：服务依赖
my_plugin.Config = S.object({...})       # 可选：配置 schema
my_plugin.name = "my_plugin"             # 可选：显示名（缺省函数名）

# 对象插件
class MyPlugin:
    name = "my_plugin"
    inject = ["database"]
    Config = S.object({...})
    def apply(self, ctx, config): ...

# 类插件
class MyPlugin:
    def __init__(self, ctx, config): ...  # 构造即插件体
```

- `apply`/函数体签名统一为 `(ctx, config)`。
- 返回 disposer（可 async）：fiber 卸载时逆序执行。
- 配置校验失败或 apply 抛错 → fiber 进入 `failed` 态，**不阻断其它插件与应用启动**；
  错误经 `handle.await()` 重抛（原始异常，不包装）。

## ctx.plugin 与 PluginHandle

```python
handle = ctx.plugin(my_plugin, {"option": 1})
await handle                 # 等待加载完成；失败重抛；pending（等依赖）立即返回
handle.state                 # FiberState: pending/loading/running/failed/unloading/disposed
handle.name / handle.config / handle.uid
await handle.dispose()       # 永久卸载（不可重启）
await handle.restart()       # 卸载并重载
```

- 注册与加载解耦：`ctx.plugin()` 只注册；加载由 `start()` 不动点或运行期服务变化
  触发（见 `../features/lifecycle.md`）。
- 同一插件（同一 callback 身份）可多次 `ctx.plugin()`：共享 runtime，各自独立 fiber
  （v4 模型，无 reusable/fork）。
- `ctx.registry`：`get/has/delete(plugin)`、`keys()/values()/entries()/forEach()/len`。
  `registry.delete(plugin)` 一次性拆除该插件的全部实例。
- 插件嵌套：apply 内 `ctx.plugin(child)` 挂载子插件；父插件卸载时子插件递归卸载
  （子 fiber 的拆除是父 fiber 的一个 disposer）。

## Fiber 状态机

```
pending → loading → running ──依赖丢失/stop──→ unloading → pending（注册保留）
             │ 失败                             │ handle.dispose()/destroy()
             ↓                                  ↓
          failed（可重试）                   disposed（不可重启）
```

- **单在途迁移**：fiber 用 `asyncio.Lock` 串行化所有加载/卸载；并发触发排队。
- **失败回滚**：apply 中途抛错时，已注册的部分 effect 逆序回滚（Cordis 语义），
  再进入 `failed`。
- **失败重试**：`failed` 状态在依赖变化（新的 settle 触发）或 `restart()` 时重试，
  不在同一 settle 内死循环。
- **卸载顺序**：先释放本 fiber 提供的服务并等待依赖者安定（B5），再逆序执行
  disposers（失败记日志不阻断）。

## effect 与可逆注册

```python
ctx.effect(lambda: disposer_fn, label="...")   # execute 立即执行，返回 disposer
ctx.dispose(cleanup_fn)                        # effect 简写；无参调用抛 TypeError
```

- `on/set/middleware/filter/plugin` 全部基于 effect：fiber 卸载自动逆序清理。
- effect 返回的 disposer 单次有效；同步 disposer 立即执行，async disposer 在卸载时
  await、直接调用时调度执行。
- fiber 已 dispose 后注册 → `InactiveEffectError`（对应 Cordis `INACTIVE_EFFECT`）。

## 配置校验

- `Config` 为 `S` schema → 加载前校验 + 默认值合并；失败 → `failed` 态
  （`SchemaValidationError` 带路径）。
- `Config` 为普通 dict → 宽松模式（浅合并默认值）。
- `Config` 为 None → 不校验。

## 框架内部事件

插件可在 apply 内监听：`internal/status(fiber, old_state)`、
`internal/error(fiber, error)`、`internal/service(name, value)` 等（见
`../features/events.md`）。
