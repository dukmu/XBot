# XCore 开发日志

> 与 XBotv2 的工程纪律一致：记录决策、原因与验证证据；文档与实现同步。
> 格式：每条记录含日期（尽力而为）、阶段、做了什么、为什么、验证结果。

## 2026-08（Round 1：Step 1 + Step 2）

### 2026-08-16 · 创建 dev-xcore 分支与目录骨架

- **做了什么**：从 `main` 创建 `dev-xcore` 分支；建立 `XCore/` 骨架
  （`xcore/`、`tests/`、`docs/`）；完成 Step 1 交付物 `01-xbot-current-state.md`。
- **为什么**：Git 工作流要求新特性在 `dev-*` 分支开发；XCore 作为独立包与 `XBotv2`
  并列，后续迁移（Step 3）以包依赖方式接入。
- **验证**：`git checkout -b dev-xcore` 成功，工作树干净。

### 2026-08-16 · Cordis 特性调研

- **做了什么**：委托子代理调研 Cordis v3 核心特性（事件/服务/插件/生命周期/状态/
  schema/中间件），产出 `02-cordis-feature-analysis.md`。
- **为什么**：保证“支持大部分 Cordis 核心特性”有据可依，而不是凭印象实现。
- **验证**：（待报告落地后补记关键结论与出处）。

### 2026-08-16 · XCore 设计

- **做了什么**：编写 `03-xcore-design.md`（模块划分、核心对象、语义决策表）。
- **为什么**：先设计后实现，设计经审查（`04-design-review.md`）再动手。
- **验证**：（实现后由测试反证）。

### 2026-08-16 · 设计审查与修订（v1.0 → v1.1）

- **做了什么**：独立子代理对照 vendored `@deepseek-ai/cordis` 源码逐行审查设计稿
  v1.0，结论 redesign-required（2 处 blocker 矛盾、2 处 blocker 语义漏洞 +
  约 30 项 should-fix/nit）；同时完成 Cordis 双版本调研
  （`references/cordis-architecture-report.md`，v3.18.1 = Koishi 实际运行版本）。
  据此修订为 v1.1 并逐项处置（`04-design-review.md`）。
- **关键修正**：① 依赖唤醒加「提供方状态迁移」触发点（A1，消除 apply 内注册服务
  的死锁）；② `internal/*` 同步派发（A2，`on()` 拦截可行）；③ start 迭代不动点 +
  await pending 立即返回（B1）；④ root dispose 语义矛盾消解为 `destroy()`（B2）；
  ⑤ stop 永不抛出（B3）；⑥ 单在途迁移（B4）；⑦ StateService 共享缓存 + 锁
  （E1）；⑧ 过滤器快照 + session 谓词明示为有意差异（A3/§14.3）。
- **验证**：修订稿通过审查，进入实现。

### 2026-08-16 · XCore 实现与测试

- **做了什么**：按 v1.1 实现 8 个模块（`errors/schema/state/events/service/plugin/
  context/__init__`），共约 1300 行；编写 8 个测试文件、103 项测试全绿。
- **实现要点**：
  - 六派发原语（emit/parallel/serial/bail/chain/waterfall）+ 通配扩展 +
    `internal/*` 同步派发；
  - ServiceStore（(label, name) 键控）+ isolate 作用域 + inject 依赖门控
    （required/optional）；
  - Fiber 状态机（单在途迁移、失败隔离与回滚、failed 可重试不循环、B5 先释放
    服务再清理）；
  - root 级生命周期锁、start 不动点、dispose 先于卸载、destroy 永久拆除；
  - StateService 原子写 + 共享缓存/锁 + 命名空间（崩溃恢复有测试）。
- **过程中修复的缺陷**（测试驱动）：
  1. `await handle` 与后台加载的竞态 → `await_fiber` 确定性驱动 converge；
  2. `settle_to(CONVERGE)` 对 failed fiber 无限重试 → 落在 FAILED 即停；
  3. `destroy()` 持锁调 `stop()` 死锁 → 抽取 `_stop_locked()`；
  4. dispose 事件在卸载后发出导致监听器已注销 → 改为卸载前发出；
  5. waterfall 续体参数遮蔽内置 `next`（审查 C5 预警兑现）→ 迭代器协议直取。
- **验证**：`XCore` 内 `pytest` 103 passed（1.06s）；公共 API 清单由
  `test_public_api.py` 对照 `features/api.md` 校验。未运行 XBotv2 全量套件
  （XCore 是新增独立包，不进入 XBotv2 的 testpaths，根 pytest 配置不受影响）。

### 2026-08-16 · 集成场景与收尾修复

- **做了什么**：编写端到端集成场景（服务提供 + inject 依赖 + 事件 + 中间件 +
  状态 + 重启恢复），驱动出三处实现修正：
  1. `ctx.config` 在插件 fiber Context 上应返回**插件已验证配置**（Cordis 语义），
     而非父 Context 的 config —— `Context.config` 属性按 fiber 归属分派；
  2. `S.object` 在整体配置为 None（插件未传配置）时应按 `{}` 校验（koishi 约定，
     属性级默认值生效）；
  3. 明确 ready 监听器在 active 期间是「调度为任务、下一个事件循环轮次执行」——
     asyncio 语义与 Cordis 一致，观测前需 `await asyncio.sleep(0)`（已写入文档）。
- **文档**：`features/` 补齐事件/服务/插件/生命周期/状态/Schema/中间件/API 清单
  共 8 篇；`README.md` 索引；服务保留名说明（`config`/`state`/事件方法等）。
- **验证**：集成场景通过；全量 103 测试再跑全绿。
