# 生命周期（Lifecycle）

> 实现对照：`xcore/context.py`、`xcore/plugin.py`。语义规格见
> `../03-xcore-design.md` §3.2/§7。

## 模型：start 门控 + inject 驱动

- `ctx.plugin()` 只注册（fiber 进入 `pending`）。加载由两条路径触发：
  1. `await ctx.start()`：置 active → **迭代不动点**加载所有依赖满足的
     pending fiber（直到无进展；等待中的保持 pending，不报错）→
     `emit("ready")`。
  2. 运行期服务变化（`set`/`unset`/提供方状态迁移）：满足的 pending fiber 立即
     加载；运行中且依赖丢失的 fiber 回滚到 pending。**仅在 active 时生效**（start
     前 set 服务不触发加载）。
- active 期间 `ctx.plugin()` 注册：依赖满足立即后台加载。
- `await handle` 语义：等在途迁移；pending（缺依赖）立即返回；failed 重抛错误。

## start / stop / restart / destroy

```python
await ctx.start()            # 激活 + 不动点加载 + ready
await ctx.stop()             # 卸载全部 fiber（逆加载序）到 pending + dispose 事件
await ctx.start()            # 再次 start：插件重新 apply（可恢复）
await ctx.destroy()          # 永久拆除（不可再 start）
```

| 保证 | 说明 |
| --- | --- |
| 重复 start/stop | no-op（记 warning）；root 级生命周期锁串行化并发调用 |
| pre-stop 失败 | 抛出异常、保留服务和 owner，允许重试排空 |
| effect disposer 失败 | 逐条记日志 + `internal/error`，继续执行其他 disposer |
| ready 插件监听器失败 | 归属实际注册 fiber，记录 activation error 并清理该 owner；loader 据必需/可选约定决定是否打开 |
| dispose / 裸 root ready 监听器失败 | 记日志，不使 root 生命周期卡死 |
| `dispose` 事件时机 | 在卸载 fiber **之前**发出（监听器仍注册着，可做最后清理） |
| 卸载顺序 | 按加载逆序（逆拓扑） |
| destroy 后 start | 抛 `RuntimeError`（不可恢复） |
| 可恢复性 | `ctx.state` 持久内容跨 stop→start 保留（见 `state.md`） |

## ready / dispose 事件

```python
def plugin(ctx, config):
    ctx.on("ready", on_ready)      # start 完成所有插件加载后触发
    ctx.on("dispose", on_dispose)  # stop 清理前触发（手动资源释放的逃生口）
```

- 完整依赖图加载并完成 ready 后，新注册的 `ready` 监听器立即调度；任务归注册 fiber，pre_stop 会取消并等待它。加载中不提前执行。
- 独立 scope.start 仅运行本 scope 的 ready。初始化期间嵌套事件错误按实际 Hook.owner 归属、清理并继续其它 owner；取消仍传播。调度边界在 finally 失效，后台任务继承它也不会改变后续普通事件的错误传播。
- 资源清理推荐用 `ctx.dispose(cb)` / apply 返回 disposer；`dispose` 事件是 Cordis
  无法追踪的副作用（如关闭服务器）的兜底。

## 依赖驱动的热插拔

- 插件 A 提供服务、插件 B `inject` 该服务：A 卸载 → B 自动回滚 pending；A 重新挂载
  → B 自动加载。无需手工编排顺序。
- `stop()` 期间在途的 async apply：先等 apply 完成再卸载（fiber 级串行化），其迟到
  的注册不会泄漏。

独立 scope 的活跃根挂载、pre-stop 与等待关闭契约见 [作用域](scopes.md)。
