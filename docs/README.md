# XBot Hermes 文档

本文档集合描述 Hermes 的当前实现、目标架构和开发约束。Hermes 是一个轻量级、高质量、单用户、本地优先的 agent。

## 当前文档

| 文档 | 内容 |
|------|------|
| [架构与设计](./architecture.md) | Hermes 的目标架构、上下文树、ask/权限、cache、subagent、mailbox、实现路线 |
| [快速开始](./getting-started.md) | 安装、配置、启动方式 |
| [配置参考](./configuration.md) | 当前代码支持的配置字段，以及规划字段说明 |
| [测试指南](./testing.md) | Mock LLM、单元测试和集成测试策略 |

## 推荐阅读顺序

新用户：

1. [快速开始](./getting-started.md)
2. [配置参考](./configuration.md)
3. [架构与设计](./architecture.md)

开发者：

1. [架构与设计](./architecture.md)
2. [配置参考](./configuration.md)
3. [测试指南](./testing.md)

## 重要约定

文档中的能力分为三类：

| 状态 | 含义 |
|------|------|
| 已实现 | 当前运行路径已经使用 |
| 部分实现 | 有代码基础，但行为尚未完整闭环 |
| 规划中 | 架构目标，尚未实现 |

当前开发阶段默认使用 `InMemorySaver` 和 `InMemoryStore`。SQLite checkpointer/store 代码已经存在，但还不是默认入口路径。

## 待补文档

这些主题已经在架构文档中定义，但尚未拆成独立文档：

- 工具系统与工具结果 cache
- 权限系统
- 上下文树和 rewind
- subagent 生命周期
- mailbox
- SQLite 持久化

拆分前，以 [架构与设计](./architecture.md) 作为这些主题的权威说明。
