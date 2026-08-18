# 服务系统（Services）

> 实现对照：`xcore/service.py`、`xcore/context.py`。语义规格见
> `../03-xcore-design.md` §5。

## 注册与解析

```python
ctx.set("database", db_instance)      # 提供服务；返回 disposer
ctx.get("database")                   # 读取；未提供/未激活返回 None
ctx.require("database")               # 未提供抛 ServiceNotFoundError
ctx.has("database")                   # 作用域内是否存在
ctx.unset("database", value=None)     # 释放；传 value 校验身份
ctx.database                          # 属性访问（__getattr__）
```

- **单次提供**（Cordis v3 语义）：同一作用域内同名服务只能提供一次；重复提供抛
  `ServiceConflictError`。释放用 `ctx.set(name, None)` 或 `ctx.unset(name)`，之后可
  重新提供。
- 服务**归属当前 fiber**：插件卸载时自动释放其提供的全部服务（无需手动清理）。
- `ctx.foo` 属性访问：未提供抛 `AttributeError`（`hasattr(ctx, "foo")` 即“服务是否
  存在”）；直接给 ctx 赋公开属性（`ctx.foo = x`）抛 `AttributeError` —— 必须用
  `ctx.set`。
- `strict=True`（默认）：提供方 fiber 不在 running 状态时 `get` 返回 None。

## Service 基类

```python
from xcore import Service

class Database(Service):
    name = "database"          # 缺省 = 类名 snake_case（MyDatabase -> my_database）

    def __init__(self, ctx, *, path: str):
        self.path = path
        super().__init__(ctx)  # 构造即注册，归属当前 fiber
```

Service 子类构造即注册；同一名字不能实例化两次（冲突抛错）。

## 作用域隔离（isolate）

```python
scoped = ctx.isolate("database")        # 独立作用域（每次调用新标签）
same = ctx.isolate("database", label)   # 显式同标签合并作用域
```

- 默认标签是每次调用新建的 `object()`：两次 `isolate("database")` **不**合并。
- 隔离子树内提供的服务对父不可见，反之亦然 —— 同一名字可有不同实现服务不同子树。

## 保留名

以下 Context 属性/方法名优先于服务解析（`__getattr__` 只在正常查找失败时生效），
**不要**用它们作服务名：`name`、`config`（插件配置）、`root`、`parent`、`fiber`、
`registry`、`state`（状态服务）、`is_active`、`on/once/off/emit/...`（事件方法）、
`set/get/has/require/unset`、`plugin/inject`、`middleware/filter/select/extend/
isolate`、`effect/dispose/start/stop/destroy`、`_` 前缀与 `then`。
（与 Cordis 一致：`ctx.config` 在插件内是插件配置。）

## 注入等待（inject）

```python
def consumer(ctx, config):
    ctx.database.query(...)             # 安全：只有 database 就绪才运行

consumer.inject = ["database"]                       # 全 required
consumer.inject = {"required": [...], "optional": [...]}   # koishi 形式
```

- **required**：服务可解析（strict 且提供方 running）前插件不加载（pending）。
- **optional**：不门控加载，运行时自行 `ctx.has`/`get` 判断。
- 唤醒时机（审查 A1 修复）：
  1. `ctx.set`/`unset`（服务出现/消失）；
  2. 提供方 fiber 状态迁移（loading→running / 离开 running）。
  因此「在 apply 内 `ctx.set` 注册服务 → 依赖它的插件随后加载」是确定性行为。
- 依赖消失 → 运行中插件回滚到 pending；依赖恢复 → 重新加载。
- 循环依赖：相关插件保持 pending（不报错不挂死，应用照常启动）。
- `ctx.inject(deps, callback)` = `ctx.plugin({inject, apply: callback})` 简写。
