# Task Mode 与 DAG 运行示例

Task mode 用来承载复杂任务的可执行状态。它不是 markdown todo list，也不是把模型输出的计划文本保存下来。Hermes 的任务执行语义是：

> LLM 负责规划、执行、验证和总结；DAG state 负责让这些动作可恢复、可观察、可纠错。

事实源是 append-only 事件和 `plan.yaml`。`state.yaml`、`context.md` 是投影视图，给 runtime、UI 和模型读取。

## 1. 任务生命周期

一次复杂任务的最小闭环如下：

```text
用户提出目标
  -> agent 判断需要 task mode
  -> task_begin(goal)
  -> plan_autofill(scope) 或 plan_add_nodes(nodes_json)
  -> plan_next() 选择一个 ready 节点并标记 running
  -> 使用 filesystem/shell/其他工具执行当前节点
  -> plan_update(node_id, status, summary, result, evidence_refs_json)
  -> 重复 plan_next / 执行 / plan_update
  -> summary_add(...) 只在跨节点或跨轮次总结有价值时使用
  -> task_status() 检查是否存在 completion_errors
  -> task_exit(status="completed" | "failed" | "blocked")
```

关键规则：

- 同一时间只运行一个 DAG 节点，除非调度器明确升级为并发模式。
- `completed` 和 `verified` 都能满足依赖。
- `task_exit(status="completed")` 必须拒绝 unfinished、blocked、failed 节点。
- 节点事实写在节点上：`summary`、`result`、`evidence_refs`、`changed_files`。
- 长期事实写入 memory；任务内事实不要放进 memory。

## 2. 状态和上下文如何变化

### 2.1 进入 task mode

用户输入：

```text
重构 workspace 中的 calculator.py 和 stats.py，只调整算术表达式空格，并验证结果。
```

agent 不需要用户给依赖图。LLM 根据任务目标和已知工具主动创建 DAG：

```json
{
  "goal": "重构 calculator.py 和 stats.py 的算术表达式空格，并验证结果"
}
```

`task_begin` 后，状态变化：

```text
events.jsonl
  + task_mode_started

task.yaml
  status: active
  goal: 重构 calculator.py 和 stats.py 的算术表达式空格，并验证结果

plan.yaml
  root: n_goal
  nodes:
    - id: n_goal
      type: goal
      status: verified

context.md
  出现当前任务目标和空 DAG 投影
```

模型下一次调用会看到 dynamic suffix 中的任务投影：

```text
# Task State Projection
Task: active
Goal: 重构 calculator.py 和 stats.py 的算术表达式空格，并验证结果
Plan:
- n_goal verified goal
Next action: add or autofill executable DAG nodes
```

### 2.2 自动生成初始 DAG

agent 调用：

```json
{
  "scope": "refactor",
  "constraints_json": "{\"artifacts\":[\"calculator.py changed\",\"stats.py changed\"],\"checks\":[\"read files before writing\",\"verify final content\"]}"
}
```

`plan_autofill(scope="refactor")` 生成保守 DAG：

```yaml
nodes:
  - id: n_goal
    type: goal
    status: verified
  - id: n_inspect
    type: inspection
    title: Inspect target files and constraints
    depends_on: [n_goal]
    status: ready
  - id: n_implement
    type: implementation
    title: Apply scoped refactor
    depends_on: [n_inspect]
    status: pending
  - id: n_verify
    type: verification
    title: Verify refactor result
    depends_on: [n_implement]
    status: pending
  - id: n_report
    type: report
    title: Summarize result and evidence
    depends_on: [n_verify]
    status: pending
```

投影给模型的上下文只应包含执行需要的信息，不应塞入所有版本号和事件 ID：

```text
Plan v2 active
Ready: n_inspect
Running: none
Pending: n_implement, n_verify, n_report

Nodes:
- n_inspect ready inspection: Inspect target files and constraints
- n_implement pending implementation: Apply scoped refactor
- n_verify pending verification: Verify refactor result
- n_report pending report: Summarize result and evidence
```

## 3. 节点执行和状态投影

### 3.1 inspection 节点

agent 调用 `plan_next()`：

```json
{
  "id": "n_inspect",
  "status": "running",
  "depends_on": ["n_goal"]
}
```

状态变化：

```text
plan.yaml
  n_inspect.status: running

state.yaml
  plan.running_nodes: [n_inspect]
  plan.ready_nodes: []

context.md
  n_inspect running
```

agent 执行工具：

```text
filesystem_read(path="calculator.py")
filesystem_read(path="stats.py")
```

然后把节点事实写回 DAG：

```json
{
  "node_id": "n_inspect",
  "status": "verified",
  "summary": "已读取 calculator.py 和 stats.py，发现两个目标表达式分别是 `a+b` 与 `total/count`。",
  "result": "需要最小修改：calculator.py 改为 `a + b`，stats.py 改为 `total / count`。",
  "evidence_refs_json": "[\"filesystem_read:calculator.py\", \"filesystem_read:stats.py\"]"
}
```

新的上下文投影：

```text
Completed / verified:
- n_inspect verified
  summary=已读取 calculator.py 和 stats.py...
  result=需要最小修改...
  evidence_refs=[filesystem_read:calculator.py, filesystem_read:stats.py]

Ready:
- n_implement
```

这就是 DAG 对模型的“工作记忆”。同一任务后续不需要重新把完整文件内容塞进 prompt；需要时用文件工具或 cache ref 精确读取。

### 3.2 implementation 节点

agent 调用 `plan_next()`，执行写入：

```text
filesystem_write(path="calculator.py", content="def add(a, b):\n    return a + b\n")
filesystem_write(path="stats.py", content="def mean(total, count):\n    return total / count\n")
```

节点更新：

```json
{
  "node_id": "n_implement",
  "status": "completed",
  "summary": "已完成两个文件的算术空格重构。",
  "result": "calculator.py 和 stats.py 均只发生目标表达式格式化修改。",
  "changed_files_json": "[\"calculator.py\", \"stats.py\"]",
  "evidence_refs_json": "[\"filesystem_write:calculator.py\", \"filesystem_write:stats.py\"]"
}
```

### 3.3 verification 节点

agent 调用 `plan_next()`，验证文件结果：

```text
filesystem_read(path="calculator.py")
filesystem_read(path="stats.py")
```

如果验证通过：

```json
{
  "node_id": "n_verify",
  "status": "verified",
  "summary": "最终文件内容包含 `return a + b` 和 `return total / count`。",
  "result": "目标重构已验证。",
  "evidence_refs_json": "[\"filesystem_read:calculator.py\", \"filesystem_read:stats.py\"]"
}
```

如果验证失败，不要隐式修正或假装完成。应该标记失败或添加修复节点。

## 4. LLM 主动动态修改 DAG

动态 DAG 来自 agent 在执行中发现的新事实，不是用户手工给依赖图。

### 4.1 执行中发现缺少验证步骤

场景：`n_verify` 发现 `stats.py` 还没有被验证，agent 需要插入一个更具体的验证节点。

```json
{
  "nodes_json": "[{\"id\":\"n_verify_stats\",\"type\":\"verification\",\"title\":\"Verify stats.py expression\",\"depends_on\":[\"n_implement\"],\"status\":\"ready\",\"success_criteria\":[\"stats.py contains return total / count\"]}]",
  "reason": "执行验证时发现 stats.py 需要单独记录证据"
}
```

状态变化：

```text
graph.jsonl
  + plan_node_added n_verify_stats

plan.yaml
  n_verify_stats ready depends_on=[n_implement]

context.md
  Ready: n_verify_stats, n_verify
```

随后 agent 可以先执行 `n_verify_stats`，再把原 `n_verify` 作为整体验证节点。

### 4.2 依赖关系需要调整

场景：agent 发现 `n_report` 不能只依赖 `n_verify`，还必须等待新增的 `n_verify_stats`。

```json
{
  "node_id": "n_report",
  "status": "pending",
  "reason": "报告节点必须等待所有验证证据完成",
  "depends_on_json": "[\"n_verify\", \"n_verify_stats\"]"
}
```

这会生成新的 plan version。旧版本不删除，`versions/plans/` 中保留 before/after snapshot。

### 4.3 取消或替换错误节点

场景：agent 创建了过宽的 `n_run_tests`，但当前沙箱没有测试命令，真实需要的是读取文件验证。

不要写模型名或任务名特判。用 DAG 事件表达纠正：

```json
{
  "node_id": "n_run_tests",
  "reason": "当前任务只需要文件内容验证，测试命令不可用；改用文件读取验证节点"
}
```

调用：

```text
plan_remove_node(node_id="n_run_tests", reason="...")
plan_add_nodes(nodes_json="[{\"id\":\"n_verify_files\",\"type\":\"verification\",\"title\":\"Verify target file contents\",\"depends_on\":[\"n_implement\"],\"status\":\"ready\"}]")
```

投影：

```text
Superseded:
- n_run_tests

Ready:
- n_verify_files
```

## 5. 错误和恢复例子

### 5.1 未完成节点导致 task_exit 拒绝

如果 `n_report` 仍是 pending，agent 调用：

```json
{"status": "completed"}
```

runtime 返回：

```json
{
  "status": "blocked",
  "completion_errors": [
    "unfinished plan nodes: n_report"
  ]
}
```

正确恢复：

```text
plan_next()
summary_add(...)
plan_update(node_id="n_report", status="verified", summary=..., result=..., evidence_refs_json=...)
task_status()
task_exit(status="completed")
```

### 5.2 工具被权限或沙箱阻断

如果当前节点需要写 `/home/user/secret.txt`，sandbox 返回 deny/ask。agent 不能绕过工具，也不能改用 shell 偷写。

正确处理：

```json
{
  "node_id": "n_implement",
  "status": "blocked",
  "reason": "写入目标路径需要用户授权或 sandbox resource 配置变更"
}
```

如果用户授权后恢复，agent 可以添加替代节点或重新运行该节点：

```json
{
  "nodes_json": "[{\"id\":\"n_implement_after_approval\",\"type\":\"implementation\",\"title\":\"Apply write after sandbox approval\",\"depends_on\":[\"n_inspect\"],\"status\":\"ready\"}]",
  "reason": "用户已授权一次性写入"
}
```

### 5.3 验证失败

如果 `filesystem_read(stats.py)` 仍显示：

```python
return total/count
```

agent 不应把 `n_verify` 标记为 verified。正确状态：

```json
{
  "node_id": "n_verify",
  "status": "failed",
  "summary": "stats.py 未包含目标表达式。",
  "result": "验证失败，仍需修复 stats.py。",
  "evidence_refs_json": "[\"filesystem_read:stats.py\"]",
  "reason": "stats.py remains `return total/count`"
}
```

然后添加修复节点：

```json
{
  "nodes_json": "[{\"id\":\"n_fix_stats\",\"type\":\"implementation\",\"title\":\"Fix stats.py spacing\",\"depends_on\":[\"n_verify\"],\"status\":\"ready\"},{\"id\":\"n_reverify_stats\",\"type\":\"verification\",\"title\":\"Reverify stats.py\",\"depends_on\":[\"n_fix_stats\"],\"status\":\"pending\"}]",
  "reason": "验证失败后追加修复和复验节点"
}
```

如果当前调度规则不允许 failed 节点满足依赖，应使用 `plan_update` 将失败节点改为 `completed` 并记录失败事实，或者使用新的依赖策略。不要通过隐藏特判让某个节点绕过调度器。

## 6. Context 流程

每次模型调用的消息结构：

```text
[stable system prefix]
[conversation history]
[dynamic task suffix]
```

stable system prefix 包含：

```text
- system template
- personality instructions
- long-term memory
- tool/runtime rules
- sandbox summary
```

dynamic task suffix 包含：

```text
- 当前时间和用户状态
- active_subagents / pending_mailbox_items
- system_notice
- 当前 task projection
- ready/running/blocked/failed 节点摘要
- 最近 summary artifacts
```

设计要求：

- 不要把所有 event_id、turn_id、tool_call_id 都注入 prompt；这些属于日志和协议。
- prompt 中只放模型决策需要的状态：目标、当前节点、依赖、状态、摘要、证据 ref。
- 大工具输出进入 cache，prompt 只放 `cache://` ref、summary、preview。
- compaction 不删除未完成 tool-call group，不压掉 active/running DAG 状态。

## 7. 高质量 agent 行为准则

对于能力较弱的本地模型，系统应该通过清晰 schema、短上下文、可执行 DAG 和验证反馈提升稳定性，而不是写模型特判。

推荐行为：

- 先读后写。
- 每次只推进当前 running 节点。
- 每个节点完成时写入 `summary` 和 `result`。
- 证据使用短 ref，例如 `filesystem_read:stats.py`、`shell:pytest -q`、`summary_000003`。
- 失败时显式 `failed` 或 `blocked`，再通过 DAG mutation 恢复。
- memory 只记录跨生命周期事实，例如用户偏好、长期项目约束、常用路径；不要记录当前任务执行结果。
- summary 只用于跨节点或跨轮次压缩；不要把每个小动作都写 summary。

## 8. 完成例子

最终 `n_report`：

```json
{
  "node_id": "n_report",
  "status": "verified",
  "summary": "calculator.py 和 stats.py 的目标表达式空格已重构并验证。",
  "result": "calculator.py 包含 `return a + b`；stats.py 包含 `return total / count`。",
  "evidence_refs_json": "[\"filesystem_read:calculator.py\", \"filesystem_read:stats.py\", \"summary_000001\"]",
  "changed_files_json": "[\"calculator.py\", \"stats.py\"]"
}
```

`task_status()`：

```json
{
  "mode": "task",
  "next_action": "task_exit",
  "completion_errors": []
}
```

`task_exit(status="completed")`：

```json
{
  "status": "completed",
  "message": "Task completed"
}
```
