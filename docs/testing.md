# 测试指南

本指南介绍如何使用 MOCK LLM 进行单元测试和集成测试，覆盖工具调用、重启恢复、子代理、权限控制、人机交互和持久化等场景。

## 测试架构

### MOCK LLM 设计

MOCK LLM 是一个模拟真实 LLM 行为的测试替身，支持：

- **预设响应序列**: 按顺序返回预定义的响应
- **工具调用模拟**: 生成指定的 tool_calls
- **流式输出模拟**: 支持 `astream()` 和 `astream_events()`
- **状态追踪**: 记录所有调用历史用于断言
- **异常注入**: 模拟 API 错误等异常情况

### 测试分层

```
┌─────────────────────────────────────┐
│        集成测试 (Integration)        │
│  - 完整流程测试                      │
│  - 多组件协作测试                    │
│  - 持久化验证                        │
│  - 使用 MemoryCheckpointer          │
└─────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│         单元测试 (Unit)             │
│  - 单个组件测试                      │
│  - Mock 外部依赖                     │
│  - 快速执行                          │
└─────────────────────────────────────┘
```

## MOCK LLM 实现

### 核心类定义

```python
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from typing import List, Optional, Any

class MockLLM(BaseChatModel):
    """用于测试的 MOCK LLM"""
    
    # 预设响应队列
    responses: List[Any] = []
    
    # 调用历史
    call_history: List[dict] = []
    
    # 当前响应索引
    _current_index: int = 0
    
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """生成响应（从预设队列中取）"""
        self.call_history.append({
            "messages": messages,
            "stop": stop,
            "kwargs": kwargs
        })
        
        if self._current_index >= len(self.responses):
            # 默认响应
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content="Default mock response")
            )])
        
        response = self.responses[self._current_index]
        self._current_index += 1
        
        # 处理不同类型的响应
        if isinstance(response, str):
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(content=response)
            )])
        elif isinstance(response, dict) and "tool_calls" in response:
            # 工具调用响应
            return ChatResult(generations=[ChatGeneration(
                message=AIMessage(
                    content=response.get("content", ""),
                    tool_calls=response["tool_calls"]
                )
            )])
        # ... 更多类型处理
    
    async def _astream(self, input, config, **kwargs):
        """流式输出模拟"""
        response = self._generate(input, **kwargs)
        content = response.generations[0].message.content
        
        for char in content:
            yield AIMessageChunk(content=char)
    
    # ... 其他必需方法
```

### 配置示例

```python
def create_mock_llm_for_tool_test():
    """创建用于工具调用测试的 MOCK LLM"""
    return MockLLM(
        responses=[
            # 第 1 次调用：请求执行 shell 命令
            {
                "content": "让我执行一个命令",
                "tool_calls": [{
                    "name": "shell",
                    "args": {"command": "ls -la"},
                    "id": "call_1"
                }]
            },
            # 第 2 次调用：收到工具结果后继续
            "命令执行完成，结果是..."
        ]
    )

def create_mock_llm_for_permission_test():
    """创建用于权限询问测试的 MOCK LLM"""
    return MockLLM(
        responses=[
            {
                "content": "我需要权限",
                "tool_calls": [{
                    "name": "shell",
                    "args": {"command": "rm -rf /tmp/test"},
                    "id": "call_1"
                }]
            },
            # 用户批准后继续
            "权限已获得，操作完成"
        ]
    )
```

## 测试场景

### 1. 工具调用测试

```python
import pytest
from langgraph.graph import StateGraph

@pytest.mark.asyncio
async def test_shell_tool_call():
    """测试 shell 工具调用"""
    # 设置 MOCK LLM
    mock_llm = create_mock_llm_for_tool_test()
    
    # 构建图
    tools = [create_shell_tool()]
    graph = build_agent_graph(mock_llm, tools, ...)
    
    # 执行
    config = {"configurable": {"thread_id": "test_1"}}
    input_state = {"messages": [HumanMessage(content="列出文件")]}
    
    result = await graph.ainvoke(input_state, config=config)
    
    # 断言
    assert len(mock_llm.call_history) == 2  # LLM 被调用 2 次
    assert mock_llm.call_history[0]["messages"][-1].tool_calls is not None
    
    # 检查工具是否被执行
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
```

### 2. 重启与恢复测试

```python
@pytest.mark.asyncio
async def test_restart_recovery():
    """测试重启后会话状态恢复"""
    mock_llm = create_mock_llm_with_state_tracking()
    checkpointer = MemorySaver()  # 内存检查点器
    
    graph = build_agent_graph(mock_llm, ..., checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "restart_test"}}
    
    # 第一轮对话
    result1 = await graph.ainvoke(
        {"messages": [HumanMessage(content="第一步")]},
        config=config
    )
    
    # 模拟重启：重新编译图
    graph = build_agent_graph(mock_llm, ..., checkpointer=checkpointer)
    
    # 第二轮对话（应该能读取之前的状态）
    result2 = await graph.ainvoke(
        {"messages": [HumanMessage(content="第二步")]},
        config=config
    )
    
    # 断言：消息历史包含之前的对话
    all_messages = result2["messages"]
    assert any("第一步" in str(m.content) for m in all_messages)
    assert any("第二步" in str(m.content) for m in all_messages)
```

### 3. 重连接测试

```python
@pytest.mark.asyncio
async def test_reconnection():
    """测试中断后恢复执行"""
    mock_llm = MockLLM(responses=[
        {"content": "", "tool_calls": [{"name": "ask_user", "args": {"question": "确认？"}, "id": "c1"}]},
        "继续执行"
    ])
    
    graph = build_agent_graph(mock_llm, ...)
    config = {"configurable": {"thread_id": "reconnect_test"}}
    
    # 第一次执行（会中断）
    try:
        async for event in graph.astream(
            {"messages": [HumanMessage(content="开始")]},
            config=config,
            stream_mode="updates"
        ):
            pass
    except GraphInterrupt as e:
        # 捕获中断
        interrupt_data = e.args[0]
        assert interrupt_data["question"] == "确认？"
    
    # 恢复执行
    resume_result = await graph.ainvoke(
        Command(resume={"approved": True}),
        config=config
    )
    
    # 断言恢复后继续执行
    assert len(resume_result["messages"]) > 0
```

### 4. 子代理测试

#### 4.1 Attach 模式

```python
@pytest.mark.asyncio
async def test_subagent_attach():
    """测试 Attach 模式子代理"""
    # 主代理 MOCK LLM
    main_llm = MockLLM(responses=[
        {
            "content": "创建子代理",
            "tool_calls": [{
                "name": "subagent_create_attach",
                "args": {"task": "计算 1+1", "mode": "attach"},
                "id": "c1"
            }]
        },
        "子代理完成了任务"
    ])
    
    # 子代理 MOCK LLM
    subagent_llm = MockLLM(responses=[
        "1+1=2"
    ])
    
    # 注入子代理 LLM
    with patch('xbot.graph.create_llm', return_value=subagent_llm):
        graph = build_agent_graph(main_llm, ...)
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="开始任务")]},
            config={"configurable": {"thread_id": "attach_test"}}
        )
    
    # 断言子代理被创建并执行
    assert "subagent" in str(result["messages"])
```

#### 4.2 Detach 模式

```python
@pytest.mark.asyncio
async def test_subagent_detach():
    """测试 Detach 模式子代理"""
    main_llm = MockLLM(responses=[
        {
            "content": "创建后台子代理",
            "tool_calls": [{
                "name": "subagent_create_detach",
                "args": {"task": "后台处理", "mode": "detach"},
                "id": "c1"
            }]
        },
        "子代理已启动"
    ])
    
    graph = build_agent_graph(main_llm, ..., store=test_store)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="启动后台任务")]},
        config={"configurable": {"thread_id": "detach_test"}}
    )
    
    # 断言子代理 ID 被返回
    subagent_id = extract_subagent_id(result["messages"])
    assert subagent_id is not None
    
    # 查询子代理状态
    subagent_status = await test_store.get(("subagents",), subagent_id)
    assert subagent_status is not None
```

#### 4.3 子代理工具调用

```python
@pytest.mark.asyncio
async def test_subagent_tool_call():
    """测试子代理中的工具调用"""
    # 子代理需要调用工具
    subagent_llm = MockLLM(responses=[
        {
            "content": "子代理需要写文件",
            "tool_calls": [{
                "name": "filesystem",
                "args": {"action": "write", "path": "output.txt", "content": "hello"},
                "id": "c1"
            }]
        },
        "文件写入完成"
    ])
    
    # ... 创建包含子代理的图并测试
```

### 5. Cron 任务测试

```python
@pytest.mark.asyncio
async def test_cron_job_execution():
    """测试定时任务执行"""
    # 模拟 cron 触发
    mock_llm = MockLLM(responses=[
        {
            "content": "执行定时任务",
            "tool_calls": [{
                "name": "shell",
                "args": {"command": "echo 'cron job'"},
                "id": "c1"
            }]
        },
        "定时任务完成"
    ])
    
    # 加载 jobs.json
    jobs_config = load_jobs_config()
    
    # 模拟 cron 触发
    graph = build_agent_graph(mock_llm, ...)
    
    # 手动触发 cron 节点
    result = await graph.ainvoke(
        {
            "messages": [],
            "cron_trigger": {"job_id": "daily_backup"}
        },
        config={"configurable": {"thread_id": "cron_test"}}
    )
    
    # 断言任务被执行
    assert mock_llm.call_history[0]["messages"][-1].tool_calls is not None
```

### 6. 权限系统测试

#### 6.1 允许规则测试

```python
@pytest.mark.asyncio
async def test_permission_allow_rule():
    """测试允许规则"""
    permissions = PermissionSystem(config={
        "default": "deny",
        "allow": [
            {"tool": "shell", "params": {"command": "^ls$"}}
        ]
    })
    
    # 匹配允许规则
    result = permissions.check("shell", {"command": "ls"}, {})
    assert result == "allow"
    
    # 不匹配则拒绝
    result = permissions.check("shell", {"command": "pwd"}, {})
    assert result == "deny"
```

#### 6.2 拒绝规则测试

```python
@pytest.mark.asyncio
async def test_permission_deny_rule():
    """测试拒绝规则"""
    permissions = PermissionSystem(config={
        "default": "allow",
        "deny": [
            {"tool": "shell", "params": {"command": "^rm\\s+-rf"}}
        ]
    })
    
    # 匹配拒绝规则
    result = permissions.check("shell", {"command": "rm -rf /tmp"}, {})
    assert result == "deny"
```

#### 6.3 询问规则测试

```python
@pytest.mark.asyncio
async def test_permission_ask_flow():
    """测试询问流程"""
    mock_llm = MockLLM(responses=[
        {"content": "", "tool_calls": [{"name": "shell", "args": {"command": "whoami"}, "id": "c1"}]},
        "权限已获得"
    ])
    
    permissions = PermissionSystem(config={"default": "ask"})
    graph = build_agent_graph(mock_llm, ..., permission_system=permissions)
    
    config = {"configurable": {"thread_id": "ask_test"}}
    
    # 第一次执行（会中断等待授权）
    interrupted = False
    try:
        await graph.ainvoke(
            {"messages": [HumanMessage(content="执行命令")]},
            config=config
        )
    except GraphInterrupt:
        interrupted = True
    
    assert interrupted
    
    # 恢复执行（模拟用户批准）
    result = await graph.ainvoke(
        Command(resume={"approved": True}),
        config=config
    )
    
    # 断言最终执行成功
    assert "权限已获得" in str(result["messages"][-1].content)
```

### 7. 子代理权限测试

```python
@pytest.mark.asyncio
async def test_subagent_permission_inheritance():
    """测试子代理权限继承"""
    # 主代理权限配置
    main_permissions = PermissionSystem(config={
        "default": "ask",
        "allow": [{"tool": "shell", "params": {"command": "^ls$"}}]
    })
    
    # 子代理应继承相同权限
    subagent_permissions = main_permissions.create_child_context()
    
    # 测试子代理中的权限检查
    result = subagent_permissions.check("shell", {"command": "ls"}, {})
    assert result == "allow"  # 继承允许规则
    
    result = subagent_permissions.check("shell", {"command": "pwd"}, {})
    assert result == "ask"  # 默认询问
```

### 8. 人机交互测试 (Human-in-the-Loop)

```python
@pytest.mark.asyncio
async def test_human_in_the_loop():
    """测试人机交互流程"""
    mock_llm = MockLLM(responses=[
        {
            "content": "需要用户确认",
            "tool_calls": [{
                "name": "ask",
                "args": {"question": "确定要删除吗？"},
                "id": "c1"
            }]
        },
        "用户已确认，执行删除"
    ])
    
    graph = build_agent_graph(mock_llm, ...)
    config = {"configurable": {"thread_id": "hitl_test"}}
    
    # 执行到人机交互点
    try:
        async for event in graph.astream(
            {"messages": [HumanMessage(content="删除文件")]},
            config=config
        ):
            if "ask" in str(event):
                break
    except GraphInterrupt:
        pass
    
    # 模拟用户回答
    resume_result = await graph.ainvoke(
        Command(resume={"answer": "是的，确定"}),
        config=config
    )
    
    # 断言继续执行
    assert "用户已确认" in str(resume_result["messages"][-1].content)
```

### 9. 持久化可用性测试

#### 9.1 Checkpointer 测试

```python
@pytest.mark.asyncio
async def test_checkpointer_persistence():
    """测试检查点持久化"""
    checkpointer = SQLiteCheckpointer(":memory:")  # 或临时文件
    await checkpointer.setup()
    
    # 保存检查点
    checkpoint = {
        "v": 1,
        "ts": time.time(),
        "channel_values": {"messages": [HumanMessage(content="test")]}
    }
    
    await checkpointer.aput(
        config={"configurable": {"thread_id": "persist_test"}},
        checkpoint=checkpoint,
        metadata={"source": "test"}
    )
    
    # 读取检查点
    saved = await checkpointer.aget_tuple(
        config={"configurable": {"thread_id": "persist_test"}}
    )
    
    # 断言数据一致
    assert saved is not None
    assert saved.checkpoint["channel_values"]["messages"][0].content == "test"
```

#### 9.2 Store 测试

```python
@pytest.mark.asyncio
async def test_store_persistence():
    """测试 Store 持久化"""
    store = SQLiteStore(":memory:")
    await store.setup()
    
    # 写入数据
    await store.aput(
        namespace=("test",),
        key="key1",
        value={"data": "value1"}
    )
    
    # 读取数据
    result = await store.aget(namespace=("test",), key="key1")
    assert result.value["data"] == "value1"
    
    # 搜索数据
    search_result = await store.asearch(namespace_prefix=("test",))
    assert len(search_result) == 1
```

#### 9.3 完整持久化流程测试

```python
@pytest.mark.asyncio
async def test_full_persistence_workflow():
    """测试完整持久化工作流"""
    # 使用临时数据库
    db_path = tempfile.mktemp(suffix=".db")
    
    try:
        checkpointer = SQLiteCheckpointer(db_path)
        store = SQLiteStore(db_path)
        await checkpointer.setup()
        await store.setup()
        
        mock_llm = MockLLM(responses=["响应 1", "响应 2"])
        graph = build_agent_graph(mock_llm, ..., checkpointer=checkpointer, store=store)
        
        config = {"configurable": {"thread_id": "full_persist_test"}}
        
        # 第一轮
        await graph.ainvoke(
            {"messages": [HumanMessage(content="消息 1")]},
            config=config
        )
        
        # 第二轮
        await graph.ainvoke(
            {"messages": [HumanMessage(content="消息 2")]},
            config=config
        )
        
        # 新建图实例（模拟重启）
        mock_llm2 = MockLLM(responses=["响应 3"])
        graph2 = build_agent_graph(mock_llm2, ..., checkpointer=checkpointer, store=store)
        
        # 第三轮（应能读取之前状态）
        result = await graph2.ainvoke(
            {"messages": [HumanMessage(content="消息 3")]},
            config=config
        )
        
        # 断言历史消息存在
        all_contents = [str(m.content) for m in result["messages"]]
        assert any("消息 1" in c for c in all_contents)
        assert any("消息 2" in c for c in all_contents)
        assert any("消息 3" in c for c in all_contents)
        
    finally:
        # 清理临时文件
        if os.path.exists(db_path):
            os.remove(db_path)
```

## 运行测试

### 安装测试依赖

```bash
pip install pytest pytest-asyncio pytest-cov
```

### 运行所有测试

```bash
pytest tests/ -v
```

### 运行特定测试

```bash
# 运行工具调用测试
pytest tests/test_agent.py::test_shell_tool_call -v

# 运行权限测试
pytest tests/test_permissions.py -v

# 运行持久化测试
pytest tests/test_persistence.py -v
```

### 生成覆盖率报告

```bash
pytest --cov=src --cov-report=html
# 打开 htmlcov/index.html 查看报告
```

## 最佳实践

### 1. 测试隔离

- 每个测试使用独立的 `thread_id`
- 使用临时数据库或内存数据库
- 测试后清理资源

### 2. MOCK 设计原则

- **行为一致**: MOCK 应模拟真实 LLM 的行为特征
- **可配置**: 通过参数灵活配置响应
- **可观测**: 记录调用历史便于断言
- **可扩展**: 易于添加新的模拟场景

### 3. 测试数据管理

```python
# 使用 fixtures 管理测试数据
@pytest.fixture
def mock_llm_for_tools():
    return MockLLM(responses=[...])

@pytest.fixture
def temp_db():
    db_path = tempfile.mktemp(suffix=".db")
    yield db_path
    os.remove(db_path)
```

### 4. 异步测试注意事项

```python
# 标记异步测试
@pytest.mark.asyncio
async def test_async_feature():
    ...

# 或使用 asyncio.run
def test_async_feature():
    async def _run():
        ...
    asyncio.run(_run())
```

## 故障排查

### 常见问题

**Q: 测试卡在异步循环？**

A: 确保所有异步操作都使用 `await`，并使用 `pytest-asyncio`。

**Q: MOCK LLM 响应顺序错乱？**

A: 检查 `responses` 列表顺序，确保与预期调用次数匹配。

**Q: 持久化测试失败？**

A: 检查数据库连接是否正确关闭，避免锁定问题。

**Q: 子代理测试超时？**

A: 设置合理的超时时间，或使用 `asyncio.wait_for` 限制执行时间。

```python
@pytest.mark.asyncio
async def test_with_timeout():
    result = await asyncio.wait_for(
        graph.ainvoke(...),
        timeout=30.0  # 30 秒超时
    )
```

## 持续集成

在 CI/CD 中运行测试：

```yaml
# .github/workflows/test.yml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'
      - name: Install dependencies
        run: pip install -r requirements.txt -r requirements-test.txt
      - name: Run tests
        run: pytest tests/ -v --cov=src
      - name: Upload coverage
        uses: codecov/codecov-action@v3
```
