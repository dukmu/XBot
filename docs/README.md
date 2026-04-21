# 单用户本地数字人 Agent 文档

本文档集合提供了单用户本地数字人 Agent 系统的完整说明，包括架构设计、使用指南、开发规范和测试策略。

## 文档目录

### 核心文档

1. **[架构设计](./architecture.md)**
   - 系统整体架构
   - 组件交互流程
   - 数据流设计
   - LangGraph 状态图详解

2. **[快速开始](./getting-started.md)**
   - 环境要求
   - 安装步骤
   - 配置说明
   - 首次运行指南

3. **[配置参考](./configuration.md)**
   - 配置文件详解
   - 用户元信息配置
   - LLM Provider 配置
   - Agent 行为配置
   - 权限规则配置
   - 人格模板配置

4. **[工具系统](./tools.md)**
   - 内置工具列表
   - 工具调用机制
   - 权限控制细节
   - 自定义工具开发

5. **[Skill 系统](./skills.md)**
   - Skill 协议规范
   - Skill 加载机制
   - 编写自定义 Skill
   - Skill 最佳实践

6. **[子代理系统](./subagents.md)**
   - Attach 模式详解
   - Detach 模式详解
   - 子代理通信
   - 资源隔离机制

7. **[持久化系统](./persistence.md)**
   - SQLite Checkpointer 实现
   - SQLite Store 实现
   - 数据备份与恢复
   - 性能优化建议

8. **[上下文压缩](./compression.md)**
   - 压缩触发条件
   - 摘要生成策略
   - 记忆归档机制
   - 压缩配置选项

9. **[权限系统](./permissions.md)**
   - 权限规则语法
   - 正则表达式约束
   - 权限询问流程
   - 安全最佳实践

10. **[测试指南](./testing.md)**
    - MOCK LLM 使用
    - 单元测试编写
    - 集成测试场景
    - 测试覆盖率要求

### 附录

- **[API 参考](./api-reference.md)** - 完整的 API 文档
- **[故障排查](./troubleshooting.md)** - 常见问题与解决方案
- **[更新日志](./changelog.md)** - 版本历史与变更说明

## 快速导航

| 角色 | 推荐文档 |
|------|----------|
| 新用户 | [快速开始](./getting-started.md) → [配置参考](./configuration.md) |
| 开发者 | [架构设计](./architecture.md) → [工具系统](./tools.md) → [测试指南](./testing.md) |
| 运维人员 | [持久化系统](./persistence.md) → [故障排查](./troubleshooting.md) |
| 高级用户 | [子代理系统](./subagents.md) → [Skill 系统](./skills.md) → [上下文压缩](./compression.md) |

## 文档维护

- 文档与代码保持同步更新
- 重大功能变更需同时更新相关文档
- 欢迎通过 Issue 提交文档改进建议
