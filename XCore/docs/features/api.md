# XCore 公共 API

产品代码和插件通过 `xcore.api` 下的模块导入公开能力；包根不转出口。实际
`Context`、服务、插件和事件总线类型由框架实现，API 入口引用相同的类型，
不会创建代理或重复的状态容器。

- `xcore.api.context`：`Context`。
- `xcore.api.services`：`ServiceKey`；`xcore.api.service`：`Service`。
- `xcore.api.state`：`StateService`。
- `xcore.api.events`：`EventKey`、`EventEmitter`、`EventDispatch`、
  `EventQuery`、`Disposer`；`xcore.api.event_bus`：`EventBus`、`is_bailed`。
- `xcore.api.plugins`：`PluginDef`、`PluginHandle`、`FiberState`、`Registry`、
  `resolve_plugin`、`current_fiber`、`current_plugin_name`、`bound_effect`。
- `xcore.api.errors`：`XCoreError`、`InactiveEffectError`、
  `ServiceNotFoundError`、`ServiceConflictError`。

`EventKey[T]` 的监听和 `emit`、`serial`、`bail` 派发使用对应的 `T` payload；
字符串事件保留动态参数。插件加载失败由 handle 重抛原始错误。

## 类型形状（稳定契约）

- `Disposer = Callable[[], bool]`。
- `Context.on/once` 返回 `Disposer`；`off` 返回 `bool`。
- 派发原语：`emit/parallel -> None`（await 完成）；`serial/bail -> Any`；
  `waterfall(event, *args, next) -> Any`。
- `PluginHandle`：可 `await`（加载完成/失败重抛）；`dispose() -> Awaitable[None]`；
  `state: FiberState`；`error: BaseException | None`；
  `missing_dependencies: tuple[str, ...]`；`restart() -> Awaitable[None]`。
- `StateService`：`get/set/delete/clear/keys/all` 全 async；`namespace(prefix) -> StateService`。`StateService(path=...)` 使用原子 JSON 文件；`StateService.memory()` 显式创建仅内存状态，两者 namespace/校验/共享锁语义相同。
- `Service`：子类定义 `name`；`__init__(ctx, *, name=None)`。
- 插件配置：插件的 `Config` 必须是提供 `model_validate` 和
  `model_json_schema` 的 Pydantic 模型；XCore 只调用 `model_validate`，不拥有配置
  DSL，也不依赖 Pydantic。

## 内部事件（`internal/` 前缀，框架扩展点）

| 事件 | 参数 | 派发模式 | 用途 |
| --- | --- | --- | --- |
| `internal/status` | `(fiber, old_state)` | emit | fiber 状态迁移通知 |
| `internal/service` | `(name, value)` | emit | 服务注册/注销/变化 |
| `internal/dispatch` | `(mode, name, args)` | emit | 派发诊断（非 internal 事件） |
| `internal/listener` | `(name, listener, options)` | bail | 监听器注册拦截（返回非 None 即替代注册） |

## 公共事件

| 事件 | 触发时机 |
| --- | --- |
| `ready` | `start()` 完成插件加载后 |
| `dispose` | `stop()` 清理完毕后 |
