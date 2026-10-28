# 业务作用域与所有权

公开入口为 `xcore.api.context.Context`、`xcore.api.services.ServiceKey[T]` 和
`xcore.api.events.EventKey[T]`。Context 直接公开实际实现类。所有 scope 共用根上的
ServiceStore、EventBus、Registry；没有第二套服务容器。

```python
from xcore.api.context import Context
from xcore.api.services import ServiceKey
from xcore.api.events import EventKey

DATABASE: ServiceKey[Database] = ServiceKey("database")
FINISHED: EventKey[Completion] = EventKey("thread.finished")

root = Context()
await root.start()
session = root.scope("session")
thread = session.scope("main", provides=(DATABASE,))
thread.provide(DATABASE, database)
thread.plugin(mount_thread)
await session.start()

db = thread.require(DATABASE)
session.on(FINISHED, on_finished, descendants=True)
await thread.emit(FINISHED, completion)
await thread.close()
await root.close()
```

`scope()` 使用现有 registry 创建专用 owner fiber，`extend()` 和 `isolate()` 仍是
沿用 owner 的视图。`start()` 在活跃根下等待该 scope 子树的依赖激活，不启动其他
兄弟 scope；缺失依赖保持 pending，插件失败保留在 handle 上，loader 应检查必需能力。

`provides` 为此 scope 声明独立服务标签。该服务尚未发布时 `require()` 报错，不能
退回父层同名值。未隔离的父服务向下共享；`isolate(KEY, label)` 可显式共享标签。
`require`、`provide`、`set`、`inject`、`isolate` 使用同一 ServiceKey 声明。
ServiceKey 按规范服务名解析，字符串生产调用在迁移阶段仍同名互通。依赖刷新在
registry 内按实际 `(label, name)` 绑定索引，不因另一 thread 的同名服务变化重启。
`ctx.state` 同样遵守隔离；只有未隔离的 state 使用根默认 StateService。

EventKey 按声明对象身份区分事件；两个同名声明不会互通。payload 原样传递，无通用
包装。字符串事件保留通配符，EventKey 使用精确声明匹配。所有分发方式均携带发送
scope；默认只匹配当前业务 scope。`descendants=True` 明确观察当前 scope 及其后代，
不跨兄弟 session。`global_=True` 只跳过 payload filter，不越过 scope。
`internal/*` 注册仅允许运行根 scope，并仅分发给根监听器。`off()` 只能移除当前
Context 注册的 callback；返回的 disposer 可直接撤销相应注册。once 消费后、off 后和
关闭时会移除 owner effect 及空事件桶。

`ctx.pre_stop(callback)` 注册停止新入口与排空业务的阶段，callback 可异步。
`await scope.close()` / `destroy()` 先将整个子树标为 closing，拒绝新注册，再执行
pre-stop；此时已有服务仍可解析。全部排空后才执行现有服务撤销、依赖卸载和 effect
清理，并等待异步 disposer（包括之前手动调用 disposer 启动的清理）。同一 owner
的并发 close 由现有 settle 锁串行化。之后移除子 fiber、依赖索引、Context 树和
父级清理引用。

pre-stop 抛错会中止关闭并向调用方抛出异常，保留服务和 owner；失败 callback 可在
下次 close 重试，已成功的 callback 不重复运行。其他健康 scope 可独立关闭。
普通 effect disposer 仍遵循既有逐条执行并记录错误的约定；需要保证成功后才能释放
锁或关键业务资源的排空操作应放在 pre-stop。

原 `xcore` 根导出与字符串服务调用暂供产品调用方迁移；整仓 API 导入迁移另行完成。
