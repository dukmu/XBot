# 04 · XCore 设计审查记录（Step 2 审查设计交付物）

> 设计稿 v1.0 提交外部审查（独立子代理，阅读 vendored `@deepseek-ai/cordis` 源码
> 逐行核对），结论 **redesign-required**；同时完成 Cordis 双版本调研
> （`cordis-architecture-report.md`，v3.18.1 = Koishi 实际运行版本，v4/vendored）。
> 本文档记录审查发现与处置；v1.1 已全部落实（见 `03-xcore-design.md`）。

## 审查发现 → 处置

| # | 严重度 | 发现（摘要） | 处置（v1.1） |
| --- | --- | --- | --- |
| A1 | blocker | 依赖重检只在 set/unset 触发；服务在 apply 内注册时依赖者永远等不到「提供方 running」的通知，典型模式死锁 | §5.4：唤醒触发点加入提供方 fiber 状态迁移（loading→running / 离开 running） |
| A2 | blocker | `internal/listener` 拦截是同步 bail，async-only 派发无法在同步 `on()` 内替代注册 | §4.3：`internal/*` 用同步派发（`_emit_sync`/`_bail_sync`），try/log 守护 |
| A3 | should-fix | 过滤器契约与 vendored 相反（thisArg[filter](hook.ctx) vs 注册时快照 session 谓词） | §3.3 + §14.3：保留 session 快照模型，明示为有意差异（bot 场景便捷语义） |
| A4 | should-fix | once 并发单触发缺机制 | §4.4：同步检查-标记（任何 await 之前） |
| A5 | should-fix | set 混并 provide/set；unset 无所有权约束 | §5.1 + §14.13：set 重复提供抛 `ServiceConflictError`（v3 语义）；unset 传 value 校验身份 |
| A6 | should-fix | failed 是死态，依赖恢复不可重试 | §6.3：failed 可经依赖恢复/restart 重试 |
| A7 | should-fix | intercept「本版实现」与「本版不做」矛盾 | 明确 v1 不做 intercept（§14.8）；inject 对象值仅记录不解释 |
| A8 | nit | 省略的内部事件未列明 | §4.3 列出省略项；`internal/dispatch` 补 `this_arg` |
| A9 | nit | bail==serial 理由错误 | §14.2：更正为「皆 await，检查解析后值」 |
| A10 | nit | generator effect 被静默丢弃 | §14.10：明确 v1 仅同步体 |
| B1 | blocker | start 加载算法欠定（注册序单遍会漏链）；await handle 挂死 pending | §3.2/§6.3：迭代不动点；await pending 立即返回；set 触发加载仅限 active |
| B2 | blocker | root dispose 语义自相矛盾（重启 vs 永久） | §3.2/§7：`destroy()` 永久拆除；`dispose(cb)` 只收回调；卸载→pending 保留 |
| B3 | should-fix | stop 抛 ExceptionGroup 破坏 restart | §3.2/§7：stop 永不抛出（逐条记日志）；ready/dispose 监听器失败记日志 |
| B4 | should-fix | 无在途迁移串行化，load/dispose 竞态 | §6.3：单在途迁移（`_transition`）；stop 先 await 在途 |
| B5 | should-fix | unset 不等依赖者安定 | §6.3：先释放服务+通知+await 依赖者，再执行 disposers |
| B6 | should-fix | EventBus 归属未定 | §3.1：root 单例 EventBus，Hook 记录 owner |
| B7 | should-fix | start/stop 作用域 vs 全局 Registry 未定 | §3.2：start/stop 为 root 级；destroy 级联子树 |
| B8 | should-fix | 卸载顺序未定；prepend 负序号 | §7：卸载 = 加载逆序；§4.1：prepend 用递减计数器（无害） |
| B9 | nit | start/stop 并发无锁 | §3.2：root 生命周期锁 |
| C1 | should-fix | `__getattr__` 无守卫、`__setattr__` 未定义 | §3：`_` 前缀抛 AttributeError；公开名禁止赋值（用 ctx.set） |
| C2 | should-fix | plugin.py 与 service store 分层缝未定 | §2：ServiceStore 在 service.py；Fiber duck-typing 访问，TYPE_CHECKING 标注 |
| C3 | nit | parallel 吞 CancelledError | §4.2：CancelledError 立即重抛 |
| C4 | nit | 类属性 inject 共享污染 | §6.1：归一化拷贝 |
| C5 | nit | waterfall 的 next 命名/传参 | §4.2：keyword-only `next=`，文档给示例 |
| C6 | nit | Service.setup 调用者未定 | §5.2：移除 setup；类插件用 __init__/apply |
| D1 | should-fix | dispose() 重载是 footgun | §3.4：无参 dispose() 抛 TypeError；破坏性用 destroy() |
| D2 | nit | isolate 缺省标签用名字会合并 | §5.3：每次调用新建 `object()` 标签 |
| D3 | nit | has 语义未定 | §5.1：has = 作用域内有 Impl（不看 strict） |
| D4 | nit | Service 在裸子 ctx 上注册的归属 | §5.2：注册到当前 fiber（文档注明） |
| D5 | nit | 错误类型/Registry.forEach 缺失；errors.py 不在模块树 | §2/§11：errors.py 入树；单校验错误类型；补 forEach |
| E1 | should-fix | StateService 跨 namespace 丢失更新 | §8：共享缓存 + asyncio.Lock + 原子写 |
| E2 | should-fix | ctx.state 惰性属性无法 inject | §8：注册为 root 服务 "state" |
| E3 | nit | middleware 组合细节 | §10：每次 run 重建快照、全局序、过滤求值 |
| E4 | nit | logger 服务被丢弃 | §12：stdlib logging；callable logger 为后续项 |
| E5 | nit | 缺并发/失败场景测试 | §13：补充 stop-during-apply、双 set、ready 抛错等 |

## 调研报告的修正（v1.1 已吸收）

1. **`ctx.state` 不是持久 KV**（v3 是 `ctx.scope` 的废弃别名）——`StateService` 是
   XCore 一等公民扩展（§14.7），设计依据 XBotv2 的 `PluginStore` 需求而非 Cordis。
2. **无 `ctx.select`**（session selector 是 koishi 的 `ctx.user(...)` 等 filter
   派生）——XCore `select` 标注为扩展（§14.3）。
3. **无事件通配符**（拦截靠 `internal/dispatch`）——XCore 通配为扩展（§14.11）。
4. **`ctx.off` 在 v3 core 未实现**——XCore 补齐（§14.12）。
5. **inject 的 required/optional 形式**（koishi `{required, optional}`）——§5.4 采纳。
6. **错误通道是 `internal/error` 事件**而非 app.on('error')——§4.3 采纳。
7. **parallel 错误**：v3 首错即抛、v4 聚合——XCore 选 v4 聚合（§14.5）。
8. **ready 在 active 期间注册立即执行**——§7 采纳。

## 结论

v1.0 → v1.1 修订完成：两处直接矛盾（B2、A7）已消解，两处 blocker（A1、A2）与
两处 blocker 级加载/await 语义（B1、B2）已落实到具体机制，其余 should-fix/nit 全部
处置或明示为有意差异。**v1.1 通过审查，进入实现阶段。**
