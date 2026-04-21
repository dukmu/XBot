# 配置参考

本文档详细说明所有配置文件的结构、选项和最佳实践。

## 配置文件总览

| 文件 | 位置 | 用途 | 格式 |
|------|------|------|------|
| `user.yaml` | `data/config/` | 用户元信息 | YAML |
| `provider.yaml` | `data/config/` | LLM Provider 配置 | YAML |
| `agent.yaml` | `data/config/` | Agent 基础配置 | YAML |
| `permissions.json` | `data/config/` | 全局权限规则 | JSON |
| `personality_template.md` | `data/config/` | 人格模板 | Markdown |
| `agent.yaml` | `data/personality/{name}/` | 人格特定配置 | YAML |
| `permissions.json` | `data/personality/{name}/` | 人格特定权限 | JSON |
| `jobs.json` | `data/personality/{name}/` | 定时任务配置 | JSON |

---

## 用户元信息配置 (user.yaml)

### 文件位置

```
data/config/user.yaml
```

### 完整示例

```yaml
user_id: "local_user_001"
user_name: "Alice"
platform: "local"
session_type: "private"
timezone: "Asia/Shanghai"
language: "zh-CN"
```

### 字段说明

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `user_id` | string | ✅ | - | 用户唯一标识符 |
| `user_name` | string | ✅ | - | 用户显示名称 |
| `platform` | string | ❌ | `"local"` | 平台类型 (`local`, `web`, `mobile`) |
| `session_type` | string | ❌ | `"private"` | 会话类型 (`private`, `shared`) |
| `timezone` | string | ❌ | `"UTC"` | 时区设置 (IANA 格式) |
| `language` | string | ❌ | `"en-US"` | 首选语言 (BCP 47 格式) |

### 最佳实践

- `user_id` 应保持稳定，不要频繁更改
- `user_name` 可以包含中文等非 ASCII 字符
- 单用户场景下，这些配置在系统运行期间保持不变

---

## LLM Provider 配置 (provider.yaml)

### 文件位置

```
data/config/provider.yaml
```

### 完整示例

```yaml
# OpenAI 配置
name: "openai"
type: "openai"
api_key: "${OPENAI_API_KEY}"
model: "gpt-4o"
base_url: null
max_concurrent: 5
timeout: 60
retry_attempts: 3
temperature: 0.7
max_tokens: 4096

# Anthropic 配置（备选）
# name: "anthropic"
# type: "anthropic"
# api_key: "${ANTHROPIC_API_KEY}"
# model: "claude-3-5-sonnet-20241022"
# max_concurrent: 3
# timeout: 120

# Minimax 配置（兼容 OpenAI 接口）
# name: "minimax"
# type: "openai"
# api_key: "${MINIMAX_API_KEY}"
# model: "Minimax-M2.7"
# base_url: "https://api.minimax.chat/v1"
# max_concurrent: 2
```

### 字段说明

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `name` | string | ✅ | - | Provider 名称（用于日志和选择） |
| `type` | string | ✅ | - | Provider 类型 (`openai`, `anthropic`) |
| `api_key` | string | ✅ | - | API 密钥（支持 `${ENV_VAR}` 语法） |
| `model` | string | ✅ | - | 模型名称 |
| `base_url` | string | ❌ | `null` | API 基础 URL（用于兼容服务） |
| `max_concurrent` | int | ❌ | `1` | 最大并发请求数 |
| `timeout` | int | ❌ | `60` | 请求超时时间（秒） |
| `retry_attempts` | int | ❌ | `3` | 失败重试次数 |
| `temperature` | float | ❌ | `0.7` | 生成温度（0-2） |
| `max_tokens` | int | ❌ | `4096` | 最大输出 token 数 |

### 环境变量引用

使用 `${VAR_NAME}` 语法引用环境变量：

```yaml
api_key: "${MY_API_KEY}"
base_url: "${API_BASE_URL:-https://api.default.com}"  # 带默认值
```

### 支持的 Provider 类型

#### OpenAI 兼容

```yaml
type: "openai"
# 适用于：OpenAI, Minimax, Moonshot, ZeroOne, 等
```

#### Anthropic

```yaml
type: "anthropic"
# 适用于：Claude 系列模型
```

---

## Agent 基础配置 (agent.yaml)

### 文件位置

```
data/config/agent.yaml
```

### 完整示例

```yaml
name: "default"
provider: "openai"
agent_role: "A helpful and harmless assistant"
max_context_tokens: 8000
include_reasoning: false
tools:
  - shell
  - filesystem
  - ask
  - message_send
  - memory_update
  - subagent_create
  - subagent_wait
  - subagent_list
  - compact
skills: []
compression_threshold: 0.8
workspace_root: "./data/sessions/default/workspace"
```

### 字段说明

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `name` | string | ❌ | `"default"` | Agent 配置名称 |
| `provider` | string | ✅ | - | 引用的 provider 名称 |
| `agent_role` | string | ❌ | `"A helpful assistant"` | Agent 角色描述 |
| `max_context_tokens` | int | ❌ | `8000` | 最大上下文 token 数 |
| `include_reasoning` | bool | ❌ | `false` | 是否包含思考内容到上下文 |
| `tools` | list | ❌ | 见上 | 启用的工具列表 |
| `skills` | list | ❌ | `[]` | 加载的 Skill 列表 |
| `compression_threshold` | float | ❌ | `0.8` | 压缩触发阈值（0-1） |
| `workspace_root` | string | ❌ | 自动 | 工作区根目录 |

### 可用工具列表

| 工具名 | 说明 | 权限敏感 |
|--------|------|----------|
| `shell` | 执行 shell 命令 | ✅ |
| `filesystem` | 文件系统操作 | ✅ |
| `ask` | 向用户提问 | ❌ |
| `message_send` | 发送消息给用户 | ❌ |
| `memory_update` | 更新长期记忆 | ✅ |
| `subagent_create` | 创建子代理 | ✅ |
| `subagent_wait` | 等待子代理完成 | ❌ |
| `subagent_list` | 列出子代理 | ❌ |
| `compact` | 手动触发压缩 | ❌ |
| `skill_load` | 加载 Skill | ❌ |

---

## 权限规则配置 (permissions.json)

### 文件位置

```
data/config/permissions.json           # 全局规则
data/personality/{name}/permissions.json  # 人格特定规则
```

### 完整示例

```json
{
  "default": "ask",
  "ask_timeout": 60,
  "allow": [
    {
      "tool": "shell",
      "params": {
        "command": "^(ls|cat|pwd|echo)$"
      }
    },
    {
      "tool": "filesystem",
      "params": {
        "action": "^(read|list)$"
      }
    }
  ],
  "deny": [
    {
      "tool": "shell",
      "params": {
        "command": "^rm\\s+-rf"
      }
    },
    {
      "tool": "shell",
      "params": {
        "command": "^sudo"
      }
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `default` | string | ❌ | `"ask"` | 默认策略 (`allow`, `deny`, `ask`) |
| `ask_timeout` | int | ❌ | `60` | 询问超时时间（秒） |
| `allow` | array | ❌ | `[]` | 允许规则列表 |
| `deny` | array | ❌ | `[]` | 拒绝规则列表 |

### 规则结构

每个规则包含：

```json
{
  "tool": "工具名称",
  "params": {
    "参数名": "正则表达式"
  }
}
```

### 匹配逻辑

1. 按顺序检查 `allow` 规则，匹配则返回 `allow`
2. 按顺序检查 `deny` 规则，匹配则返回 `deny`
3. 都不匹配则返回 `default`

### 正则表达式示例

```json
// 允许安全命令
{"command": "^(ls|cat|pwd|head|tail|wc)$"}

// 允许读取特定目录
{"path": "^/workspace/data/.*$"}

// 拒绝危险操作
{"command": "^rm\\s+(-rf|--no-preserve-root)"}
{"command": "^chmod\\s+777"}
{"command": "^dd\\s+"}
```

---

## 人格模板 (personality_template.md)

### 文件位置

```
data/config/personality_template.md
```

### 完整示例

```markdown
# Agent Personality Template

## User Information
- **Name**: {{user_name}}
- **User ID**: {{user_id}}
- **Platform**: {{platform}}
- **Language**: {{language}}

## Agent Role
{{agent_role}}

## System Instructions

You are a helpful, harmless, and honest AI assistant. Follow these guidelines:

1. **Safety First**: Never perform actions that could harm the user's system or data.
2. **Ask for Permission**: When in doubt, ask the user before executing potentially dangerous operations.
3. **Be Transparent**: Explain your reasoning and what you're doing.
4. **Respect Privacy**: Do not access or share personal information unnecessarily.

## Available Tools

You have access to the following tools:
- Shell command execution (with permission)
- File system operations (within workspace)
- Subagent creation for parallel tasks
- Memory management

## Response Style

- Be concise but thorough
- Use clear and simple language
- Provide context when making important decisions
- Admit when you're uncertain

## Memory Context

{{memory_content}}

## Skills

{{skills_list}}
```

### 变量替换

| 变量 | 来源 |
|------|------|
| `{{user_name}}` | `user.yaml` |
| `{{user_id}}` | `user.yaml` |
| `{{platform}}` | `user.yaml` |
| `{{language}}` | `user.yaml` |
| `{{agent_role}}` | `agent.yaml` |
| `{{memory_content}}` | `MEMORY.md` |
| `{{skills_list}}` | 加载的 Skills |

---

## 人格特定配置

### 目录结构

```
data/personality/default/
├── agent.yaml           # 人格特定 Agent 配置
├── permissions.json     # 人格特定权限
├── jobs.json            # 定时任务
├── AGENT.md             # 系统提示词
├── MEMORY.md            # 长期记忆
└── skills/              # 人格专属 Skills
```

### agent.yaml (人格级)

覆盖全局 `agent.yaml` 的配置：

```yaml
agent_role: "You are a coding expert assistant"
tools:
  - shell
  - filesystem
  - ask
skills:
  - code_review
  - test_generation
```

### jobs.json (定时任务)

```json
{
  "jobs": [
    {
      "id": "daily_backup",
      "schedule": "0 2 * * *",
      "enabled": true,
      "prompt": "Perform daily backup of workspace files"
    },
    {
      "id": "weekly_cleanup",
      "schedule": "0 3 * * 0",
      "enabled": true,
      "prompt": "Clean up temporary files older than 7 days"
    }
  ]
}
```

#### Cron 表达式格式

```
* * * * *
│ │ │ │ │
│ │ │ │ └─ 星期 (0-6, 0=周日)
│ │ │ └─── 月份 (1-12)
│ │ └───── 日期 (1-31)
│ └─────── 小时 (0-23)
└───────── 分钟 (0-59)
```

### AGENT.md (系统提示词)

```markdown
# Assistant Identity

You are an AI assistant specialized in software development.

## Expertise Areas
- Python programming
- Shell scripting
- System administration
- Code review and best practices

## Working Style
- Write clean, well-documented code
- Test thoroughly before suggesting changes
- Explain trade-offs when multiple solutions exist
```

### MEMORY.md (长期记忆)

```markdown
# Long-term Memory

## User Preferences
- Prefers Python over other languages
- Uses VS Code as primary editor
- Works on macOS

## Project Context
- Current project: Personal automation scripts
- Main languages: Python, Bash
- Testing framework: pytest

## Important Notes
- Backup important files before modifications
- Prefer non-destructive operations
```

---

## 配置优先级

当多个配置文件存在冲突时，优先级如下（从高到低）：

1. **人格特定配置** (`data/personality/{name}/`)
2. **全局配置** (`data/config/`)
3. **默认值** (代码中定义)

---

## 配置验证

### 使用 Pydantic 验证

```python
from pydantic import ValidationError

try:
    config = AgentConfig.model_validate(yaml.safe_load(file))
except ValidationError as e:
    print(f"配置验证失败：{e}")
```

### 命令行验证工具

```bash
# 验证所有配置文件
python -m xbot.config validate

# 验证特定文件
python -m xbot.config validate data/config/agent.yaml
```

---

## 配置热重载

某些配置支持运行时重新加载：

| 配置 | 热重载 | 方式 |
|------|--------|------|
| `user.yaml` | ❌ | 需重启 |
| `provider.yaml` | ❌ | 需重启 |
| `agent.yaml` | ⚠️ | 部分生效 |
| `permissions.json` | ✅ | 自动检测 |
| `jobs.json` | ✅ | 自动检测 |

---

## 故障排查

### 常见问题

**Q: 配置不生效？**

A: 检查：
1. YAML/JSON 格式是否正确
2. 字段名称是否拼写正确
3. 配置优先级是否符合预期

**Q: 环境变量未解析？**

A: 确保：
1. 使用 `${VAR}` 语法
2. 环境变量已导出：`export VAR=value`
3. 重启应用使变更生效

**Q: 权限规则不匹配？**

A: 测试正则表达式：

```python
import re
pattern = r"^(ls|cat|pwd)$"
print(re.match(pattern, "ls"))  # 应该匹配
print(re.match(pattern, "pwd"))  # 应该匹配
print(re.match(pattern, "rm"))   # 不应匹配
```
