# XCore Documentation

XCore 是一个 Python 的、类似 Cordis 的、以插件为核心的运行时核心包（stdlib-only，
零第三方依赖），为以插件为核心的 XBot 提供：**可恢复的状态（State）、插件化
（Plugins）、事件系统（Events）、生命周期（Lifecycle）、服务系统（Services）**。

本文档按项目惯例（对照 `XBotv2/docs`）组织：先讲来源与设计过程，再讲特性与契约，
最后是开发日志。文档与实现同步维护；契约以 `xcore/` 中的类型与测试为准。

## 设计过程（Step 1–2 的轨迹）

1. [XBot 现状与设计分析](01-xbot-current-state.md) — 对 `XBotv2` 现状的调研结论
   （Step 1 交付物）。
2. [Cordis 特性分析](02-cordis-feature-analysis.md) — 对 Cordis v3 核心特性的逐项
   调研与 Python 移植映射（Step 2 特性分析）。
3. [XCore 设计文档](03-xcore-design.md) — XCore 的整体设计：模块划分、核心对象、
   语义决策（Step 2 设计）。
4. [设计审查记录](04-design-review.md) — 对设计文档的外部审查意见与处置
   （Step 2 审查设计）。

## 特性文档

- [事件系统](features/events.md) — 事件名/通配符、emit/parallel/bail/serial/chain、
  过滤器与选择器。
- [服务系统](features/services.md) — `ctx.set/get/unset`、Service 基类、属性访问、
  服务注入（inject）。
- [插件系统](features/plugins.md) — 函数/对象/类插件、`ctx.plugin`、Registry 状态机、
  依赖（required/inject）、错误语义、dispose。
- [生命周期](features/lifecycle.md) — start/stop/dispose/restart、插件加载顺序、
  可恢复性保证。
- [状态服务](features/state.md) — `ctx.state` 可恢复 KV、原子持久化、命名空间、
  崩溃恢复。
- [Schema 配置](features/schema.md) — `S` DSL：声明、校验、默认值合并。
- [中间件](features/middleware.md) — `ctx.middleware` / `ctx.filter` 拦截链。

## 开发日志

- [开发日志](development-log.md) — 每日/每轮进展、决策与原因、验证记录。

## 验证

- [测试说明](verification/testing.md) — 测试组织与运行方式。
