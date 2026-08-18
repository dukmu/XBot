# 中间件（Middleware）

> 实现对照：`xcore/context.py`。语义规格见 `../03-xcore-design.md` §10。

## 用法

```python
async def logger_mw(session, next):
    print("before", session)
    result = await next()          # 委托给下一环
    print("after", session)
    return result

async def guard_mw(session, next):
    if not allowed(session):
        return "denied"            # 不调 next() 即短路整条链
    return await next()

disposer = ctx.middleware(logger_mw)          # 返回 disposer
ctx.middleware(guard_mw, prepend=True)        # 插到链首

result = await ctx.run_middleware(session)    # 执行整链；返回短路值或 None
```

- 中间件签名：`async (session, next) -> Any`（`next` 为 `async () -> Any`）。
- 链按全局注册序组合（prepend 插首）；`run_middleware` **每次调用重建链快照**
  （运行期可增删中间件）。
- 短路：不调 `next()` 即返回，链上后续中间件不执行；返回值向上传播，最终返回
  最外层中间件的返回值（无短路时链尾返回 None）。
- 过滤：`ctx.filter(predicate)` / `ctx.select(...)` 对之后注册的中间件生效
  （注册时快照，与事件监听器一致）。
- 错误传播：中间件抛错向上抛给 `run_middleware` 调用方。
- 归属 fiber：插件卸载自动移除其注册的中间件。

## 与 Cordis 的关系

Cordis core 没有独立 middleware 原语 —— 环绕链就是 `waterfall` 事件（内部事件
`internal/update` 等即用此模式）。XCore 的 `ctx.middleware` 是基于同一 waterfall
语义的便捷 API（koishi 的 `Processor` 是应用层实现；XCore 收进核心并保留
`session` 过滤）。
