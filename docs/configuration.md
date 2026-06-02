# 配置参考

XBot 的本地配置现在只有两个顶层概念：

```text
data/
  config/                    # 用户与 provider 等全局配置
  personalities/<id>/        # 某个 agent personality 的完整配置
  sessions/<id>/             # 某次隔离运行的 workspace/cache/state/saver
```

不再读取旧的 `data/personality/`、`AGENT.md`、`MEMORY.md`、`person.yaml` 或 `data/config/agent.yaml`。

`session_id` 是一次隔离运行的目录命名空间；`personality_id` 选择 agent 配置；`thread_id` 只作为 LangGraph checkpoint 的线程键；`task_id` 是 DAG state 主体标识，主 agent 固定为 `agent`，subagent 使用自己的 id。

## 加载顺序

| 配置 | 路径 | 说明 |
|------|------|------|
| 用户信息 | `data/config/user.yaml` | 加载为 `UserContext` |
| Provider | `data/config/provider.yaml` | 加载为 `ProviderConfig` |
| System template | `data/config/system_template.md` | system prompt 模板 |
| Personality | `data/personalities/<personality_id>/personality.yaml` | 加载为 `AgentConfig` |
| Instructions | `data/personalities/<personality_id>/instructions.md` | 拼进 system prompt |
| Memory | `data/personalities/<personality_id>/memory.md` | 拼进 system prompt，可由 `memory_update` 追加 |
| Permissions | `data/personalities/<personality_id>/permissions.json` | 加载为 `PermissionConfig` |
| Sandbox | `data/personalities/<personality_id>/sandbox.json` | 加载为 `SandboxConfig`，不存在时使用保守默认 |
| Skills | `data/skills/*/SKILL.md` 与 `data/personalities/<personality_id>/skills/*/SKILL.md` | 生成 skills 摘要 |

CLI 支持通过 `--session-id` / `XBOT_SESSION_ID` 和 `--personality-id` / `XBOT_PERSONALITY_ID` 选择本地 session 与 personality。

## user.yaml

```yaml
user_id: "local_user"
user_name: "Alice"
platform: "local"
session_type: "private"
```

## provider.yaml

DeepSeek 使用 OpenAI-compatible API：

```yaml
name: "deepseek"
type: "openai"
base_url: "${DEEPSEEK_OPENAI_BASE_URL}"
api_key: "${DEEPSEEK_API_TOKEN}"
model: "deepseek-v4-flash"
max_concurrent: 2
```

`type` 支持 `anthropic`、`openai` 和 `smoke`。`smoke` 只用于本地端到端测试，不访问网络。

## personalities/<id>/personality.yaml

```yaml
name: "default"
provider: "deepseek"
agent_role: "A local code-focused assistant that makes small, auditable changes."
max_context_tokens: 8000
include_reasoning: false
tools:
  - shell
  - filesystem
  - ask
  - message_send
  - memory_update
  - task_begin
  - task_status
  - task_exit
  - plan_autofill
  - plan_next
  - plan_update
  - summary_add
  - summary_list
  - summary_read
  - claim_add
  - claim_list
  - compact
  - skill_load
skills: []
```

`filesystem` 会展开为 `filesystem_read`、`filesystem_write`、`filesystem_list`。

## instructions.md 和 memory.md

`instructions.md` 是 personality 的长期行为指令。`memory.md` 是长期记忆。

```text
data/personalities/default/
  personality.yaml
  instructions.md
  memory.md
  permissions.json
  sandbox.json
  skills/
```

## permissions.json

```json
{
  "default": "ask",
  "ask_timeout": 60,
  "allow": [
    {"tool": "filesystem.*", "params": {}},
    {"tool": "message_send", "params": {}},
    {"tool": "compact", "params": {}}
  ],
  "deny": [
    {"tool": "shell", "params": {"command": "^(rm|sudo|chmod|chown|git reset).*$"}}
  ],
  "ask": [
    {"tool": "shell"}
  ]
}
```

匹配顺序是 `deny -> allow -> ask -> default`。

## sandbox.json

如果 personality 没有 `sandbox.json`，runtime 会生成保守默认：当前 session 的 workspace/subagents 可写，tasks 和 personality 只读，`memory.md` 可写，其他路径默认 deny。

```json
{
  "enabled": true,
  "backend": "bubblewrap",
  "default": "deny",
  "network": false,
  "timeout_seconds": 30,
  "max_output_chars": 20000,
  "resources": [
    {"path": "sessions/<session_id>/workspace", "access": "readwrite", "recursive": true},
    {"path": "sessions/<session_id>/subagents", "access": "readwrite", "recursive": true},
    {"path": "sessions/<session_id>/state", "access": "readonly", "recursive": true},
    {"path": "personalities/<personality_id>", "access": "readonly", "recursive": true},
    {"path": "personalities/<personality_id>/memory.md", "access": "readwrite", "recursive": false},
    {"path": "skills", "access": "readonly", "recursive": true},
    {"path": "personalities/<personality_id>/skills", "access": "readonly", "recursive": true}
  ]
}
```

## 可审计运行产物

每次 `HermesInteraction.create()` 会在 `data/sessions/<session_id>/state/` 创建主 agent DAG 状态；LangGraph saver 位于 `data/sessions/<session_id>/saver/`。Attach-mode subagent 使用自己的 `data/sessions/<session_id>/subagents/<subagent_id>/state/` 和 `saver/`：

```text
task.yaml
goal.md
plan.yaml
events.jsonl
graph.jsonl
context_tree.jsonl
mailbox.jsonl
state.yaml
context.md
claims.yaml
artifacts/
checkpoints/
versions/
summaries/
locks/
```

`events.jsonl`、`graph.jsonl`、`context_tree.jsonl` 和 `mailbox.jsonl` 是 append-only source of truth；`state.yaml` 是 materialized view。`xbot.verification.verify_task_state()` 会校验任务文件、plan DAG、上下文树、mailbox、事件计数和 state 投影一致性。
