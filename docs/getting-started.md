# 快速开始

本指南帮助你在本地运行 XBot Hermes。

Hermes 当前处于早期开发阶段：主循环、LangGraph、权限检查和基本工具已可运行；subagent、mailbox、上下文树、工具结果 cache 等能力仍在设计和实现中。

## 环境要求

- Python 3.10+
- uv
- 可用的 OpenAI 或 Anthropic 兼容模型服务

## 安装

```bash
uv sync
uv sync --all-extras
```

当前仓库不包含 `requirements.txt`，推荐使用 `uv` 和 `pyproject.toml` 管理依赖。

## 配置 Provider

编辑 `data/config/provider.yaml`：

```yaml
name: "minimax"
type: "anthropic"
base_url: "https://api.minimaxi.com/anthropic"
api_key: "${ANTHROPIC_API_KEY}"
model: "Minimax-M2.7"
max_concurrent: 2
```

设置环境变量：

```bash
export ANTHROPIC_API_KEY="your-api-key"
```

`type` 当前支持：

- `anthropic`
- `openai`

## 配置用户

编辑 `data/config/user.yaml`：

```yaml
user_id: "local_user"
user_name: "Alice"
platform: "local"
session_type: "private"
```

## 配置 Agent

默认会优先读取 `data/personality/default/agent.yaml`，不存在时读取 `data/config/agent.yaml`。可通过 `--personality-id` 或 `XBOT_PERSONALITY_ID` 切换 personality。

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
  - message_send
  - memory_update
  - subagent_create
  - subagent_wait
  - subagent_list
  - subagent_stop
  - compact
  - skill_load
skills: []
```

注意：`tools` 字段会过滤暴露给模型的工具；`filesystem` 会展开为 `filesystem_read`、`filesystem_write`、`filesystem_list`。

## 配置权限

编辑 `data/personality/<personality_id>/permissions.json` 或 `data/config/permissions.json`：

```json
{
  "default": "ask",
  "ask_timeout": 60,
  "allow": [
    {"tool": "shell", "params": {"command": "^(ls|cat|pwd|echo)$"}},
    {"tool": "message_send", "params": {}}
  ],
  "deny": [
    {"tool": "shell", "params": {"command": "^(rm|sudo|chmod).*$"}}
  ]
}
```

当前匹配顺序是 `deny -> allow -> default`。避免写出同一操作既 allow 又 deny 的规则。

## 启动

```bash
python main.py
```

常用调试参数：

```bash
python main.py --print-tools
python main.py --print-thoughts
```

启动后输入消息，使用 `/exit` 退出。

## 当前运行特征

- Runtime 默认使用 `InMemorySaver` 和 `InMemoryStore`。
- sandbox 开启时，`shell` 在 bubblewrap 内执行；sandbox 关闭时，`shell` 不可用。
- `filesystem_*` 会按 sandbox 或 legacy workspace 边界访问本地文件。
- 权限策略为 `ask` 时，会通过 interrupt/resume 请求用户确认。
- `ask` 已接入 interrupt/resume 的基础流程。
- `subagent_*` 当前是 P0 记录/状态实现，还不是完整异步 agent。

## 验证安装

可以先尝试：

```text
Alice> 请列出 workspace 文件
```

如果模型请求调用 `filesystem_list`，并且权限允许或用户确认，终端会显示工具结果和模型回复。

## 下一步阅读

- [架构与设计](./architecture.md)
- [配置参考](./configuration.md)
- [测试指南](./testing.md)
