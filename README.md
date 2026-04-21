# Digital Human Agent

A single-user local digital human agent system built with LangGraph and SQLite.

## Features

- **LangGraph-based ReAct Loop**: Full agent reasoning cycle with tool use
- **SQLite Persistence**: Session state and data storage in `./data/`
- **Fine-grained Permissions**: Tool parameter regex constraints, target restrictions
- **Sub-agents**: Attach (wait for result) and Detach (independent) modes
- **Context Compression**: Automatic summarization when token limit approached
- **Skill System**: Anthropic Skills protocol support
- **Streaming Output**: Real-time response streaming to terminal

## Directory Structure

```
./
├── main.py                    # Entry point
├── pyproject.toml             # Project metadata and dependencies (uv)
├── README.md                  # This file
├── data/
│   ├── config/
│   │   ├── provider.yaml      # LLM Provider config
│   │   ├── agent.yaml         # Agent base config
│   │   ├── permissions.json   # Permission rules
│   │   ├── personality_template.md
│   │   └── user.yaml          # User metadata
│   ├── sessions/
│   │   └── default/
│   │       ├── conversation.db    # SQLite database
│   │       ├── workspace/         # Agent working directory
│   │       ├── cache/             # Tool result cache
│   │       └── subagents/         # Sub-agent workspaces
│   └── personality/
│       └── default/
│           ├── AGENT.md           # System prompt
│           ├── MEMORY.md          # Long-term memory
│           ├── agent.yaml         # Personality-specific config
│           ├── permissions.json   # Personality-specific permissions
│           ├── jobs.json          # Cron jobs
│           └── skills/            # Personality-specific skills
└── src/
    ├── __init__.py
    ├── models.py              # Pydantic models
    ├── config.py              # Configuration loading
    ├── permissions.py         # Permission system
    ├── tools.py               # Built-in tools
    ├── skills.py              # Skill loader
    ├── llm.py                 # LLM factory
    ├── graph.py               # LangGraph state graph
    └── checkpointer.py        # SQLite persistence
```

## Installation

### Using uv (Recommended)

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and setup
git clone <repository-url>
cd <project-directory>

# Sync dependencies
uv sync            # Production dependencies
uv sync --all-extras  # Include dev dependencies (pytest, etc.)
```

### Using pip

```bash
pip install -r requirements.txt
```

## Configuration

### 1. Set up LLM Provider

Edit `data/config/provider.yaml`:

```yaml
name: "minimax"
type: "anthropic"
base_url: "https://api.minimax.com/anthropic"
api_key: "${ANTHROPIC_API_KEY}"
model: "Minimax-M2.7"
max_concurrent: 2
```

Set the environment variable:

```bash
export ANTHROPIC_API_KEY="your-api-key-here"
```

### 2. Configure User

Edit `data/config/user.yaml`:

```yaml
user_id: "local_user"
user_name: "Alice"
platform: "local"
session_type: "private"
```

### 3. Set Permissions

Edit `data/config/permissions.json` or `data/personality/default/permissions.json`:

```json
{
  "default": "ask",
  "allow": [
    {"tool": "shell", "params": {"command": "^(ls|cat|pwd)$"}}
  ],
  "deny": [
    {"tool": "shell", "params": {"command": "^(rm|sudo).*$"}}
  ]
}
```

## Usage

```bash
python main.py
```

Then interact with the agent:

```
==================================================
Digital Human Agent
==================================================

Loading configuration...
  User: Alice (local_user)
  Agent: default
  Provider: minimax (Minimax-M2.7)

Initializing components...
  Enabled tools: ['shell', 'filesystem_read', ...]
  Database: ./data/sessions/default/conversation.db
Building agent graph...

==================================================
Agent ready! Type your message (or /exit to quit)
==================================================

You: List files in the workspace
[Agent uses filesystem_list tool]
f test.txt
d projects

You: What's in test.txt?
[Agent reads and displays content]

You: /exit
Goodbye!
```

## Available Tools

| Tool                 | Description                                        |
| -------------------- | -------------------------------------------------- |
| `shell`            | Execute shell commands (restricted by permissions) |
| `filesystem_read`  | Read file contents                                 |
| `filesystem_write` | Write to files                                     |
| `filesystem_list`  | List directory contents                            |
| `ask`              | Ask user questions                                 |
| `message_send`     | Send messages to user                              |
| `memory_update`    | Update long-term memory                            |
| `subagent_create`  | Create sub-agent                                   |
| `subagent_wait`    | Wait for sub-agent                                 |
| `subagent_list`    | List active sub-agents                             |
| `subagent_stop`    | Stop sub-agent                                     |
| `compact`          | Trigger context compression                        |
| `skill_load`       | Load skill definition                              |

## Architecture

### State Graph

```
START → agent → tools → agent → END
                ↓
          permission_ask → tools
                ↓
            compress → agent
```

### Nodes

1. **agent**: Calls LLM with tools bound
2. **tools**: Executes tool calls with permission checking
3. **permission_ask**: Interrupts for user approval
4. **compress**: Summarizes old messages

### Persistence

- **Checkpointer**: Saves conversation checkpoints to SQLite
- **Store**: Archives compressed messages and sub-agent data

## License

MIT
