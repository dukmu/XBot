# 配置参考

本文档以当前代码中的 Pydantic model 为准，同时列出 Hermes 目标架构需要新增的规划配置。

## 配置加载顺序

当前配置入口在 `xbot/config.py`。

| 配置 | 当前路径 | 说明 |
|------|----------|------|
| 用户信息 | `data/config/user.yaml` | 加载为 `UserContext` |
| Provider | `data/config/provider.yaml` | 加载为 `ProviderConfig` |
| Agent | `data/personality/default/agent.yaml` 优先，否则 `data/config/agent.yaml` | 加载为 `AgentConfig` |
| 权限 | `data/personality/default/permissions.json` 优先，否则 `data/config/permissions.json` | 加载为 `PermissionConfig` |
| 沙箱 | `data/personality/default/sandbox.json` 优先，否则 `data/config/sandbox.json`，再否则使用 P0 保守默认 | 加载为 `SandboxConfig` |
| 人格模板 | `data/config/personality_template.md` | system prompt 模板 |
| Agent 指令 | `data/personality/default/AGENT.md` | 拼进 system prompt |
| 长期记忆 | `data/personality/default/MEMORY.md` | 拼进 system prompt |
| Skills | `data/skills/*/SKILL.md` 与 `data/personality/default/skills/*/SKILL.md` | 生成 skills 摘要 |

当前代码固定使用 `data/personality/default`，尚未根据配置动态选择 personality 名称。

## user.yaml

当前支持字段：

```yaml
user_id: "local_user"
user_name: "Alice"
platform: "local"
session_type: "private"
```

| 字段 | 类型 | 必填 | 默认值 |
|------|------|------|--------|
| `user_id` | string | 是 | 无 |
| `user_name` | string | 是 | 无 |
| `platform` | string | 否 | `local` |
| `session_type` | literal | 否 | `private` |

`session_type` 当前只允许 `private`。

规划字段：

| 字段 | 用途 |
|------|------|
| `timezone` | 构造 system state message |
| `language` | 默认回复语言和区域格式 |
| `workspace_profile` | 未来支持多个本地工作区 |

## provider.yaml

当前支持字段：

```yaml
name: "minimax"
type: "anthropic"
base_url: "https://api.minimaxi.com/anthropic"
api_key: "${ANTHROPIC_API_KEY}"
model: "Minimax-M2.7"
max_concurrent: 2
```

| 字段 | 类型 | 必填 | 默认值 |
|------|------|------|--------|
| `name` | string | 是 | 无 |
| `type` | `openai` 或 `anthropic` | 是 | 无 |
| `base_url` | string/null | 否 | `null` |
| `api_key` | string | 是 | 无 |
| `model` | string | 是 | 无 |
| `max_concurrent` | int | 否 | `1` |

环境变量语法：

```yaml
api_key: "${ANTHROPIC_API_KEY}"
```

当前只支持完整 `${VAR_NAME}` 替换，不支持 `${VAR_NAME:-default}`。

规划字段：

| 字段 | 用途 |
|------|------|
| `timeout` | 请求超时 |
| `retry_attempts` | 模型调用重试 |
| `temperature` | 采样温度 |
| `max_tokens` | 最大输出 token |
| `rate_limit` | 结合 `max_concurrent` 控制并发 |

## agent.yaml

当前支持字段：

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

| 字段 | 类型 | 必填 | 默认值 |
|------|------|------|--------|
| `name` | string | 否 | `default` |
| `provider` | string | 否 | `minimax` |
| `agent_role` | string | 否 | `A helpful assistant` |
| `max_context_tokens` | int | 否 | `8000` |
| `include_reasoning` | bool | 否 | `false` |
| `tools` | list[string] | 否 | `["shell", "filesystem", "ask"]` |
| `skills` | list[string] | 否 | `[]` |

重要限制：

- `tools` 会过滤暴露给模型的工具；`filesystem` 会展开为 `filesystem_read/write/list`。
- `include_reasoning` 当前主要影响输出/意图配置，尚未完整实现“是否把 think 放入下轮上下文”的上下文构造策略。
- `max_context_tokens` 尚未形成完整自动压缩触发链路。
- `sandbox` 开启后，未知工具默认拒绝；会碰宿主资源的工具必须走系统 sandbox 后端。

Hermes 规划字段：

```yaml
context:
  max_tokens: 8000
  compression_threshold: 0.85
  capture_think: false
  include_think_in_context: false
  include_system_state: true

runtime:
  persistence: inmemory  # inmemory | sqlite
  thread_id: default

cache:
  enabled: true
  max_inline_chars: 4000
  default_read_limit: 8000

subagents:
  enabled: true
  default_mode: sync
  default_context_policy: inherit_summary
  timeout_seconds: 300
  max_tool_calls: 30

mailbox:
  enabled: true
  include_unread_summary_in_context: true
```

这些字段是目标设计，当前代码尚未读取。

## sandbox.json

可选系统级沙箱配置，用来限制工具运行时能看到的宿主资源。

如果两个 `sandbox.json` 都不存在，P0 runtime 默认启用一个保守 sandbox：workspace/subagents 可写，personality/skills 只读，`MEMORY.md` 可写，其他宿主路径默认 deny。可以用 `XBOT_SANDBOX=disabled` 或 CLI `--no-sandbox` 显式关闭。

```json
{
  "enabled": true,
  "backend": "bubblewrap",
  "default": "deny",
  "network": false,
  "timeout_seconds": 30,
  "max_output_chars": 20000,
  "resources": [
    {"path": "sessions/default/workspace", "access": "readwrite", "recursive": true},
    {"path": "sessions/default/subagents", "access": "readwrite", "recursive": true},
    {"path": "personality/default", "access": "readonly", "recursive": true},
    {"path": "personality/default/MEMORY.md", "access": "readwrite", "recursive": false},
    {"path": "skills", "access": "readonly", "recursive": true},
    {"path": "personality/default/skills", "access": "readonly", "recursive": true}
  ]
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | Pydantic 默认 `false`，P0 runtime fallback 为 `true` | 是否启用系统级 sandbox |
| `backend` | string | `bubblewrap` | 当前仅支持 `bubblewrap` |
| `default` | `deny` / `ask` | `deny` | 未命中资源规则时的默认策略 |
| `network` | bool | `false` | 是否允许网络 namespace 共享 |
| `timeout_seconds` | int | `30` | 单次 sandbox 命令超时 |
| `max_output_chars` | int | `20000` | 输出截断上限 |
| `resources` | list[rule] | `[]` | 明确挂载的宿主资源 |

资源规则：

```json
{"path": "sessions/default/workspace", "access": "readwrite", "recursive": true}
```

`path` 相对 `data/` 解析，也支持绝对路径。`access` 支持：

- `readwrite`：可读写挂载。
- `readonly`：只读挂载。
- `deny`：在 sandbox 内遮蔽该路径；如果它位于可写父目录下，shell 子进程也看不到宿主内容。
- `ask`：默认按 deny 遮蔽；命中时通过 `sandbox_confirm` interrupt 复用 ask 流程，获批后只临时挂载本次工具调用的精确路径。

实现约束：

- 所有暴露给模型的工具都必须在 `TOOL_SANDBOX_MODE` 中声明为 `sandboxed` 或 `host`。启用 sandbox 后，未声明工具会在图构建阶段失败。
- `host` 表示 ask、message、cache、compact 等不直接接触宿主资源的控制面工具；这不是绕过注册，而是显式声明它不需要系统级子进程隔离。
- `shell` 在 sandbox 开启时真实执行，但只能看到 bubblewrap 挂载出的资源；脚本、子进程和命令替换都会继承同一个边界。

## permissions.json

当前支持字段：

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

| 字段 | 类型 | 默认值 |
|------|------|--------|
| `default` | `allow` / `deny` / `ask` | `ask` |
| `ask_timeout` | int | `60` |
| `allow` | list[rule] | `[]` |
| `deny` | list[rule] | `[]` |

规则结构：

```json
{
  "tool": "shell",
  "params": {
    "command": "^(ls|pwd)$"
  }
}
```

匹配顺序：

1. 先匹配 `deny`
2. 再匹配 `allow`
3. 未命中则使用 `default`

deny 优先可以避免危险操作被宽泛 allow 规则覆盖。配置时仍建议避免写出“同一操作既 allow 又 deny”的冲突规则。

规划改进：

- 支持显式 `ask` 规则列表。
- 支持风险等级，如 `low`、`medium`、`high`。
- 支持资源类型，如 file、shell、network、memory。
- 支持按工具声明的风险元数据生成更好的确认文案。

## personality_template.md

当前模板会在 `xbot/graph.py` 中用字符串替换填充。

当前支持变量：

```text
{{ user_context.user_id }}
{{ user_context.user_name }}
{{ user_context.platform }}
{{ user_context.session_type }}
{{ agent_config.agent_role }}
```

注意：当前代码里的 `agent_config.agent_role` 实际从 graph state 读取，默认值为 `A helpful assistant`。后续应把 `AgentConfig` 显式放入 state 或 context builder。

## AGENT.md 和 MEMORY.md

`data/personality/default/AGENT.md` 会作为 Agent Instructions 拼进 system prompt。

`data/personality/default/MEMORY.md` 会作为 Long-term Memory 拼进 system prompt。

Hermes 目标设计中，长期记忆应逐步拆为：

- 人类可读 Markdown。
- 结构化 facts。
- open threads。
- 工具结果引用。

## Skills

当前 skill 发现路径：

```text
data/skills/<skill_name>/SKILL.md
data/personality/default/skills/<skill_name>/SKILL.md
```

`get_skills_summary()` 只把 skill 名称和简短描述放入 system prompt。完整 skill 内容需要通过 `skill_load` 工具读取。

## 当前工具配置状态

`agent.yaml` 中的 `tools` 字段会过滤暴露给模型的工具。真实可用行为如下：

| 工具 | 当前行为 |
|------|----------|
| `shell` | sandbox 关闭时 mock；sandbox 开启时在 bubblewrap 内真实执行 |
| `filesystem_read` | 通过 sandbox/legacy workspace 边界读取 |
| `filesystem_write` | sandbox 关闭时 mock；sandbox 开启时通过 bubblewrap 写入 |
| `filesystem_list` | 通过 sandbox/legacy workspace 边界列目录 |
| `ask` | 触发 `user_ask` interrupt/resume |
| `message_send` | 通过 interaction adapter 发送用户可见消息 |
| `memory_update` | 追加写入 `MEMORY.md` |
| `subagent_create` | 创建 P0 subagent 记录和 workspace |
| `subagent_wait` | 读取 P0 subagent 状态和结果文件 |
| `subagent_list` | 列出 P0 subagent 记录 |
| `subagent_stop` | 将 P0 subagent 标记为 stopped |
| `compact` | 手动请求下一次图循环进行上下文压缩 |
| `skill_load` | 通过 sandbox/运行时资源边界读取 skill 文件 |

## 配置设计建议

短期建议：

- 不增加多 personality 动态加载，先稳定 `default`。
- 保持 `InMemorySaver/Store` 默认，避免 interrupt 和持久化同时复杂化。
- 将 `include_reasoning` 拆成 `capture_think` 和 `include_think_in_context`。
- 将 sandbox 资源配置拆成单独示例文件，降低启用成本。

中期建议：

- 增加 `context` 配置块，管理 token、压缩和 system state。
- 增加 `cache` 配置块，管理工具结果 hook。
- 增加 `subagents` 配置块，限制后台任务资源。
- 增加 `mailbox` 配置块，控制异步事件进入上下文的方式。
