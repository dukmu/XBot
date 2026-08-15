# Schema 配置（S DSL）

> 实现对照：`xcore/schema.py`。语义规格见 `../03-xcore-design.md` §9。

## 声明

```python
from xcore import S

Config = S.object({
    "name": S.string(),
    "retries": S.number().default(3),
    "verbose": S.boolean().optional(),
    "tags": S.array(S.string()),
    "mode": S.enum(["fast", "safe"]).default("fast"),
    "nested": S.object({
        "url": S.string(),
    }),
    "either": S.union([S.string(), S.number()]),
})
```

类型构造器：`S.any() / S.string() / S.number() / S.boolean() / S.array(item) /
S.object({...}) / S.union([...]) / S.enum([...]) / S.const(v)`。

修饰（不可变 builder，每次返回新 schema）：`.default(v)`（深拷贝填充）、
`.optional()`（允许缺省，解析为 None）、`.strict()`（仅 object：丢弃未知键）、
`.description(text)`（文档用）。

## 校验语义（对齐 schemastery 核心）

| 规则 | 行为 |
| --- | --- |
| 缺键 | 有 `.default` → 深拷贝默认值；`.optional()` → 省略；否则抛 `SchemaValidationError`（路径 `$.name`） |
| 未知键 | **默认保留**；`.strict()` 丢弃 |
| 类型检查 | `number` 拒绝 bool；`array`/`object`/`union` 递归，错误带 `$.a.b[0]` 路径 |
| union | 按声明序尝试，全部失败抛聚合错误 |
| 默认值 | 深拷贝，默认值永不被共享/变异 |

```python
schema.validate(config) -> validated   # 返回带默认值的副本，不修改入参
```

## 插件配置接线

```python
def my_plugin(ctx, config):
    ...

my_plugin.Config = S.object({...})     # 加载前自动校验 + 默认值合并
my_plugin.Config = {"a": 1}            # 宽松模式：仅浅合并默认值
my_plugin.Config = None                # 不校验
```

- 校验失败 = 插件加载失败 → fiber `failed` 态（`handle.await()` 重抛
  `SchemaValidationError`），不阻断其它插件。
- `validate_config(schema, raw, default=None)` 是插件加载器使用的底层入口。
