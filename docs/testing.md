# 测试指南

本文档描述当前 P0 Hermes runtime 的真实测试方式。目标是覆盖交互 runtime、LangGraph 消息链、权限、系统 sandbox、流式事件和压缩事件，而不是保留未实现设计的伪代码。

## 运行命令

```bash
uv run pytest -q
uv run python -m py_compile main.py xbot/*.py tests/test_agent.py
```

如果本机安装了 `bubblewrap`，sandbox 集成测试会真实运行；否则相关测试会自动跳过。跳过不代表 sandbox 可回退执行，生产路径仍然是 fail closed。

## 当前覆盖面

- `PermissionSystem`：allow/deny/ask 优先级和正则匹配。
- 系统 sandbox：工具注册、默认 deny、ask 一次性授权、symlink escape、shell 预检和 bubblewrap 子进程隔离。
- 工具调用：`shell`、`filesystem_*`、`ask`、`compact`、`skill_load`、memory 和 P0 subagent 记录工具。
- 交互 runtime：batch/stream 两种模式，interrupt resume，`/reset` 对应的 clean thread 语义。
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

`runtime_events` 是一次性事件，正常图循环会清空旧事件，交互层也会按事件 id 去重。

## 暂不覆盖

这些是设计目标或后续阶段，不应在 P0 测试中伪装成已实现能力：

- 真正异步执行的 subagent graph。
- mailbox、rewind、上下文树访问工具。
- SQLite checkpointer/store 持久化替代当前 `InMemorySaver/InMemoryStore`。
- 多平台 UI adapter 除 terminal 外的端到端测试。
