# 架构设计

## 系统整体架构

本系统是一个面向单用户的本地数字人 Agent，所有组件运行在同一进程中，数据持久化于 `./data/` 目录下。

### 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                      Terminal / UI Layer                     │
│                    (用户交互界面层)                            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                       Main Loop                              │
│                   (主程序循环控制)                             │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐     │
│  │ Input Handler│  │ Stream Processor│  │ Interrupt Handler│    │
│  └─────────────┘  └──────────────┘  └─────────────────┘     │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    LangGraph State Graph                     │
│                    (状态图编排引擎)                           │
│  ┌─────────┐    ┌─────────┐    ┌──────────────┐            │
│  │  agent  │───▶│  tools  │───▶│permission_ask│            │
│  └─────────┘    └─────────┘    └──────────────┘            │
│       │              │                                       │
│       ▼              │                                       │
│  ┌─────────┐         │                                       │
│  │compress │◀────────┘                                       │
│  └─────────┘                                                 │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌───────────────┐   ┌─────────────────┐   ┌─────────────────┐
│  LLM Layer    │   │   Tool Layer    │   │ Permission Layer│
│ (模型调用层)   │   │   (工具执行层)   │   │  (权限控制层)    │
│ ┌───────────┐ │   │ ┌─────────────┐ │   │ ┌─────────────┐ │
│ │MockLLM    │ │   │ │ shell       │ │   │ │ Rule Engine │ │
│ │OpenAI     │ │   │ │ filesystem  │ │   │ │ Regex Match │ │
│ │Anthropic  │ │   │ │ ask         │ │   │ │ Ask/Deny    │ │
│ └───────────┘ │   │ │ subagent    │ │   │ └─────────────┘ │
└───────────────┘   │ └─────────────┘ │   └─────────────────┘
                    │ ┌─────────────┐ │
                    │ │ memory_update││
                    │ └─────────────┘ │
                    └─────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Persistence Layer                          │
│                   (持久化存储层)                              │
│  ┌─────────────────────┐    ┌─────────────────────┐         │
│  │  SQLiteCheckpointer │    │    SQLiteStore      │         │
│  │  (会话状态检查点)     │    │  (归档数据存储)      │         │
│  └─────────────────────┘    └─────────────────────┘         │
│           │                        │                         │
│           └────────────┬───────────┘                         │
│                        ▼                                     │
│            ┌───────────────────────┐                         │
│            │  conversation.db      │                         │
│            │  (SQLite 数据库文件)    │                         │
│            └───────────────────────┘                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Configuration Layer                        │
│                   (配置管理层)                                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │user.yaml │  │provider.yaml│ │agent.yaml │ │permissions│   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘    │
└─────────────────────────────────────────────────────────────┘
```

## 组件交互流程

### 1. 用户消息处理流程

```
用户输入 → Input Handler → 构建 AgentState → LangGraph 执行
                                              │
                                              ▼
                                    ┌─────────────────┐
                                    │   agent 节点     │
                                    │  - 调用 LLM      │
                                    │  - 生成响应/工具调用│
                                    └─────────────────┘
                                              │
                              ┌───────────────┼───────────────┐
                              │               │               │
                              ▼               ▼               ▼
                        直接回复          需要工具调用      需要压缩
                              │               │               │
                              │               ▼               │
                              │      ┌─────────────────┐      │
                              │      │   tools 节点     │      │
                              │      │  - 权限检查      │      │
                              │      │  - 执行工具      │      │
                              │      └─────────────────┘      │
                              │               │               │
                              │      ┌────────┴────────┐      │
                              │      │                 │      │
                              │      ▼                 ▼      │
                              │  允许执行          需要询问    │
                              │      │                 │      │
                              │      │                 ▼      │
                              │      │     ┌─────────────────┐│
                              │      │     │permission_ask 节点││
                              │      │     │  - interrupt    ││
                              │      │     │  - 等待用户响应  ││
                              │      │     └─────────────────┘│
                              │      │               │        │
                              │      └───────────────┼────────┘
                              │                      │
                              ▼                      ▼
                        Stream Processor ←───────────┘
                              │
                              ▼
                          输出到终端
```

### 2. 工具调用与权限检查流程

```
工具调用请求
      │
      ▼
┌─────────────────┐
│  PermissionSystem│
│  .check()       │
└─────────────────┘
      │
      ├──────────────┬──────────────┐
      │              │              │
      ▼              ▼              ▼
   allow          deny           ask
      │              │              │
      ▼              │              ▼
执行工具         返回错误     设置 pending_permission_request
      │                         │
      ▼                         ▼
返回结果                    interrupt
      │                         │
      └────────────┬────────────┘
                   │
                   ▼
            路由回 agent 节点
```

### 3. 子代理创建与执行流程（Attach 模式）

```
主代理调用 subagent_create_attach
                │
                ▼
        动态构建子图
                │
                ▼
        初始化子代理状态
                │
                ▼
        ┌───────────────┐
        │ subagent 节点  │
        │  - ainvoke    │
        │  - 阻塞等待    │
        └───────────────┘
                │
                ▼
        子代理执行完成
                │
                ▼
        返回结果给主代理
                │
                ▼
        主代理继续执行
```

### 4. 上下文压缩流程

```
agent 节点调用 LLM 前
        │
        ▼
计算当前 Token 数
        │
        ├──────────────────┐
        │                  │
   < 阈值            ≥ 阈值
        │                  │
        │                  ▼
        │         设置 compression_pending
        │                  │
        │                  ▼
        │         route_after_agent 检测
        │                  │
        │                  ▼
        │         跳转到 compress 节点
        │                  │
        │                  ▼
        │         ┌─────────────────┐
        │         │  compress 节点   │
        │         │  - 生成摘要      │
        │         │  - 归档旧消息    │
        │         │  - 替换消息块    │
        │         └─────────────────┘
        │                  │
        │                  ▼
        └──────────←───────┘
                   │
                   ▼
            返回 agent 节点继续
```

## 数据流设计

### 1. AgentState 数据流

```python
class AgentState(MessagesState):
    # 只读数据（从配置加载）
    user_context: UserContext
    
    # 可变状态（运行时更新）
    messages: List[Message]           # 对话历史
    pending_permission_request: dict  # 权限询问暂存
    compression_pending: bool         # 压缩标记
    active_subagents: List[str]       # 活跃子代理
    output_events: List[OutputEvent]  # 输出事件队列
```

**数据流向：**

- **流入**: 用户输入 → HumanMessage → messages
- **流出**: AIMessage / ToolMessage → messages → Stream Processor → 终端
- **内部流转**: 各节点通过读取/修改 state 传递信息

### 2. 持久化数据流

```
运行时状态 (AgentState)
        │
        │ checkpoint 保存
        ▼
┌─────────────────┐
│ Checkpointer    │
│ put(thread_id,  │
│     checkpoint) │
└─────────────────┘
        │
        │ SQL INSERT/UPDATE
        ▼
┌─────────────────┐
│ checkpoints 表   │
│ - thread_id     │
│ - checkpoint_id │
│ - checkpoint    │
│ - metadata      │
└─────────────────┘

工具执行结果/归档数据
        │
        │ store.put()
        ▼
┌─────────────────┐
│ Store           │
│ put(namespace,  │
│     key, value) │
└─────────────────┘
        │
        │ SQL INSERT
        ▼
┌─────────────────┐
│ store 表         │
│ - namespace     │
│ - key           │
│ - value         │
└─────────────────┘
```

## LangGraph 状态图详解

### 状态图定义

```python
from langgraph.graph import StateGraph, START, END

def build_agent_graph(...):
    builder = StateGraph(AgentState)
    
    # 添加节点
    builder.add_node("agent", make_agent_node(llm, tools))
    builder.add_node("tools", make_tool_node(tools, permission_system))
    builder.add_node("permission_ask", make_permission_ask_node())
    builder.add_node("compress", make_compress_node(llm))
    
    # 添加边
    builder.add_edge(START, "agent")
    
    # 条件边：agent 之后去哪里？
    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        {
            "tools": "tools",
            "compress": "compress",
            "end": END
        }
    )
    
    # 条件边：tools 之后去哪里？
    builder.add_conditional_edges(
        "tools",
        route_after_tools,
        {
            "agent": "agent",
            "permission_ask": "permission_ask"
        }
    )
    
    # 固定边
    builder.add_edge("permission_ask", "tools")  # 批准后重新执行
    builder.add_edge("compress", "agent")        # 压缩后继续对话
    
    return builder.compile(checkpointer=checkpointer, store=store)
```

### 节点职责

| 节点 | 职责 | 输入 | 输出 |
|------|------|------|------|
| **agent** | 调用 LLM 生成响应或工具调用 | messages, user_context | AIMessage (含 tool_calls) |
| **tools** | 执行工具调用，处理权限 | AIMessage with tool_calls | ToolMessage(s) |
| **permission_ask** | 中断并等待用户授权 | pending_permission_request | 用户授权结果 |
| **compress** | 压缩上下文，生成摘要 | messages (过长时) | 压缩后的 messages |

### 路由逻辑

```python
def route_after_agent(state: AgentState) -> str:
    """决定 agent 节点之后的执行路径"""
    last_msg = state["messages"][-1]
    
    # 有工具调用 → 去 tools 节点
    if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
        return "tools"
    
    # 需要压缩 → 去 compress 节点
    if state.get("compression_pending"):
        return "compress"
    
    # 否则结束
    return "end"

def route_after_tools(state: AgentState) -> str:
    """决定 tools 节点之后的执行路径"""
    # 有待处理的权限询问 → 去 permission_ask
    if state.get("pending_permission_request"):
        return "permission_ask"
    
    # 否则返回 agent 继续对话
    return "agent"
```

## 关键设计决策

### 1. 为什么使用 LangGraph？

- **状态管理**: 内置的状态管理机制简化了复杂对话流程的实现
- **可中断性**: 原生支持 interrupt，便于实现权限询问等人机交互
- **检查点**: 自动保存执行状态，支持断点续传和重启恢复
- **可扩展性**: 易于添加新节点和自定义路由逻辑

### 2. 为什么选择 SQLite？

- **轻量级**: 无需额外服务，单文件数据库
- **事务支持**: ACID 特性保证数据一致性
- **成熟稳定**: 广泛使用，性能可靠
- **易于备份**: 单文件便于备份和迁移

### 3. 权限系统设计原则

- **最小权限**: 默认拒绝，显式允许
- **细粒度控制**: 支持工具级别、参数级别的约束
- **人机协同**: 不确定时询问用户，而非直接拒绝
- **可审计**: 所有权限决策记录在案

### 4. 上下文压缩策略

- **预防性压缩**: 在达到上限前（80%）触发，避免截断
- **增量压缩**: 仅压缩自上次检查点以来的消息
- **可追溯**: 保留压缩摘要，必要时可还原详情
