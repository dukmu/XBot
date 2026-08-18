# XCore 公共 API 清单

> 规则（对齐 XBotv2 `api_inventory.md` 惯例）：`xcore.__all__` 必须与本文档一致，
> 由 `tests/test_public_api.py` 校验。插件与迁移层只允许 import 此清单内的符号。

## `xcore` 顶层导出（`xcore/__init__.py`）

| 符号 | 来源模块 | 说明 |
| --- | --- | --- |
| `Context` | `xcore.context` | 核心对象：事件/服务/插件/生命周期/中间件/状态 |
| `Service` | `xcore.service` | 服务基类（构造即注册、归属 fiber） |
| `Registry` | `xcore.plugin` | 插件注册表（`ctx.registry`） |
| `PluginHandle` | `xcore.plugin` | `ctx.plugin()` 返回值（fiber 的 Python 视图） |
| `PluginDef` | `xcore.plugin` | 归一化插件定义（name/callback/Config/inject/provide） |
| `FiberState` | `xcore.plugin` | pending/loading/running/failed/unloading/disposed |
| `current_fiber` | `xcore.plugin` | 当前正在执行 apply 的 fiber（服务用于绑定 fiber 级清理；非 apply 期间为 None） |
| `bound_effect` | `xcore.plugin` | 把 disposer 绑定到当前 apply fiber 的卸载（服务注册清理的一行封装；非 apply 期间 no-op） |
| `current_plugin_name` | `xcore.plugin` | 当前正在 apply 的插件名（非 apply 期间为 `"unknown"`） |
| `StateService` | `xcore.state` | 可恢复持久 KV（JSON 原子写 + 命名空间） |
| `S` | `xcore.schema` | Schema DSL 命名空间 |
| `SchemaValidationError` | `xcore.errors` | 配置校验失败（唯一校验错误类型，带 path） |
| `EventBus` | `xcore.events` | 事件总线（六种派发 + 过滤） |
| `Disposer` | `xcore.events` | `() -> bool` 类型别名（可逆注册的返回） |
| `XCoreError` | `xcore.errors` | 所有 XCore 异常的基类 |
| `InactiveEffectError` | `xcore.errors` | 在已销毁 fiber 上创建效果 |
| `ServiceNotFoundError` | `xcore.errors` | `ctx.require` 未找到服务 |
| `ServiceConflictError` | `xcore.errors` | 同一作用域重复提供服务 |
| `__version__` | `xcore` | `"0.1.0"` |

> 注意：插件加载失败**不**包装为专用异常 —— `PluginHandle` 的 `await` 重抛原始
> 错误（Cordis 对齐）；`Registry.plugin` 对非法插件形态抛 `TypeError`。

## 类型形状（稳定契约）

- `Disposer = Callable[[], bool]`。
- `Context.on/once` 返回 `Disposer`；`off` 返回 `bool`。
- 派发原语：`emit/parallel -> None`（await 完成）；`serial/bail -> Any`；
  `waterfall(event, *args, next) -> Any`。
- `PluginHandle`：可 `await`（加载完成/失败重抛）；`dispose() -> Awaitable[None]`；
  `state: FiberState`；`restart() -> Awaitable[None]`。
- `StateService`：`get/set/delete/clear/keys/all` 全 async；`namespace(prefix) -> StateService`。
- `Service`：子类定义 `name`；`__init__(ctx, *, name=None)`。
- `S`：`S.any()/string()/number()/boolean()/array(item)/object({...})/union([...])/
  enum([...])/const(v)`，修饰 `.default(v)/.optional()/.description(text)`；
  `schema.validate(config) -> validated`。

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

## 版本

`__version__ = "0.1.0"`。
