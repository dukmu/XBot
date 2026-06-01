可以。基于你前面提出的  **DAG 执行轨迹 + 节点状态 + 文件即 state** ，我会把一个 “Hermes-like agent system” 设计成：

> **一个极小内核 + 文件状态图 + 插件化工具/技能 + 可替换 Loop 阶段 + 可重放执行日志。**

不要一开始做“大而全 agent 框架”。真正重要的是两件事：

1. **State 设计必须稳定、可读、可恢复、可版本化。**
2. **Loop 阶段必须少、清晰、可插拔、可观测。**

Hermes Agent 的现有方向也很有参考价值：它强调持久记忆、跨会话搜索、技能生成与改进、工具调用、子 agent、上下文压缩和持久化；其文档中还单独描述了 agent loop 的 turn lifecycle、工具执行、预算、fallback、compression/persistence 等机制。([Hermes Agent](https://hermes-agent.nousresearch.com/docs/?utm_source=chatgpt.com "Hermes Agent Documentation")) 但如果你要自己设计，我建议比 Hermes 更“少即是多”：先不要追求自改进、多入口、多 agent，而是先把 **state/loop/harness** 做对。

---

# 1. 总体原则：不要做“会聊天的框架”，要做“可恢复的运行时”

一个 agent 系统最容易走偏成：

```text
messages + tools + while loop + memory
```

这很快会变成不可调试的黑盒。

更好的最小架构是：

```text
agent/
  kernel/             # 极小内核：状态机、调度器、权限、事件日志
  runtime/            # loop 执行器
  workspace/          # 当前任务工作区
  state/              # 文件化 state
  artifacts/          # 产物
  skills/             # 可复用技能
  plugins/            # 工具插件
  evals/              # 回归测试和任务集
```

这里最关键的思想是：

> **模型不是系统中心，state 才是系统中心。**
> LLM 只是 planner / executor / verifier / summarizer 之一。

OpenAI 关于 Codex harness 的文章也强调，agent 能力并不只来自模型，而是来自 model–harness–environment 系统：harness 决定模型如何观察项目、调用工具、获取反馈、验证完成。([OpenAI](https://openai.com/index/harness-engineering/?utm_source=chatgpt.com "Harness engineering: leveraging Codex in an agent-first ...")) 近期 arXiv 的 harness engineering 论文也把 task state、observability、verification、permissions、failure attribution 等作为 agent runtime 的核心职责，而不是附属功能。([arXiv](https://arxiv.org/abs/2605.13357?utm_source=chatgpt.com "AI Harness Engineering: A Runtime Substrate for Foundation-Model Software Agents"))

---

# 2. 文件即 State：推荐目录结构

我建议每个任务都是一个独立目录，目录本身就是状态数据库。

```text
.hermes/
  config.yaml
  plugins/
  skills/
  memory/
  tasks/
    task_20260601_001/
      task.yaml
      goal.md
      plan.yaml
      graph.jsonl
      state.yaml
      context.md
      claims.yaml
      artifacts/
        diffs/
        logs/
        reports/
        screenshots/
        models/
      checkpoints/
      summaries/
      events.jsonl
      locks/
```

其中：

```text
task.yaml      任务元数据
goal.md        用户目标和边界
plan.yaml      当前计划图
graph.jsonl    append-only DAG 事件流
state.yaml     当前 materialized state
context.md     给模型看的上下文投影
claims.yaml    当前被支持/被反驳/过期的结论
artifacts/     所有文件产物
events.jsonl   所有 runtime 事件
```

重点：
**不要只存最终 state，要同时存 append-only event log。**

也就是：

```text
events.jsonl / graph.jsonl = 真相来源
state.yaml = 从事件流 materialize 出来的当前视图
```

这样你能 replay、debug、rollback、比较不同 loop 策略。

---

# 3. State 分层：不要一个大 JSON

一个好的 agent state 至少分六层。

## 3.1 Goal State：用户到底要什么

```yaml
# goal.yaml
id: goal_root
original_request: |
  设计一个文件即state的Hermes-like agent系统
current_interpretation: |
  重点设计DAG状态图、执行Loop、模块化/插件化架构
constraints:
  - 少即是多
  - 模块化
  - 插件化
  - 可迭代升级
  - 文件即state
non_goals:
  - 不做复杂多agent系统
  - 不先做GUI
success_criteria:
  - 给出可落地state schema
  - 给出loop阶段设计
  - 给出MVP迭代路线
```

Goal State 必须稳定存在，因为长任务中模型最容易漂移。

---

## 3.2 Plan State：计划是可执行图，不是 Markdown 列表

```yaml
# plan.yaml
version: 3
status: active
root: n_goal
nodes:
  - id: n1
    type: subtask
    title: 设计文件状态结构
    depends_on: []
    status: verified
    success_criteria:
      - state目录结构明确
      - 支持恢复和回放

  - id: n2
    type: subtask
    title: 设计Loop阶段
    depends_on: [n1]
    status: running
    success_criteria:
      - 阶段数量少
      - 每阶段输入输出明确
      - 支持插件hook
```

Plan State 要遵守一个原则：

> **计划不可原地改写。计划变更产生新版本。**

```text
plan_v1 -> plan_v2 -> plan_v3
```

旧计划不要删除，只标记：

```yaml
superseded_by: plan_v3
reason: "执行中发现需要加入 verification phase"
```

Anthropic 的 “building effective agents” 里有一个很重要的实践原则：优先使用简单、可组合的 workflow；只有任务路径不确定时才增加 agent 自治。([Anthropic](https://www.anthropic.com/research/building-effective-agents?utm_source=chatgpt.com "Building Effective AI Agents")) 所以你的 Plan Mode 不应该只是“让模型想一想”，而应该产出 scheduler 能理解的任务图。

---

## 3.3 Graph State：DAG 是执行真相

建议用 `graph.jsonl` 做 append-only。

```json
{"event":"node_created","id":"n1","type":"plan","title":"设计state schema","ts":"2026-06-01T10:00:00Z"}
{"event":"node_started","id":"n1","ts":"2026-06-01T10:01:00Z"}
{"event":"artifact_created","id":"a1","type":"document","path":"artifacts/reports/state_schema.md","producer":"n1"}
{"event":"node_succeeded","id":"n1","ts":"2026-06-01T10:05:00Z"}
{"event":"node_verified","id":"n1","verifier":"v1","ts":"2026-06-01T10:06:00Z"}
```

同时你可以 materialize 出一个 `state.yaml`：

```yaml
active_node: n2
ready_nodes: [n3, n4]
blocked_nodes: []
failed_nodes: []
verified_nodes: [n1]
artifacts:
  a1:
    path: artifacts/reports/state_schema.md
    producer: n1
```

为什么要 `jsonl`？

因为它天然支持：

```text
append-only
diff
git versioning
streaming
replay
partial recovery
```

这比 SQLite 更符合“文件即 state”。当然，后期可以加 SQLite/duckdb 作为索引层，但真相仍然保留在文件事件流里。

---

## 3.4 Claim State：结论必须显式化

这是很多 agent 框架缺失的关键层。

```yaml
# claims.yaml
claims:
  - id: c1
    text: "当前任务适合单agent + 插件化skills，不适合一开始做多agent系统"
    status: supported
    confidence: high
    evidence:
      - n1
      - a1
    scope:
      task: task_20260601_001
    invalidates_if:
      - "用户要求并行多角色协作"
      - "任务需要长期后台监控"

  - id: c2
    text: "graph.jsonl 是当前任务的source of truth"
    status: verified
    evidence:
      - n_state_design
```

区分：

```text
Observation: 工具返回了什么
Claim: agent 相信什么
Decision: agent 决定做什么
Artifact: agent 产出了什么
```

这会让系统的推理链更可审计。

---

## 3.5 Artifact State：产物是第一等对象

```yaml
# artifacts/index.yaml
artifacts:
  - id: a_diff_001
    type: diff
    path: artifacts/diffs/001.patch
    producer: n_patch_code
    hash: sha256:...
    dependencies:
      - n_design_api

  - id: a_log_001
    type: test_log
    path: artifacts/logs/pytest_001.log
    producer: n_run_tests
    supports:
      - c_tests_pass
```

Agent 不应该只“说它做了什么”，而应该产生可验证文件：

```text
diff
log
report
benchmark
screenshot
json result
model file
onnx file
profile result
```

近期 “Code as Agent Harness” 这类工作也强调，代码和可执行产物正在成为 agent 运行时的核心基底：agent 的计划、工具、环境建模、验证都应该尽量可执行、可复用、可审计。([arXiv](https://arxiv.org/abs/2605.18747?utm_source=chatgpt.com "Code as Agent Harness"))

---

## 3.6 Runtime State：只存当前执行必要信息

```yaml
# state.yaml
task_id: task_20260601_001
phase: execute
active_node: n2
last_event_id: e102
budget:
  max_iterations: 20
  used_iterations: 7
  max_tool_calls: 50
  used_tool_calls: 13
permissions:
  filesystem: workspace_write
  network: disabled
  shell: approval_required_for_dangerous
locks:
  - locks/active_node.lock
```

Runtime State 要小。
不要把所有历史塞进去。

---

# 4. Loop 阶段设计：建议 7 阶段，但核心只有 4 个

我建议设计为：

```text
0. Intake
1. Context Projection
2. Plan
3. Select
4. Act
5. Observe
6. Verify
7. Reflect / Persist
```

但 MVP 里可以收缩成四个核心：

```text
Plan -> Act -> Observe -> Verify
```

更完整的 loop：

```text
Intake
  ↓
Project Context
  ↓
Plan / Replan
  ↓
Select Ready Node
  ↓
Act
  ↓
Observe
  ↓
Verify
  ↓
Update Graph
  ↓
Reflect / Persist
  ↓
Repeat or Finish
```

---

## 4.1 Intake：固定目标和边界

输入：

```text
用户请求
当前 workspace
已有 memory
已有 task state
```

输出：

```text
goal.yaml
task.yaml
初始 graph node
```

这里不要做复杂 reasoning，只做：

```text
识别目标
识别约束
识别不做什么
识别风险
建立任务目录
```

---

## 4.2 Context Projection：从全图投影局部上下文

这是文件即 state 的关键。

不要把整个 `.hermes/tasks/...` 都塞给模型，而是生成：

```text
context.md
```

例如：

```md
# Current Task Context

## Goal
设计一个文件即state的Hermes-like agent系统。

## Active Node
n2: 设计 Loop 阶段。

## Direct Dependencies
- n1: state schema 已完成。

## Relevant Claims
- c1: 计划应该是可执行图，而非自然语言列表。
- c2: graph.jsonl 是 source of truth。

## Available Artifacts
- artifacts/reports/state_schema.md

## Constraints
- 少即是多
- 插件化
- 可迭代升级
```

这个阶段是纯函数：

```python
context.md = project(graph, active_node, memory, artifacts)
```

Anthropic 后续关于 context engineering 的文章也强调，agent 的有效性很大程度取决于给模型什么上下文、不给什么上下文，而不是简单扩充上下文窗口。([Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents?utm_source=chatgpt.com "Effective context engineering for AI agents"))

---

## 4.3 Plan / Replan：只规划缺口，不重写世界

Plan 阶段只做两件事：

```text
如果没有计划：生成计划图
如果计划失效：生成局部补丁
```

不要每轮都“重新规划整个任务”。
应该是：

```text
plan patch
```

例如：

```yaml
plan_patch:
  add_nodes:
    - id: n_verify_schema
      type: verification
      depends_on: [n_state_schema]
  supersede_nodes:
    - old: n_loop_design
      new: n_loop_design_v2
      reason: "增加插件hook设计"
```

这样计划迭代是可审计的。

---

## 4.4 Select：调度器选择一个 ready node

这一步最好不用 LLM，或者只让 LLM 给建议。

```python
def select_ready_node(state, graph):
    ready = [
        n for n in graph.nodes
        if n.status == "ready"
        and deps_verified(n)
        and budget_ok(n)
        and permissions_ok(n)
    ]
    return priority_sort(ready)[0]
```

优先级建议：

```text
1. verification 节点优先
2. unblock 当前主链路的节点优先
3. 低成本诊断优先
4. 高风险动作需要审批
5. 可并行节点可以交给 worker
```

---

## 4.5 Act：执行动作

Act 阶段可以有多种 executor：

```text
llm_executor
tool_executor
shell_executor
browser_executor
skill_executor
human_executor
```

但接口统一：

```python
class Executor:
    def can_handle(node) -> bool: ...
    def run(node, context, tools) -> ExecutionResult: ...
```

输出必须结构化：

```yaml
result:
  status: succeeded
  observations:
    - id: obs_001
      type: tool_output
      path: artifacts/logs/tool_001.log
  artifacts:
    - id: a_001
      type: report
      path: artifacts/reports/loop_design.md
  proposed_claims:
    - text: "Loop 应该分为 Plan/Act/Observe/Verify"
      confidence: medium
```

---

## 4.6 Observe：把外部反馈落盘

Observe 不应该只是 append 到 messages。

它应该创建节点：

```text
tool_call node
observation node
artifact node
error node
```

例如：

```json
{"event":"tool_call_started","node":"n_run_tests","tool":"shell","args":{"cmd":"pytest"}}
{"event":"observation_created","node":"obs_12","content_path":"artifacts/logs/pytest.log"}
{"event":"artifact_created","id":"a_pytest_log","path":"artifacts/logs/pytest.log"}
```

---

## 4.7 Verify：验证与执行分离

任何重要节点都应该有 verifier。

```yaml
verifier:
  type: checklist
  checks:
    - "是否产生了文件化state schema"
    - "是否区分了goal/plan/graph/artifact/runtime"
    - "是否支持append-only replay"
```

对于代码任务：

```yaml
verifier:
  type: command
  command: "pytest tests/"
```

对于文档任务：

```yaml
verifier:
  type: llm_review
  rubric: "是否满足用户约束，是否过度复杂"
```

Anthropic 的 evaluator-optimizer 模式本质上就是把生成和评价拆开；这比一个 agent 自说自话稳定得多。([Anthropic](https://www.anthropic.com/research/building-effective-agents?utm_source=chatgpt.com "Building Effective AI Agents"))

---

## 4.8 Reflect / Persist：反思不是写小作文，而是更新 skill/memory

Reflect 阶段只允许产生三类东西：

```text
memory candidate
skill patch
eval case
```

例如：

```yaml
memory_candidates:
  - scope: project
    text: "用户偏好文件即state、DAG execution graph、少即是多的agent设计"
    evidence: [task_20260601_001]

skill_patches:
  - skill: agent_state_design
    change: "加入claims.yaml和graph.jsonl双层状态"

eval_cases:
  - name: "loop_design_should_not_overbuild"
    input: "设计agent loop"
    expected_properties:
      - "阶段少"
      - "state落盘"
      - "verification独立"
```

Hermes Agent 的一个核心卖点是 learning loop：从经验中创建 skill、改进 skill、持久化记忆。([Hermes Agent](https://hermes-agent.nousresearch.com/docs/?utm_source=chatgpt.com "Hermes Agent Documentation")) 但我建议初期不要让它自动改核心逻辑。先让它产生候选项，人工或测试通过后再合并。

---

# 5. “少即是多”的模块划分

不要一开始做 20 个模块。MVP 只需要 8 个模块。

```text
1. StateStore
2. GraphStore
3. ContextProjector
4. Planner
5. Scheduler
6. Executor
7. Verifier
8. Skill/Plugin Registry
```

## 5.1 StateStore

负责文件读写、锁、版本。

```python
class StateStore:
    def load_task(task_id): ...
    def append_event(task_id, event): ...
    def materialize(task_id): ...
    def write_artifact(task_id, artifact): ...
```

## 5.2 GraphStore

负责节点和边。

```python
class GraphStore:
    def add_node(node): ...
    def add_edge(src, dst, type): ...
    def get_ready_nodes(): ...
    def mark_status(node_id, status): ...
    def invalidate_downstream(node_id): ...
```

## 5.3 ContextProjector

负责生成给模型看的 `context.md`。

```python
class ContextProjector:
    def project(task_id, active_node_id) -> str: ...
```

## 5.4 Planner

只负责生成/修补计划。

```python
class Planner:
    def create_plan(goal, context) -> PlanPatch: ...
    def repair_plan(error, graph) -> PlanPatch: ...
```

## 5.5 Scheduler

尽量 deterministic。

```python
class Scheduler:
    def pick_next(graph, state) -> Node: ...
```

## 5.6 Executor

执行节点。

```python
class Executor:
    def run(node, context) -> ExecutionResult: ...
```

## 5.7 Verifier

验证节点。

```python
class Verifier:
    def verify(node, result) -> VerificationResult: ...
```

## 5.8 Plugin Registry

注册工具和技能。

```python
class PluginRegistry:
    def list_tools(): ...
    def get_tool(name): ...
    def list_skills(): ...
    def get_skill(name): ...
```

Anthropic 关于工具设计的文章强调，高质量工具对 agent 效果非常关键；工具应当清晰、面向模型、接口稳定，并能通过 eval 迭代。([Anthropic](https://www.anthropic.com/engineering/writing-tools-for-agents?utm_source=chatgpt.com "Writing effective tools for AI agents—using ...")) 所以插件系统最重要的不是“动态加载很酷”，而是 schema、权限、文档、测试。

---

# 6. 插件化设计：Tool、Skill、Middleware 分开

不要把插件都叫 plugin。至少分三类。

## 6.1 Tool Plugin：能做外部动作

```yaml
# plugins/shell/plugin.yaml
name: shell
type: tool
version: 0.1
permissions:
  - filesystem:workspace
  - shell:restricted
entrypoint: shell_tool.py
schema:
  input:
    cmd: string
    cwd: string
  output:
    exit_code: int
    stdout_path: string
    stderr_path: string
risk_level: high
```

## 6.2 Skill Plugin：程序性知识

Skill 更像一个文件夹，不一定有代码。

```text
skills/
  code_review/
    skill.yaml
    instructions.md
    checklist.md
    examples/
    evals/
```

```yaml
name: code_review
type: skill
version: 0.2
triggers:
  - "review code"
  - "check patch"
inputs:
  - diff
outputs:
  - review_report
```

Anthropic 研究者近来也强调，与其构造很多专用 agent，不如用一个通用 agent 加一组可组合 skills；skills 本质上是组织化的文件集合，封装程序性知识和组织流程。([Business Insider](https://www.businessinsider.com/anthropic-researchers-ai-agent-skills-barry-zhang-mahesh-murag-2025-12?utm_source=chatgpt.com "Anthropic researchers say the industry should stop building tons of AI agents &amp;mdash; the real breakthrough is something simpler")) 这和你的“文件即 state”思想非常一致。

## 6.3 Middleware Plugin：改变 Loop 行为

例如：

```text
budget_guard
permission_guard
prompt_injection_guard
context_compressor
artifact_indexer
trace_exporter
```

Middleware 不直接做任务，而是在 loop 阶段插入 hook：

```python
before_plan()
after_plan()
before_act()
after_observe()
before_verify()
after_finish()
```

---

# 7. Loop Hook 设计：模块化升级的关键

每个阶段提供 hook：

```python
class LoopHooks:
    def before_context(self, state): ...
    def after_context(self, context): ...

    def before_plan(self, state, context): ...
    def after_plan(self, plan_patch): ...

    def before_act(self, node, context): ...
    def after_act(self, result): ...

    def before_verify(self, node, result): ...
    def after_verify(self, verification): ...

    def before_persist(self, updates): ...
    def after_persist(self, state): ...
```

这样你以后可以迭代：

```text
加入更好的上下文压缩器
加入更严格的权限系统
加入新的 verifier
加入远程工具
加入多 agent worker
加入自动 skill 更新
```

而不需要重写内核。

---

# 8. 最小 Loop 伪代码

```python
def run_task(task_id):
    state = store.materialize(task_id)

    while not state.done:
        context = context_projector.project(task_id, state.active_node)

        plan_patch = planner.maybe_plan_or_repair(state, context)
        if plan_patch:
            graph.apply_patch(plan_patch)
            store.append_event({"type": "plan_patch_applied", "patch": plan_patch})

        node = scheduler.pick_next(graph, state)
        if node is None:
            store.append_event({"type": "blocked", "reason": "no_ready_nodes"})
            break

        store.append_event({"type": "node_started", "node_id": node.id})

        result = executor.run(node, context)
        store.persist_result(node, result)

        verification = verifier.verify(node, result)
        store.persist_verification(node, verification)

        if verification.passed:
            graph.mark_verified(node.id)
        else:
            graph.mark_failed(node.id)
            recovery = planner.repair_plan(verification.error, graph)
            graph.apply_patch(recovery)

        state = store.materialize(task_id)
```

这个 loop 的特点：

```text
简单
可中断
可恢复
可插 hook
所有状态落盘
所有行为可 replay
```

---

# 9. Agent State 的最小 Schema

下面是我认为最值得固定下来的核心 schema。

## Node

```yaml
id: n_loop_design
type: subtask
title: 设计 Loop 阶段
status: running
parents:
  - n_root
depends_on:
  - n_state_schema
produces:
  - a_loop_design_doc
consumes:
  - a_state_schema_doc
success_criteria:
  - 阶段数量少
  - 每阶段输入输出明确
  - 支持插件hook
  - 支持中断恢复
metadata:
  created_at: "2026-06-01T10:00:00Z"
  version: 1
  risk_level: low
```

## Edge

```yaml
src: n_state_schema
dst: n_loop_design
type: depends_on
```

边类型建议先只保留 8 个：

```text
depends_on
produces
consumes
supports
contradicts
verifies
supersedes
invalidates
```

不要一开始设计 30 种边。

## Event

```yaml
id: e_001
type: node_started
node_id: n_loop_design
timestamp: "2026-06-01T10:01:00Z"
payload: {}
```

## Artifact

```yaml
id: a_loop_design_doc
type: document
path: artifacts/reports/loop_design.md
producer: n_loop_design
hash: sha256:...
metadata:
  format: markdown
```

## Claim

```yaml
id: c_minimal_loop
text: "MVP loop 应该是 Plan -> Act -> Observe -> Verify"
status: supported
confidence: high
evidence:
  - n_loop_design
  - a_loop_design_doc
scope:
  task_id: task_20260601_001
invalidates_if:
  - "需要多agent并行"
  - "需要长期后台任务"
```

---

# 10. 如何迭代升级：从 H0 到 H4

我建议按 5 个阶段做，不要一口吃成 Hermes。

## H0：单任务文件状态机

目标：

```text
能创建任务目录
能写 goal/plan/graph/events/artifacts
能跑最小 loop
```

不要 memory，不要 plugins，不要多 agent。

完成标准：

```text
中断后可继续
graph 可 replay
每个节点有状态
每个结果有 artifact
```

---

## H1：插件化工具

加入：

```text
tool registry
tool schema
permission policy
tool logs
```

工具先只支持：

```text
read_file
write_file
shell
search_workspace
```

完成标准：

```text
所有工具调用落盘
危险工具可拦截
工具输出保存为 artifact
```

---

## H2：Skill 系统

加入：

```text
skills/*/skill.yaml
instructions.md
checklist.md
examples/
evals/
```

但 skill 只读，不自动改。

完成标准：

```text
planner 能选择 skill
context_projector 能注入 skill 摘要
verifier 能调用 skill checklist
```

---

## H3：Memory / Claim / Summary

加入：

```text
memory/
claims.yaml
summaries/
context compression
```

注意：memory 写入要审核。

Hermes 的 persistent memory 是 bounded and curated memory，强调持久但有限、经过整理，而不是把所有历史都塞进去。([Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory?utm_source=chatgpt.com "Persistent Memory - Hermes Agent - nous research")) 这点非常重要。

完成标准：

```text
长期事实可检索
过期事实可 invalidated
summary 能追溯 derived_from
```

---

## H4：Subagent / Parallel Worker

最后再加多 agent。

不要一开始做。

Hermes 当前关于 subagent 的 issue 也能看出一个工程难点：子 agent 隔离有利于安全，但也会导致上下游共享状态困难，只能通过 parent 传递结果，增加 token 和延迟。([GitHub](https://github.com/NousResearch/hermes-agent/issues/377?utm_source=chatgpt.com "Shared Memory Pools Between Sub-Agents in Workflows ...")) 这说明多 agent 不是免费能力，必须有明确的共享上下文和隔离边界。

完成标准：

```text
子agent有独立任务目录
通过artifact/claim回传结果
不共享任意上下文
parent负责合并和验证
```

---

# 11. 文件即 State 的关键约束

如果要坚持这个思想，我建议遵守下面几条硬规则。

## 规则 1：所有重要状态必须可 `git diff`

所以优先用：

```text
yaml
jsonl
markdown
patch
log
```

少用二进制。
二进制 artifact 可以存，但必须有 metadata。

## 规则 2：事件流 append-only

不要改历史事件。

错误也不要删除：

```json
{"event":"node_failed","node":"n1","reason":"test failed"}
{"event":"node_retried","node":"n1","retry":1}
{"event":"node_succeeded","node":"n1"}
```

## 规则 3：当前状态可以重建

`state.yaml` 不是 source of truth。
真正 source of truth 是：

```text
goal.md + plan versions + graph.jsonl + events.jsonl + artifacts
```

## 规则 4：大输出不进 JSON

不要把工具 stdout 全塞进 graph。

应该：

```json
{"event":"observation_created","path":"artifacts/logs/run_001.stdout"}
```

## 规则 5：每个 artifact 有 hash

这能避免 agent 引用已变化文件却不自知。

---

# 12. “少即是多”的反面：哪些东西先不要做

先不要做：

```text
复杂知识图谱
自动长期记忆写入
自动修改自身核心代码
复杂多agent聊天
GUI workflow builder
完整MCP生态
向量数据库优先
多模型路由优先
```

这些东西不是不能做，而是容易在 state/loop 没稳定前制造复杂度。

MVP 里最值得做的是：

```text
文件状态
DAG事件流
上下文投影
工具日志
验证节点
计划版本
artifact索引
```

---

# 13. 一个推荐的最终架构图

```text
User / CLI / API
      |
      v
Task Intake
      |
      v
File State Store  <------------------------------+
      |                                           |
      v                                           |
Context Projector                                 |
      |                                           |
      v                                           |
Planner / Replanner                               |
      |                                           |
      v                                           |
Scheduler ---> Executor ---> Observer ---> Verifier
      |             |             |             |
      |             v             v             v
      |          Tool Plugins   Artifacts     Claims
      |             |             |             |
      +-------------+-------------+-------------+
                    |
                    v
             graph.jsonl / events.jsonl
                    |
                    v
              Reflect / Skill / Memory
```

---

# 14. 我会给这个系统定一个核心不变量

这是整个设计的灵魂：

> **任何一次 agent 行为，都必须能回答：
> 为什么执行？基于哪些依赖？做了什么？产生了什么文件？如何验证？影响了哪些下游节点？**

对应到 state：

```text
为什么执行        -> plan.yaml / depends_on
基于哪些依赖      -> graph edges
做了什么          -> events.jsonl
产生了什么文件    -> artifacts/index.yaml
如何验证          -> verification node
影响哪些下游节点  -> invalidates / supersedes edges
```

只要这个不变量成立，系统就能迭代升级。

---

# 15. 最后给一个非常实用的 MVP 清单

第一版只实现这些：

```text
.hermes/
  tasks/
    <task_id>/
      goal.md
      plan.yaml
      graph.jsonl
      events.jsonl
      state.yaml
      context.md
      artifacts/
        index.yaml
```

核心命令：

```bash
hermes init-task "..."
hermes plan <task_id>
hermes step <task_id>
hermes run <task_id>
hermes status <task_id>
hermes graph <task_id>
hermes resume <task_id>
```

核心节点：

```text
goal
plan
subtask
tool_call
observation
artifact
claim
verification
error
summary
```

核心 loop：

```text
project_context
maybe_plan
select_node
act
observe
verify
persist
```

核心插件：

```text
read_file
write_file
shell
llm
checklist_verifier
```

做到这里，已经比大多数“while messages + tools”的 agent 稳得多。

一句话总结：

> **Hermes-like 系统的最小正确形态，不是多 agent，不是复杂 memory，而是一个文件化、可重放、可验证的 DAG runtime。
> State 是骨架，Loop 是心跳，Plugin 是肌肉，Skill 是经验，Memory 是长期适应。先把骨架和心跳做对。**
>
