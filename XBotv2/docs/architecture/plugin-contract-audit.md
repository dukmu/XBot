# Plugin Contract Audit

> 审计日期：2026-09-09
>
> 事实来源：`scripts/audit_plugin_contracts.py`（Python AST）和
> `scripts/check_architecture.py --scope plugins`。

## 规则

跨插件依赖只有三种合法形式：

1. 导入目标插件的 `contracts.py`：数据模型、Port/Protocol、操作定义。
2. 导入目标插件的 `events.py`：事件名、事件载荷和事件协议。
3. 导入目标插件的 `protocol.py`：HTTP/线协议模型、路由契约和 wire event。
4. 导入共享基础层 `XBotv2.core` 或宿主组合层明确允许的 API。

禁止：

- 跨插件导入 `service.py`、`plugin.py`、`runtime.py`、`manager.py`、`models.py`、`commands.py` 等实现模块。
- 通过目标插件包根 `__init__.py` 重导出实现类、实现函数来绕过规则。
- 在 application 或其他插件中重新声明目标插件已经拥有的数据模型或 Port。
- 将某个插件的运行时实现类当作另一个插件的依赖类型。

插件内部可以让 `plugin.py`、`service.py`、`runtime.py`、`commands.py` 等实现模块互相协作；但跨插件只能依赖公开契约。宿主组合入口（例如 ACP transport 启动 application）单独标记为 host composition exception，不视为插件契约依赖。

## 可重复审计

生成完整的插件、包根导出、源文件、目标模块和具体符号清单：

```bash
PYTHONPATH=XBotv2 .venv/bin/python \
  scripts/audit_plugin_contracts.py --format markdown
```

生成适合进一步分析的 TSV：

```bash
PYTHONPATH=XBotv2 .venv/bin/python \
  scripts/audit_plugin_contracts.py --format tsv
```

执行架构边界检查：

```bash
PYTHONPATH=XBotv2 .venv/bin/python \
  scripts/check_architecture.py --scope plugins
```

脚本只使用 `ast.parse`，不会导入或执行业务模块。它检查：

- 每个 `*/plugin.py` 对应的插件目录。
- 包根 `__all__` 的实际导出符号。
- 所有 `Import` 和 `ImportFrom` 的源文件、目标模块和具体符号。
- 目标模块分类：`contracts`、`protocol`、共享 `core`、包根 API、宿主组合、实现依赖。

## 插件清单

下表是当前 28 个插件的包根导出和契约文件。完整符号级跨插件导入清单由上面的 AST 命令生成，避免在文档中维护第二份容易过期的 import 图。

| 插件 | 包根公开导出 | contracts | protocol |
|---|---|---|---|
| `acp_plugin` | `ACPLaunch` | 有 | 无 |
| `agentloop` | loop ports、events、tool contracts | 有 | 有 |
| `agents` | agent catalog/runtime contracts、events、commands | 有 | 有 |
| `browser` | 无 | 无 | 无 |
| `commands` | command models、ports、operations、helpers | 有 | 有 |
| `compact` | compaction events/models | 无 | 有 |
| `config` | policy models、settings port、wire models | 有 | 有 |
| `content_cache` | 无 | 无 | 无 |
| `context_builder` | context models/events、fragment stage | 有 | 无 |
| `coretools` | 无 | 无 | 无 |
| `goal` | 无 | 无 | 无 |
| `interactions` | interaction models、waiter、wire models | 有 | 有 |
| `jobs` | job/task/output contracts、commands、wire models | 有 | 有 |
| `llm` | model/provider ports、catalog、wire models | 有 | 有 |
| `mcp_plugin` | `MCP_PLUGIN_ID` | 无 | 无 |
| `permissions` | permission/approval contracts、events、wire models | 有 | 有 |
| `persistence` | persistence ports、records | 有 | 无 |
| `prompts` | `PromptsPort` | 有 | 无 |
| `sandbox` | sandbox command builder | 有 | 无 |
| `server` | route/server contracts、helpers | 有 | 有 |
| `session` | session ports/models/events、wire models | 有 | 有 |
| `skills` | 无 | 无 | 无 |
| `subagents` | `SubagentAgentError` | 有 | 无 |
| `todolist` | 无 | 有 | 有 |
| `token_manager` | 无 | 无 | 无 |
| `usage` | `UsageData` | 无 | 无 |
| `workspace_instructions` | 无 | 无 | 无 |
| `workspaces` | workspace models/events/ports | 有 | 有 |

说明：包根导出清单不是“所有同包实现都可以跨插件使用”的许可。包根公开符号仍必须属于该插件的公开契约；审计器会检查包根重导出实现的情况。

## 当前 AST 统计

当前 AST 发现 382 条跨包符号依赖：

| 分类 | 数量 | 结论 |
|---|---:|---|
| `allowed plugin contract` | 40 | 合法，但必须确认契约归属正确 |
| `allowed shared contract` | 250 | 共享 `core`/基础协议，合法 |
| `package-root API` | 91 | 需由包根 `__all__` 审计保证只导出契约 |
| `host composition exception` | 1 | ACP 启动器导入 application 启动组合；不是插件间依赖 |

数量来自 AST 运行结果，不是手工统计；重新运行脚本后应以新结果为准。AST 报告同时输出每个公开 Protocol 的 required methods 和仓库级 structural candidates，用于发现“声明存在但没有实现”的空壳契约。

## 归属审查结论

### 已修复的契约归属问题

- `InboxInput`、`InboxTarget`、`InboxSink` 已归属 `agentloop/contracts.py`；持久化接口由 persistence 自己的 `InboxPersistencePort` 拥有。
- `InteractionResult`、`InteractionWaiterPort`、`InteractionNotPending` 已归属 `interactions/contracts.py`；application 不再复制 `InteractionResultPort`。
- filesystem 使用 sandbox 的 `SandboxPort`，不依赖 `SandboxPolicy` 实现。
- subagents 使用 `PromptsPort`，不依赖 `PromptsService` 实现。
- prompts 使用 context builder 的 `PromptFragmentRegistry`，不依赖 `ContextBuilder` 实现。

### 本轮完成的泄露迁移

- 所有包根 `_...EXPORTS`/`__getattr__` 符号路由已删除；`__all__` 与普通显式导入构成唯一公开表面。
- config 的运行配置模型、workspace 的资源/目录模型与 Port、Todo 状态模型均归入各自 `contracts.py`。
- session 的公开 event frame、cursor error 与历史投影归入 session contract；event stream 保持内部运行时实现。
- persistence 只公开真正跨边界的 `ThreadLifecycleRecord`；message/inbox trajectory codec 和具体 store 不再由包根导出。
- `InteractionWaiter` 保持 interactions 内部实现；permissions 通过 `InteractionsPort` 创建 waiter，不再跨插件导入实现。
- 通用 child application handle/result/error 归 application；subagent 专用错误归 subagents，不再由 agents 冒充所有者。
- MCP 的公开插件标识归 `mcp_plugin/contracts.py`。

审计器现在还检查 `contracts.py`、`events.py`、`protocol.py` 是否反向导入同包实现模块。这个补充规则曾发现 session event stream、Todo models 和 workspace directory 的四处漏报；迁移后严格审计为 `contract audit: ok`。

## 判定标准

一次审计只有在以下条件同时满足时才算通过：

- `scripts/check_architecture.py --scope plugins` 返回 `architecture boundaries: ok`。
- `scripts/audit_plugin_contracts.py --format tsv` 返回 `contract audit: ok`。
- AST 审计没有 `IMPLEMENTATION DEPENDENCY`。
- 所有跨插件数据模型、Port、Protocol、事件常量、操作定义都能在所属插件的 `contracts.py`、`events.py` 或 `protocol.py` 找到唯一来源。
- application、transport、测试夹具没有复制同名或等价的第二套契约。
- 包根 `__all__` 不隐藏地重导出实现模块中的类或函数。
- 修改后运行完整测试套件，而不是只验证导入检查器。

当前状态：`scripts/check_architecture.py --scope all` 与严格 contract audit 均通过。本轮回归为 core `720 passed`、integration `139 passed`、ACP `4 passed`；ACP socket 用例在受限沙箱内被 OS 拒绝，在允许本地 socket 的环境复跑后通过。

## 两个脚本的边界

`audit_plugin_contracts.py` 是可读清单和严格 contract 泄露检查：它报告符号级跨包依赖、包根实现重导出、声明模块对实现的反向依赖，以及 Protocol 的结构候选。

`check_architecture.py` 仍有独立价值，它检查 Agent loop/tool 所有权、application 启动组合、XCore service locator、插件依赖声明、profile 环和 ACP transport 边界。它不应复制一份 contract 分类表；后续若将 contract audit 纳入单一 CI 命令，应复用审计器结果，而不是维护第三套规则。原先禁止根 `agentloop/plugin.py` 的历史规则已删除：当前约束是每个 capability 只在根 `plugin.py` 导出并从那里注入 carrier facet。

## 待办

- 对项目文档做一次以当前源码为准的完整事实核对，修正插件清单、契约归属、启动组合、服务与事件说明，并删除已经失效的路径和历史设计描述。
- 对 `xbot-plugin-development` skill 及全部参考资料做完整核对，补齐服务、事件、生命周期、沙箱/离线环境和验证流程，并确保速查清单与各插件详情之间边界清楚、内容一致。
