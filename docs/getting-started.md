# 快速开始

本指南将帮助您快速安装并运行单用户本地数字人 Agent 系统。

## 环境要求

### 系统要求

- **操作系统**: Linux / macOS / Windows (WSL 推荐)
- **Python**: ≥ 3.10
- **内存**: 最低 2GB，推荐 4GB+
- **磁盘**: 至少 500MB 可用空间

### 依赖项

- Python 包管理工具：`pip` 或 `poetry`
- Git（用于克隆仓库）

## 安装步骤

### 1. 克隆仓库（如适用）

```bash
git clone <repository-url>
cd <project-directory>
```

### 2. 创建虚拟环境（推荐）

```bash
# 使用 uv（推荐）
uv venv
source .venv/bin/activate  # Linux/macOS
# 或
.venv\Scripts\activate     # Windows

# 或使用 venv
python -m venv .venv
source .venv/bin/activate  # Linux/macOS

# 或使用 conda
conda create -n agent python=3.10
conda activate agent
```

### 3. 安装依赖

**使用 uv（推荐）：**

```bash
# 安装 uv（如果尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 同步依赖
uv sync            # 安装生产依赖
uv sync --all-extras  # 包含开发依赖（pytest 等）
```

**使用 pip：**

```bash
pip install -r requirements.txt
```

**主要依赖包括：**

- `langgraph` ≥ 1.0 - Agent 框架
- `langchain-openai` / `langchain-anthropic` - LLM 集成
- `aiosqlite` - SQLite 异步驱动
- `pydantic` ≥ 2.0 - 数据校验
- `pyyaml` - 配置文件解析
- `tiktoken` - Token 计数

### 4. 初始化目录结构

```bash
# 创建必要的目录
mkdir -p data/config
mkdir -p data/sessions/default/{workspace,cache,subagents}
mkdir -p data/personality/default/skills
mkdir -p data/skills
```

系统首次运行时会自动创建这些目录，但预先创建可以方便您编辑配置文件。

## 配置说明

### 1. 用户元信息配置

编辑 `data/config/user.yaml`：

```yaml
user_id: "local_user"
user_name: "Alice"
platform: "local"
session_type: "private"
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `user_id` | 用户唯一标识 | 必填 |
| `user_name` | 用户显示名称 | 必填 |
| `platform` | 平台类型 | `"local"` |
| `session_type` | 会话类型 | `"private"` |

### 2. LLM Provider 配置

编辑 `data/config/provider.yaml`：

```yaml
name: "minimax"
type: "anthropic"  # 或 "openai"
api_key: "${MINIMAX_API_KEY}"  # 支持环境变量
model: "Minimax-M2.7"
max_concurrent: 2
```

**支持的 Provider 类型：**

- `anthropic` - Anthropic API (Claude)
- `openai` - OpenAI API (GPT)
- 兼容 OpenAI 接口的服务（如 Minimax、Moonshot 等）

**设置 API 密钥：**

```bash
# 方式 1：环境变量（推荐）
export MINIMAX_API_KEY="your-api-key-here"

# 方式 2：直接在配置文件中填写（不推荐用于生产环境）
api_key: "sk-..."
```

### 3. Agent 基础配置

编辑 `data/config/agent.yaml`：

```yaml
name: "default"
provider: "minimax"
agent_role: "A helpful assistant"
max_context_tokens: 8000
include_reasoning: false
tools:
  - shell
  - filesystem
  - ask
  - subagent_create
  - subagent_wait
skills: []
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `name` | Agent 名称 | `"default"` |
| `provider` | 使用的 LLM provider | 必填 |
| `agent_role` | Agent 角色描述 | `"A helpful assistant"` |
| `max_context_tokens` | 最大上下文 token 数 | `8000` |
| `include_reasoning` | 是否包含思考内容 | `false` |
| `tools` | 启用的工具列表 | 见上 |
| `skills` | 加载的 Skills | `[]` |

### 4. 权限规则配置

编辑 `data/config/permissions.json`：

```json
{
  "default": "ask",
  "ask_timeout": 60,
  "allow": [
    {"tool": "shell", "params": {"command": "^(ls|cat|pwd)$"}},
    {"tool": "filesystem", "params": {"action": "read"}}
  ],
  "deny": [
    {"tool": "shell", "params": {"command": "^rm\\s+-rf"}}
  ]
}
```

**权限策略：**

- `allow` - 直接允许
- `deny` - 直接拒绝
- `ask` - 询问用户（默认）

详见 [权限系统文档](./permissions.md)。

### 5. 人格配置（可选）

编辑 `data/personality/default/AGENT.md`：

```markdown
# Agent Personality

You are a helpful, harmless, and honest assistant.

## User Information
- Name: {{user_name}}
- Platform: {{platform}}

## Capabilities
- You can execute shell commands (with permission)
- You can read/write files in the workspace
- You can create subagents for parallel tasks

## Style
- Be concise and clear
- Ask for clarification when needed
- Explain your reasoning when making important decisions
```

## 首次运行指南

### 1. 启动 Agent

```bash
python main.py
```

您将看到类似输出：

```
Agent ready. Type your message (or /exit).
You: 
```

### 2. 基本交互

**发送消息：**

```
You: 你好，请介绍一下你自己
Agent: 你好！我是一个本地数字人助手...
```

**执行命令：**

```
You: 列出当前目录的文件
[Permission Ask] Shell command 'ls' requires permission. Allow?
Your response (yes/no): yes
Agent: 文件列表如下：...
```

**退出程序：**

```
You: /exit
Goodbye!
```

### 3. 验证安装

运行以下命令验证系统正常工作：

```
You: 请执行一个简单的测试命令：pwd
[Permission Ask] Shell command 'pwd' requires permission. Allow?
Your response (yes/no): yes
Agent: 当前工作目录是：/workspace/data/sessions/default/workspace
```

### 4. 检查持久化

运行后检查数据库文件是否生成：

```bash
ls -la data/sessions/default/conversation.db
# 应该能看到一个 SQLite 数据库文件
```

## 常见问题

### Q: 遇到 "ModuleNotFoundError"

**解决：** 确保已激活虚拟环境并安装了依赖：

```bash
# 使用 uv
uv sync --all-extras

# 或使用 pip
source .venv/bin/activate
pip install -r requirements.txt
```

### Q: API 密钥错误

**解决：** 检查环境变量是否正确设置：

```bash
echo $MINIMAX_API_KEY  # Linux/macOS
echo %MINIMAX_API_KEY%  # Windows
```

### Q: 权限询问无响应

**解决：** 检查 `permissions.json` 配置，确保格式正确。可临时设置为全允许进行测试：

```json
{"default": "allow"}
```

### Q: 数据库锁定

**解决：** 关闭所有使用该数据库的程序，或删除锁文件：

```bash
rm data/sessions/default/conversation.db-shm
rm data/sessions/default/conversation.db-wal
```

## 下一步

- 阅读 [配置参考](./configuration.md) 了解详细配置选项
- 查看 [工具系统](./tools.md) 学习如何使用各种工具
- 探索 [Skill 系统](./skills.md) 扩展 Agent 能力
- 阅读 [测试指南](./testing.md) 学习如何编写测试

## 获取帮助

如遇到问题：

1. 查看 [故障排查](./troubleshooting.md)
2. 检查日志输出（如有）
3. 提交 Issue 并附上错误信息
