# 测试指南

本文档描述当前 P0 Hermes runtime 的真实测试方式。目标是覆盖交互 runtime、LangGraph 消息链、权限、系统 sandbox、流式事件和压缩事件，而不是保留未实现设计的伪代码。

## 运行命令

```bash
uv run pytest -q
uv run pytest -q tests/test_personality_runtime.py
python -m py_compile main.py scripts/provider_smoke_refactor.py xbot/*.py tests/test_agent.py tests/test_runtime_boundaries.py tests/test_personality_runtime.py
uv run python scripts/provider_smoke_refactor.py --env-file ~/env.sh --data-dir /tmp/xbot-deepseek-smoke
```

如果本机安装了 `bubblewrap`，sandbox 集成测试会真实运行；否则相关测试会自动跳过。跳过不代表 sandbox 可回退执行，生产路径仍然是 fail closed。

## 当前覆盖面

- `PermissionSystem`：allow/deny/ask 优先级和正则匹配。
- 系统 sandbox：工具注册、默认 deny、ask 一次性授权、symlink escape、shell 预检和 bubblewrap 子进程隔离。
- Runtime paths：session/personality 路径派生和 context-local 隔离。
- Personality config：canonical `data/personalities/<id>/` 布局、instructions/memory 加载、personality-scoped permissions/sandbox。
- 工具调用：`shell`、`filesystem_*`、`ask`、`compact`、`context_head`、`context_rewind`、`mailbox_send`、`mailbox_read`、`skill_load`、memory 和 P0 task record 工具。
- 工具结果 cache：大结果 file-backed cache，可通过新 cache 实例读取持久化内容。
- 交互 runtime：batch/stream 两种模式，合并 tool confirmation，interrupt resume，`/reset` 对应的 clean thread 语义。
- 文件化任务 state：任务目录初始化、`events.jsonl`/`graph.jsonl`/`context_tree.jsonl`/`mailbox.jsonl` append-only 日志、`state.yaml` materialized view、interaction 事件落盘。
- 上下文树/rewind：turn/message/tool 事件生成 context 节点，`context_rewind` 移动 head 但保留历史。
- Mailbox：send/read/ack 都写入 append-only 队列，`state.yaml` 投影 pending count。
- Subagent：attach 模式在 parent session 下运行 child thread，访问 main workspace，写 result 并通过 mailbox 回传。
- Debug tools：`debug_analyze` 汇总 DAG、plan、state、context tree、mailbox 和 subagent manifest。
- File write performance：批量事件记录只 materialize 一次 `state.yaml`，避免每条投影事件都重写状态文件。
- Plan/DAG state：校验缺失依赖、选择 ready verification node、计划更新版本化到 `checkpoints/plans`。
- Task mode：`task_begin` 写入目标和 DAG，`plan_next`/`plan_update` 推进节点，`context.md` 真实投影任务状态。
- Summaries/mailbox projection：summary artifacts 和 pending mailbox 会进入 `context.md`。
- Read locator：`filesystem_read` 支持 pattern、line range、context lines 和截断。
- Verification 阶段：校验任务目录文件、计划 DAG、事件计数和 materialized state 一致性。
- Provider/smoke refactor：隔离 data dir 中通过 `HermesInteraction` 执行 `calculator.py` 重构并验证 audit state。
- 流式事件：文本 delta、完整 tool call 归一化、避免最终消息重复、隐藏 `prepare_context` 内部总结。
- 压缩：保留 tool-call/tool-result 分组，丢弃 provider 不接受的孤儿 `ToolMessage`，并向终端发出一次性 runtime status。

## Mock LLM

测试替身在 [xbot/mock_llm.py](/home/shefrin/repo/XBot/xbot/mock_llm.py)。它支持：

- `set_response_sequence([...])`：按调用顺序返回文本或 tool call。
- `chunk_size`：控制流式文本切片。
- `call_history`：断言模型调用、消息链和工具绑定情况。
- `verify_tool_call_made(...)`：检查预设序列中是否出现过某类工具调用。

典型用法：

```python
mock_llm.set_response_sequence([
    {
        "content": "calling",
        "tool_calls": [
            {"name": "shell", "args": {"command": "pwd"}, "id": "call_1"}
        ],
    },
    {"content": "done"},
])
```

## Smoke model

`xbot.smoke_llm.SmokeRefactorLLM` 是端到端行为测试替身。它不直接改文件，而是通过真实 `filesystem_read` / `filesystem_write` 工具完成一个小型 Python 重构，用来验证 runtime、personality config、permissions、task state 和 audit log。

`scripts/provider_smoke_refactor.py` 使用真实 provider，默认读取 `DEEPSEEK_API_TOKEN` 和 `DEEPSEEK_OPENAI_BASE_URL`，模型默认是已验收通过的 `deepseek-v4-flash`。它会在隔离目录生成完整配置和 workspace。该脚本不属于普通单元测试，因为它依赖外部 provider 配额和网络。

## 写新测试的原则

- 每个 graph 测试使用独立 `thread_id`，避免 checkpointer 状态串扰。
- 优先通过 `HermesInteraction` 测试用户可见事件，通过 `build_agent_graph` 测试图级消息状态。
- 新工具必须加入 `TOOL_SANDBOX_MODE`，并补一条 sandbox enabled 时的注册或资源访问测试。
- 触碰宿主文件系统的测试使用 `temp_data_dir`，不要依赖真实 `data/sessions/default`。
- 对流式输出只断言 normalized `InteractionEvent`，不要让 terminal renderer 重新拼 provider chunk。
- 测试压缩时至少跑两轮对话，因为 `prepare_context` 会在下一次模型调用前压缩旧历史。

## 常见断言点

### Sandbox 写路径语义

`filesystem_write(path=...)` 的 `path` 必须按写操作判断；`filesystem_read` 和 `filesystem_list` 才按读操作判断。这样 ask/deny 会发生在工具执行前，而不是由 helper 在内部抛错。

### Tool Call 流式归一化

provider 可能先发 `shell({})` 形态的半成品，再发完整参数。交互层必须只发出完整 `tool_call` 事件，终端只负责渲染。

### 压缩事件

压缩总结本身不能作为 agent 文本流向用户；用户只应看到 runtime status，例如：

```text
Context compacted: summarized 2 messages, kept 1 recent messages.
```

Runtime events 通过 LangGraph custom stream 发出，不进入持久 graph state；交互层按事件 id 去重。

## 暂不覆盖

这些是设计目标或后续阶段，不应在 P0 测试中伪装成已实现能力：

- 真正异步执行的 subagent background runner。
- 官方 SQLite/Postgres 持久化替代当前 file-backed checkpoint pickle 和 `InMemoryStore`。
- 多平台 UI adapter 除 terminal 外的端到端测试。
