# 快速开始

本指南帮助你在本地运行 XBot Hermes。

Hermes 当前处于早期开发阶段：主循环、LangGraph、权限检查、文件化 agent state、Plan/DAG、工具结果 cache、上下文树、mailbox 和 attach-mode subagent 已可运行；异步 subagent runner 和 checkpoint 持久化仍在推进中。

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
name: "deepseek"
type: "openai"
base_url: "${DEEPSEEK_OPENAI_BASE_URL}"
api_key: "${DEEPSEEK_API_TOKEN}"
model: "deepseek-v4-flash"
max_concurrent: 2
```

设置环境变量：

```bash
export DEEPSEEK_API_TOKEN="your-api-key"
export DEEPSEEK_OPENAI_BASE_URL="https://your-deepseek-compatible-endpoint"
```

`type` 当前支持：

- `anthropic`
- `openai`
- `smoke`，仅用于本地端到端 smoke 测试，不发网络请求

## 配置用户

编辑 `data/config/user.yaml`：

```yaml
user_id: "local_user"
user_name: "Alice"
platform: "local"
session_type: "private"
```

## 配置 Personality

每个 personality 都是一个独立目录：`data/personalities/<personality_id>/`。可通过 `--personality-id` 或 `XBOT_PERSONALITY_ID` 切换 personality。

```yaml
# data/personalities/default/personality.yaml
name: "default"
provider: "deepseek"
agent_role: "A local code-focused assistant that makes small, auditable changes."
max_context_tokens: 8000
include_reasoning: false
tools:
  - filesystem
  - ask
  - message_send
skills: []
```

`instructions.md` 是 personality 指令，`memory.md` 是长期记忆，`permissions.json` 和 `sandbox.json` 是该 personality 的工具边界。`tools` 字段会过滤暴露给模型的工具；`filesystem` 会展开为 `filesystem_read`、`filesystem_write`、`filesystem_list`。

## 配置权限

编辑 `data/personalities/<personality_id>/permissions.json`：

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

- Runtime 默认使用 file-backed LangGraph checkpoint saver 和 `InMemoryStore`。
- sandbox 开启时，`shell` 在 bubblewrap 内执行；sandbox 关闭时，`shell` 不可用。
- `filesystem_*` 会按 sandbox 或 workspace 边界访问本地文件。
- 权限策略或 sandbox 资源策略为 `ask` 时，会通过一次合并的 `tool_confirm` interrupt/resume 请求用户确认。
- `ask` 已接入 interrupt/resume 的基础流程。
- `subagent_create(mode="attach")` 会在当前 session 下同步运行 child thread，并访问 main workspace；`detach` 仍是 pending record，等待后续后台 runner。
- `debug_analyze` 可检查当前 task 的 DAG、plan、state、context tree、mailbox 和 subagent manifest。

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
