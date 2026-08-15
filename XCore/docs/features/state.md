# 状态服务（State）

> 实现对照：`xcore/state.py`。语义规格见 `../03-xcore-design.md` §8。
>
> **定位**：XCore 相对 Cordis 的一等公民扩展。Cordis/Koishi 没有持久 KV 状态服务
> （`ctx.state` 在 v3 是 `ctx.scope` 的废弃别名；持久化靠 Minato 数据库与配置文件）。
> XCore 的 `StateService` 直接服务「可恢复状态」需求（并承接 XBotv2 `PluginStore`
> 的迁移语义）。

## 使用

```python
state = ctx.state                     # root 单例服务（首次访问惰性创建并注册为 "state"）
await state.get("key", default=None)  # 读取（惰性读盘 + 内存缓存）
await state.set("key", value)         # 写入 + 立即原子落盘
await state.delete("key")
await state.clear()
await state.keys() / state.all()
ns = state.namespace("goal")          # 命名空间视图（键前缀 "goal."）
```

- 值必须 JSON 可序列化（dict/list/str/int/float/bool/None）；非法抛 `TypeError`。
- 全部方法 async；`set`/`delete` 返回即已落盘（await 保证持久）。

## 可恢复性保证

1. **原子写**：写临时文件 + `os.replace` —— 进程崩溃不产生半写文件；残留 `.tmp`
   文件不影响下次读取。
2. **重启恢复**：同一 `path` 新建实例（或 `stop()`→`start()`）读到上次持久值。
3. **损坏文件 fail-loud**：文件非合法 JSON 抛 `RuntimeError`（不静默恢复，XBot
   纪律）。
4. **UTF-8** 全链路（项目纪律）。

## 并发与命名空间

- 同一文件的全部视图（含所有 `namespace()`）**共享内存缓存与 `asyncio.Lock`**：
  `set`/`delete` 持锁做 read-modify-write —— 多插件并发写互不丢键。
- `namespace(prefix)`：键实际存储为 `"prefix.<key>"`；per-plugin 隔离（迁移层将
  XBotv2 `PluginStore` 映射为 `ctx.state.namespace(plugin_name)`）。
- `ctx.state` 注册为 root 服务 `"state"`：插件可以 `inject: ["state"]`，也可
  `ctx.state` 直接访问（二者同一实例）。

## 路径约定

`Context(data_dir=...)` → `ctx.state` 落在 `data_dir/state.json`。生产环境必须显式
传入 `data_dir`（缺省为当前目录）。
