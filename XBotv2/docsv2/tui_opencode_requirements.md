# XBotv2 TUI 设计需求与设计文档（类 OpenCode 风格）

Status: 设计稿 v2。引入对 OpenCode 实际仓库与官方文档的逐项调研，所有设计选择都给出依据。
后续若有改动，先改本文，再动代码；代码 PR 引用章节号。

Last reviewed: 2026-06-05

---

## 0. 文档目标

为 XBotv2 的 Textual 协议 TUI（`xbotv2/tui/textual_client.py` 等）建立：

1. **可审查的需求基线** — 来自 OpenCode 官方文档/源码的设计参考，以及 XBotv2 自有的、明确偏离 OpenCode 的本地约束。
2. **可落地的设计规约** — 布局、视觉、组件、状态机、键位、协议映射、诊断、验证。
3. **可验收的完成定义（DoD）** — 每条要求都能在 SVG/PIL 截图、回放测试或真机手测中被确认。

非目标：

- 不重写 OpenCode 全部 UI 行为。
- 不重新设计协议；TUI 仍只读 JSONL `ProtocolFrame`。
- 不在 TUI 内引入核心运行时依赖（保持 `xbotv2/tui/*` 对核心/引擎/LangChain 的零导入边界）。
- 不引入 OpenCode 的"Bun + OpenTUI + Solid.js"栈；XBotv2 仍走 Python Textual，借鉴**思想**与**结构**，不照搬**代码**。

---

## 1. 术语

| 术语 | 含义 |
| --- | --- |
| Composer | 底部多行输入框（`ComposerTextArea`），承载用户文本输入。 |
| Stream | 主区域，按时间顺序渲染的对话/事件/选择/本地回执。 |
| Status bar | 顶部或底部一条单行状态栏，承载运行态/会话/Agent/排队/用量。 |
| Choice | 协议需要人决策时，由 TUI 渲染的内联选项（权限/沙箱/ask_user）。 |
| Live interaction | 服务端发出 `permission_request` / `user_input_required` 后，等待客户端回执的一段窗口期。 |
| Render log | 一份按时间排序的可见项列表（消息/工具/选择/回执/错误），UI 是它的投影。 |
| Trace | 通过 `XBOTV2_TUI_TRACE` 写入的 JSONL 诊断日志。 |
| Mode | TUI 的高层交互状态：`COMPOSING` / `RUNNING` / `CHOOSING` / `SUBMITTED` / `ERROR`。 |
| Provider | 在 OpenCode 语义中指 LLM 服务商；在 XBotv2 协议层叫 `provider_name`。 |
| Personality | XBotv2 的"人格/系统提示 + 工具集"封装，对应 OpenCode 的 agent 概念。 |
| Slash command | `/` 前缀的内联命令，OpenCode 与 XBotv2 都以此作为主要命令入口。 |
| Leader key | OpenCode 的 `ctrl+x` 修饰键；后续键按下触发"组合键"。XBotv2 v1 不引入 leader。 |
| Slot | OpenCode 的插件扩展点（如 `app_bottom`、`app`）；XBotv2 暂不引入。 |
| Keybind namespace | OpenCode 用 `app` / `app.global` / `app_exit` / `palette` 等做模式分组。 |

---

## 2. OpenCode 调研（第一手证据）

本节为后续所有设计选择的**唯一依据**。引用均来自官方文档或公开源码。

### 2.1 官方文档原文要点

来源：

- TUI 文档：`https://opencode.ai/docs/tui/`
- Keybinds 文档：`https://opencode.ai/docs/keybinds/`
- Permissions 文档：`https://opencode.ai/docs/permissions/`

#### 2.1.1 TUI 文档

- 入口：`opencode [project-path]`，对应当前工作目录。
- 文件引用：在消息中以 `@` 触发模糊文件名搜索，可读入文件内容。
- Shell 命令：以 `!` 开头的消息视作 shell 命令，输出作为 tool result 入栈。
- Slash 命令（完整列表与默认键位）：

  | Slash | 别名 | 键位（默认） | 行为 |
  | --- | --- | --- | --- |
  | `/connect` | — | — | 选择 provider 并填 API key |
  | `/compact` | `/summarize` | `<leader>c` | 压缩当前 session |
  | `/details` | — | — | 切换工具执行细节显示 |
  | `/editor` | — | `<leader>e` | 调 `$EDITOR` 写消息（支持 `code --wait` 等） |
  | `/exit` | `/quit`, `/q` | `<leader>q` | 退出 |
  | `/export` | — | `<leader>x` | 导出 Markdown 到默认编辑器 |
  | `/help` | — | — | 帮助对话框 |
  | `/init` | — | — | 引导生成/更新 `AGENTS.md` |
  | `/models` | — | `<leader>m` | 列出可选模型 |
  | `/new` | `/clear` | `<leader>n` | 新 session |
  | `/redo` | — | `<leader>r` | 重做（依赖 git） |
  | `/sessions` | `/resume`, `/continue` | `<leader>l` | 列出/切换 session |
  | `/share` | — | — | 分享 session |
  | `/themes` | — | `<leader>t` | 主题 |
  | `/thinking` | — | — | 切换"思考块"显示 |
  | `/undo` | — | `<leader>u` | 撤销（依赖 git） |
  | `/unshare` | — | — | 取消分享 |

- 配置位于 `tui.json`（或 `tui.jsonc`），schema：`https://opencode.ai/tui.json`。
  `tui.json` **与** `opencode.json`（服务端/运行期配置）是**两个文件**。
- `tui.json` 主要字段：
  - `theme`：主题名。
  - `keybinds`：与内置默认合并，只覆写需要的项。
  - `leader_timeout`：默认 `2000` ms。
  - `scroll_acceleration.enabled`：macOS 风格滚动加速；开启后覆盖 `scroll_speed`。
  - `scroll_speed`：默认 `3`（最小 0.001）。
  - `diff_style`：`"auto" \| "stacked"`。
  - `mouse`：默认 `true`；关闭则保留终端原生选择/滚动。
  - `attention.{enabled,notifications,sound,volume,sound_pack,sounds}`：通知 + 提示音；默认全关。
- 主题/命令可由命令面板（`<leader>q` 之外的另一快捷键，详见下文）持久化。
- 自定义示例：是否在消息里显示用户名（"username display"）。

#### 2.1.2 Keybinds 文档

- 默认前缀 `ctrl+x`（leader）。多数键位是 `<leader> + X`。
- **完整默认键位（节选至本文档需要用到的）**：

  | 命令 ID | 默认键 | 说明 |
  | --- | --- | --- |
  | `app_exit` | `ctrl+c,ctrl+d,<leader>q` | 退出 app |
  | `app_debug` | `none` | 默认未绑定 |
  | `app_console` | `none` | 默认未绑定 |
  | `app_heap_snapshot` | `none` | 默认未绑定 |
  | `app_toggle_animations` | `none` | 默认未绑定 |
  | `command_list` | `ctrl+p` | 命令面板（fuzzy 搜索命令） |
  | `help_show` | `none` | 默认未绑定 |
  | `docs_open` | `none` | 默认未绑定 |
  | `editor_open` | `<leader>e` | 调外部编辑器 |
  | `theme_list` | `<leader>t` | 主题列表 |
  | `theme_switch_mode` | `none` | 默认未绑定 |
  | `theme_mode_lock` | `none` | 默认未绑定 |
  | `sidebar_toggle` | `<leader>b` | 侧边栏 |
  | `scrollbar_toggle` | `none` | 默认未绑定 |
  | `status_view` | `<leader>s` | 状态视图（**全屏**） |
  | `session_export` | `<leader>x` | 导出 |
  | `session_copy` | `none` | 默认未绑定 |
  | `session_new` | `<leader>n` | 新 session |
  | `session_list` | `<leader>l` | session 列表 |
  | `session_timeline` | `<leader>g` | session 时间线 |
  | `session_fork` | `none` | 默认未绑定 |
  | `session_rename` | `ctrl+r` | 重命名 |
  | `session_delete` | `ctrl+d` | 删除 |
  | `session_share` | `none` | 默认未绑定 |
  | `session_unshare` | `none` | 默认未绑定 |
  | `session_interrupt` | `escape` | 中断当前 session |
  | `session_compact` | `<leader>c` | 压缩 |
  | `session_toggle_timestamps` | `none` | 默认未绑定 |
  | `session_toggle_generic_tool_output` | `none` | 默认未绑定 |
  | `session_child_first` | `<leader>down` | 子 session 跳转 |
  | `session_child_cycle` | `right` | 下一个子 session |
  | `session_child_cycle_reverse` | `left` | 上一个子 session |
  | `session_parent` | `up` | 父 session |
  | `model_provider_list` | `ctrl+a` | provider 列表 |
  | `model_favorite_toggle` | `ctrl+f` | 收藏 |
  | `model_list` | `<leader>m` | 模型列表 |
  | `model_cycle_recent` | `f2` | 最近模型 |
  | `model_cycle_recent_reverse` | `shift+f2` | 反向 |
  | `model_cycle_favorite` | `none` | 默认未绑定 |
  | `model_cycle_favorite_reverse` | `none` | 默认未绑定 |
  | `mcp_list` | `none` | 默认未绑定 |
  | `provider_connect` | `none` | 默认未绑定 |
  | `agent_list` | `<leader>a` | agent 列表 |
  | `agent_cycle` | `tab` | 下一个 agent |
  | `agent_cycle_reverse` | `shift+tab` | 上一个 agent |
  | `variant_cycle` | `ctrl+t` | 切换变体 |
  | `variant_list` | `none` | 默认未绑定 |
  | `messages_page_up` | `pageup,ctrl+alt+b` | 翻页 |
  | `messages_page_down` | `pagedown,ctrl+alt+f` | 翻页 |
  | `messages_line_up` | `ctrl+alt+y` | 单行 |
  | `messages_line_down` | `ctrl+alt+e` | 单行 |
  | `messages_half_page_up` | `ctrl+alt+u` | 半页 |
  | `messages_half_page_down` | `ctrl+alt+d` | 半页 |
  | `messages_first` | `ctrl+g,home` | 顶部 |
  | `messages_last` | `ctrl+alt+g,end` | 底部 |
  | `messages_next` | `none` | 默认未绑定 |
  | `messages_previous` | `none` | 默认未绑定 |
  | `messages_last_user` | `none` | 默认未绑定 |
  | `messages_copy` | `<leader>y` | 复制 |
  | `messages_undo` | `<leader>u` | 撤销 |
  | `messages_redo` | `<leader>r` | 重做 |
  | `messages_toggle_conceal` | `<leader>h` | 折叠/隐藏 |
  | `tool_details` | `none` | 默认未绑定 |
  | `display_thinking` | `none` | 默认未绑定 |
  | `prompt_submit` | `none` | 默认未绑定 |
  | `prompt_editor_context_clear` | `none` | 默认未绑定 |
  | `prompt_skills` | `none` | 默认未绑定 |
  | `prompt_stash` | `none` | 默认未绑定 |
  | `prompt_stash_pop` | `none` | 默认未绑定 |
  | `prompt_stash_list` | `none` | 默认未绑定 |
  | `workspace_set` | `none` | 默认未绑定 |
  | `input_clear` | `ctrl+c` | 清空输入 |
  | `input_paste` | `ctrl+v` | 粘贴（`preventDefault=false`） |
  | `input_submit` | `return` | 提交 |
  | `input_newline` | `shift+return,ctrl+return,alt+return,ctrl+j` | 换行 |
  | `input_move_left/right/up/down` | `left,ctrl+b` / `right,ctrl+f` / `up` / `down` | Emacs 风 |
  | `input_select_*` | `shift+...` | 选择 |
  | `input_line_home/end` | `ctrl+a` / `ctrl+e` | 行首/行尾 |
  | `input_visual_line_home/end` | `alt+a` / `alt+e` | 视觉行 |
  | `input_buffer_home/end` | `home` / `end` | buffer 端 |
  | `input_delete_line` | `ctrl+shift+d` | 整行 |
  | `input_delete_to_line_end` | `ctrl+k` | kill |
  | `input_delete_to_line_start` | `ctrl+u` | back-kill |
  | `input_backspace` | `backspace,shift+backspace` | 退格 |
  | `input_delete` | `ctrl+d,delete,shift+delete` | 删字符 |
  | `input_undo/redo` | `ctrl+-,super+z` / `ctrl+.,super+shift+z` | 撤销重做 |
  | `input_word_forward/backward` | `alt+f,alt+right,ctrl+right` / `alt+b,alt+left,ctrl+left` | 词 |
  | `input_delete_word_forward/backward` | `alt+d,alt+delete,ctrl+delete` / `ctrl+w,ctrl+backspace,alt+backspace` | 删词 |
  | `input_select_all` | `super+a` | 全选 |
  | `history_previous` | `up` | 历史 |
  | `history_next` | `down` | 历史 |
  | `dialog.select.prev/next/page_up/page_down/home/end/submit` | `up` / `down` / `pageup` / `pagedown` / `home` / `end` / `return` | 列表选择 |
  | `dialog.prompt.submit` | `return` | 弹窗里的文本确认 |
  | `dialog.mcp.toggle` | `space` |  |
  | `prompt.autocomplete.prev/next` | `up` / `down` | 补全 |
  | `prompt.autocomplete.hide` | `escape` |  |
  | `prompt.autocomplete.select` | `return` |  |
  | `prompt.autocomplete.complete` | `tab` |  |
  | `permission.prompt.fullscreen` | `ctrl+f` | 权限全屏 |
  | `plugins.toggle` | `space` | 插件 |
  | `dialog.plugins.install` | `shift+i` |  |
  | `terminal_suspend` | `ctrl+z` | （Windows 强制 `none`） |
  | `tips_toggle` | `<leader>h` |  |
  | `plugin_manager` / `plugin_install` | `none` |  |
  | `which_key_*` | `ctrl+alt+k` 系 | which-key 弹层 |

- 桌面 prompt 还提供一组"不可配置"的 Readline/Emacs 风格快捷键（已在表中体现为 `input_*`）。
- 字符串值可以是单键或逗号分隔的多个键；高级用法可以传对象 `{ key, event, preventDefault, fallthrough }`。
- 用 `none` 或 `false` 显式禁用某键位。
- Windows 特殊：`input_undo` 默认追加 `ctrl+z`；`terminal_suspend` 强制 `none`。
- 某些终端默认不把 `Shift+Enter` 当成带修饰键的 Enter，文档给出 Windows Terminal 的设置示例（`\u001b[13;2u`）。

#### 2.1.3 Permissions 文档

- 行为枚举：`"allow"` / `"ask"` / `"deny"`。
- 配置位置：服务端 `opencode.json` 中 `permission` 字段（**不是** `tui.json`）。
- 自 `v1.1.1`，旧 `tools` 布尔配置被并入 `permission`；`tools` 仍可读（旧兼容）。
- 通用 + 工具级覆写（"最后匹配规则胜出"）：

  ```json
  {
    "permission": {
      "*": "ask",
      "bash": "allow",
      "edit": "deny"
    }
  }
  ```

- 精细规则（对象语法）支持通配：

  ```json
  {
    "permission": {
      "bash": { "*": "ask", "git *": "allow", "rm *": "deny" },
      "edit": { "*": "deny", "packages/web/src/content/docs/*.mdx": "allow" }
    }
  }
  ```

- 通配规则：`*` 零或多字符；`?` 严格一字符；其它字面。
- 路径前缀支持 `~` / `$HOME` 展开。
- `external_directory`：访问工作目录外的路径必须显式 allow；`*` 仍默认 `ask`。
- 工具/域清单：`read` / `edit` / `glob` / `grep` / `bash` / `task` / `skill` / `lsp` / `question` / `webfetch` / `websearch` / `external_directory` / `doom_loop`。
- 默认策略：多数 `allow`；`doom_loop` 与 `external_directory` 默认 `ask`；`read` 默认 `allow` 但 `.env` / `.env.*` 默认 `deny`（`.env.example` `allow`）。
- 运行时审批 UI 提供 **三个**结果（与 `tui.json` 解耦）：

  | 结果 | 含义 |
  | --- | --- |
  | `once` | 仅批准本次 |
  | `always` | 批准"匹配建议模式"的所有未来请求（**当前 OpenCode session 期间**） |
  | `reject` | 拒绝 |

  文档原文："approve future requests matching the suggested patterns (for the rest of the current OpenCode session)"。
- Agent 级可覆写（`agent.<name>.permission`）；agent markdown 文件也支持 frontmatter `permission:` 字段。
- `doom_loop` 在同一 tool call 三次重复同样输入时触发。

### 2.2 仓库结构（顶层 tui 目录）

来源：`https://github.com/anomalyco/opencode/tree/dev/packages/opencode/src/cli/cmd/tui`

```
tui/
├── app.tsx                33 KB / 1113 行  主入口（仅 shell + Provider 树 + 命令注册）
├── attach.ts              2.8 KB           attach <url> 子命令
├── attention.ts           8.9 KB           通知/提示音
├── event.ts               1.6 KB           TUI 内部事件类型
├── keymap.tsx             8.4 KB           键位/模式栈/leader
├── layer.ts               300 B            Effect Layer 装配
├── thread.ts              8.1 KB           yargs CLI + spawn worker
├── validate-session.ts    823 B            启动前校验 session
├── win32.ts               3.5 KB           Windows ENABLE_PROCESSED_INPUT 处理
├── worker.ts              3.0 KB           Worker 线程：跑 Server
├── component/             30+ files        Dialog/Spinner/Logo/Prompt/...
├── config/                5 files          tui.ts / keybind.ts / tui-schema.ts / tui-migrate.ts / cwd.ts
├── context/               20+ files        Providers + theme.tsx(31KB) + sync.tsx(23KB)
├── feature-plugins/       4 dirs           home / session / sidebar / system
├── plugin/                5 files          插件 runtime + api + slots
├── routes/                home.tsx + home/, session/
├── ui/                    10 files         dialog, dialog-select, dialog-prompt, ...
└── util/                  11 files         audio, clipboard, selection, scroll, signal, transcript, ...
```

**关键观察**：

- `app.tsx` 1113 行但**本身不渲染功能性 UI**。它只做：(1) 装配 Provider 树；(2) 注册全局 keybind 与 ~35 个命令；(3) 监听少量全局事件（`session.error` / `session.deleted` / `installation.update-available`）；(4) 处理 Win32 终端模式；(5) 注入插件 API。**所有功能性 UI（消息、工具、prompt、权限、状态栏、侧栏）都委托给 `Home` / `Session` 子路由**。
- 没有"单文件大组件"——每个功能一个独立文件：`dialog-model.tsx`、`dialog-mcp.tsx`、`dialog-session-list.tsx`（10.8KB）、`dialog-provider.tsx`（14.6KB）等。
- 主题是**首要子系统**：`context/theme.tsx` 31KB、还有 `context/theme/` 子目录；颜色是运行时 token，所有组件通过 `useTheme()` 取色。
- 同步是另一个大子系统：`context/sync.tsx` 23KB + `context/sync-v2.tsx` 18KB（两套并存，v1/v2 兼容层）。
- 插件 API 是"插槽（slot）"机制：`TuiPluginRuntime.Slot name="app" / "app_bottom"` 等；通过 `api.routes: Map` 注册自定义路由。
- `util/clipboard.ts`（6.2KB）+ `util/selection.ts`（1.9KB）专门处理"选区即复制"。

### 2.3 关键源码要点

#### 2.3.1 `thread.ts`（CLI 启动流）

- TUI 是个**子命令**：`opencode $0 [project]`。支持 `--model`, `--continue/-c`, `--session/-s`, `--fork`, `--prompt`, `--agent`。
- 启动顺序（行 161–219）：
  1. `win32InstallCtrlCGuard()` + `win32DisableProcessedInput()` 抑制 Windows Ctrl-C 与 ENABLE_PROCESSED_INPUT。
  2. `process.chdir(project)` 切到工程目录。
  3. `new Worker(file, { env })` 起一个 **真正的 Worker 线程**（用 `Worker(file, { env })`，不是 child_process）；该 worker 跑服务端。
  4. `Rpc.client(worker)` 建 RPC 通道。
  5. `validateSession(...)` 启动前预检 session/目录/网络。
  6. 调 `createTuiRenderer(config)` 创建 `CliRenderer`；调 `tui({...})` 渲染 Solid 树。
  7. `await handle.done` 等退出。
  8. finally：5s 内 `client.call("shutdown")` 优雅停 worker，再 `worker.terminate()`。
- `OPENCODE_PROCESS_ROLE = "worker"` + `OPENCODE_RUN_ID` 通过 env 传给 worker。
- `SIGHUP` 触发 `reload`：`client.call("reload", undefined)` 让 worker 重新加载配置。
- 若检测到 `--port` / `--hostname` / `--mdns`，worker 启动**外部 HTTP 服务**（server 走真实端口，外部可访问），否则 TUI 与 worker 通过 `Worker + Rpc` 走内存 fetch / EventSource 桥接（`createWorkerFetch`、`createEventSource`）。

#### 2.3.2 `worker.ts`

- 订阅 `GlobalBus.on("event", ...)`，通过 `Rpc.emit("global.event", event)` 转发给 TUI 线程。
- 暴露 RPC：`fetch`（包装服务端 Hono app）/ `snapshot`（写 heap dump）/ `server`（开端口）/ `checkUpgrade` / `reload` / `shutdown`。

#### 2.3.3 `keymap.tsx`

- 包装 `@opentui/keymap/solid` 的 `KeymapProvider` / `useKeymap` / `useBindings`。
- 内部维护一个 **Mode 栈**（`createOpencodeModeStack`）：
  - 基础模式 `OPENCODE_BASE_MODE = "base"`。
  - 任意子模块可 `push(mode)` 注册局部键位（push 时打开，pop 时关闭），`useOpencodeModeStack()` 取。
  - 用 `WeakMap<keymap, stack>` 保证 GC。
- `registerOpencodeKeymap(keymap, renderer, config)` 一次性注册 addons：
  - `addons.registerCommaBindings`（用逗号连击）
  - `addons.registerBaseLayoutFallback`（未匹配键的兜底）
  - `addons.registerTimedLeader`（leader + 2000ms 超时）
  - `addons.registerEscapeClearsPendingSequence`（Esc 清未完成序列）
  - `addons.registerBackspacePopsPendingSequence`（Backspace 退序列）
  - `addons.registerManagedTextareaLayer`（**焦点在 Textarea 时启用输入层**；其他时候输入层失效）
- `hasManagedTextareaFocus(renderer)` 判断当前焦点是不是 `TextareaRenderable` 而非 `InputRenderable`——这是**关键**：只有 composer 拿到焦点时，输入层键位才生效；其他时候 app 层键位生效。
- `KEY_ALIASES = { enter→return, esc→escape, pgdown→pagedown, pgup→pageup }`，通过 `appendBindingExpander` 在键位匹配时改写。
- `useCommandSlashes()` 把命令映射成 `/xxx` 列表（`slashName` / `slashAliases`）。
- `useLeaderActive()`：是否处于 leader 按下后的等待态。

#### 2.3.4 `win32.ts`

- 通过 `bun:ffi` 调 `kernel32.dll`：`GetStdHandle` / `GetConsoleMode` / `SetConsoleMode` / `FlushConsoleInputBuffer`。
- `ENABLE_PROCESSED_INPUT = 0x0001` 必须清掉，否则 Ctrl-C 变 CTRL_C_EVENT 直接杀进程组。
- `win32InstallCtrlCGuard` 同时 hook `stdin.setRawMode`，并在 `setImmediate` + 100ms 间隔轮询兜底（因为其他运行时会重新设置 console 模式，且该 flag 是 console 全局而非 per-process）。
- `win32FlushInputBuffer` 在切换时清掉残留输入。

#### 2.3.5 `event.ts`（TUI 内部事件总线）

- 用 `@opencode-ai/core/event` 的 `EventV2.define` + Effect Schema 定义：
  - `TuiEvent.PromptAppend { text }`：往 prompt 追加文本。
  - `TuiEvent.CommandExecute { command }`：`Schema.Union(Literals, String)`——白名单字面量 + 自由字符串。
  - `TuiEvent.ToastShow { title?, message, variant: "info"|"success"|"warning"|"error", duration }`。
  - `TuiEvent.SessionSelect { sessionID }`。
- DEFAULT_TOAST_DURATION = 5000ms。

#### 2.3.6 `app.tsx` 行为

- `tui(input)` 三件套：(1) `win32InstallCtrlCGuard` + `win32DisableProcessedInput`；(2) `createTuiRenderer` + `createDefaultOpenTuiKeymap(renderer)` + `registerOpencodeKeymap`；(3) `mountTui(...)` 启动 Solid 渲染。
- `tuiRendererConfig` 关键选项：
  - `externalOutputMode: "passthrough"`
  - `targetFps: 60`
  - `useKittyKeyboard: {}`（Kitty keyboard protocol）
  - `autoFocus: false`
  - `openConsoleOnError: false`
  - `useMouse: mouseEnabled`（**默认 true**，可被 `OPENCODE_DISABLE_MOUSE` flag 或 `tui.json` 的 `mouse:false` 关闭）
  - `exitOnCtrlC: false`（**关闭**默认 Ctrl-C 退出，改由 app 自己处理）
  - `consoleOptions.keyBindings: [{ name: "y", ctrl: true, action: "copy-selection" }]`
  - `consoleOptions.onCopySelection(text) → Clipboard.copy(text)`：**核心行为"选中即复制"**
- `<box>`（顶层容器）上的鼠标事件：
  - `onMouseDown`：仅在实验 flag `OPENCODE_EXPERIMENTAL_DISABLE_COPY_ON_SELECT` 开启且**右键**时复制选区。
  - `onMouseUp`：**默认路径下**，鼠标松开即复制选区。这是 OpenCode 区别于绝大多数 TUI 的关键交互。
- 监听的事件：
  - `tui.command.execute`、`tui.toast.show`、`tui.session.select`（跨 workspace 路由到本地 keymap / toast / route）
  - `session.deleted`（如果当前正显示该 session，跳回 home）
  - `session.error`（除 `MessageAbortedError` 外，toast 出来）
  - `installation.update-available`（弹 `DialogConfirm` 调用升级）
- 调色板预热：`renderer.getPalette({ size: 16 })` 异步获取主题色，避免 system 主题首帧闪烁。
- 注册的命令（appCommands，`createMemo` 形式，约 35 个）：
  - 会话：`session.list`（`/sessions`/`/resume`/`/continue`）、`session.new`（`/new`/`/clear`）、`session.quick_switch.1..9`（hidden，绑定数字键）
  - Workspace：`workspace.copy_path`、`workspace.list`（`/workspaces`，受实验 flag 门控）
  - 模型/Agent：`model.list`（`/models`/`/mo`）、`model.cycle_recent[_reverse]`、`model.cycle_favorite[_reverse]`、`agent.list`（`/agents`）、`mcp.list`（`/mcps`）、`agent.cycle[.reverse]`、`variant.cycle`、`variant.list`（`/variants`）
  - Provider：`provider.connect`（`/connect`）、`console.org.switch`（`/org`/`/orgs`/`/switch-org`，仅当 `switchableOrgCount > 1` 存在）
  - 系统：`opencode.status`（`/status`）、`theme.switch`（`/themes`）、`theme.switch_mode`、`theme.mode_lock`、`help.show`（`/help`）、`docs.open`、`app.exit`（`/exit`/`/quit`/`/q`）、`app.debug`、`app.console`、`app.heap_snapshot`、`terminal.suspend`（非 win32）、`terminal.title.toggle`、`app.toggle.{animations,file_context,diffwrap,paste_summary,session_directory_filter}`
  - 内部：`COMMAND_PALETTE_COMMAND`（`command.palette.show`，hidden）调 `CommandPaletteDialog`

#### 2.3.7 路由与 Slot

- `Route` 是结构化判别式联合：`{ type: "home" }` / `{ type: "session", sessionID }` / `{ type: "plugin", id, data? }`。
- 顶层 `App` 组件只做 `<Switch><Match when="home"><Home/></Match>...`：所有功能性 UI 在子路由里。
- 插件扩展点（slot）：`<TuiPluginRuntime.Slot name="app">`、`name="app_bottom"`。
- Plugin API 通过 `createTuiApi(...)` 构造：暴露 `dialog / keymap / route / event / sdk / sync / theme / toast / renderer / attention`。

#### 2.3.8 Mouse / 选择 / 剪贴板

- `tui.json` 的 `mouse: false` 关闭后会回退到终端原生选择。
- 默认 `useMouse: true`，**鼠标松开即复制**——这是 OpenCode 的"特色"，但代价是 OS 选区行为被劫持。
- 同样支持 Kitty keyboard protocol（多键序列、修饰键位检测）。
- `consoleOptions.onCopySelection` 把选中文本送进 `Clipboard.copy`（6KB 实现，含 OSC 52 / pbcopy / wl-copy / xclip 探测）。

### 2.4 OpenCode 行为小结（XBotv2 设计基线）

| 维度 | OpenCode 现状 | XBotv2 v1 选择 | 理由 |
| --- | --- | --- | --- |
| 渲染栈 | OpenTUI + Solid.js | Textual + Rich | 既有实现 |
| 通信 | Worker 线程 + Rpc（内部）/ 外部 HTTP | **默认 HTTP + SSE**（`aiohttp`）；stdio 保留为测试/回放后端（见 §10.5） | 性能 + 远端可寻址 + 与 OpenCode 同形 |
| 布局 | 流 + 顶/底栏 + 多 dialog | 流 + 紧凑状态栏 + 内联选择 | 用户硬约束 |
| 主题 | 31KB 主题系统，多主题切换 | 单主题，变量化预留 | 减面 |
| 键位可配 | `tui.json` 完整覆盖 | v1 内置；v2 引入 JSON | 渐进 |
| Leader key | `ctrl+x` + 2s 超时 | v1 不引入；v2 再评估 | 减面 |
| 模式 | Base mode + Mode 栈 | 显式 `ModeController` | 适合 Python |
| 鼠标 | `useMouse: true`；松开复制 | 滚轮 + 选中文本不强复制 | 保留原生选择 |
| 权限 ask 结果 | `once` / `always` / `reject` | `once` / `session` / `always`（与运行时一致） | XBotv2 协议层有 session 概念 |
| Slash 命令 | 17+ | v1 4 个：`/exit` `/clear` `/help` `/status` | 减面 |
| 复制 | 默认选中即复制 | v1 走 `pyperclip` / OSC 52 | 与 XBotv2 一致 |
| 通知/音 | `attention.*` 完整 | 不引入 | 减面 |
| 滚动 | macOS 加速 + 速度 | 仅速度（数字可调） | 减面 |
| Diff 渲染 | `auto` / `stacked` | 不引入（不在 TUI 渲染 diff） | 由服务端 artifacts 承担 |
| Plugin | Slot + Routes | 不引入 | 协议边界 |
| 主题切换 | `/themes` | 不引入 | 单主题 |
| 状态视图 | `<leader>s` 全屏 | 紧凑状态栏一行 | 硬约束 |
| 侧栏 | `<leader>b` | 不引入 | 硬约束 |

---

## 3. XBotv2 本地硬约束

下列约束来自项目自身的明确选择，优先级高于 OpenCode 默认：

1. **工作范围** — 只在 `XBotv2/` 内迭代；旧 `xbot/` 弃用。
2. **TUI 版本** — 默认 `--mode tui`（Textual），不复活旧 curses 界面作为主界面（curses 保留为回归用 fallback）。
3. **布局** — 事件流为主，不保留右侧工具/事件区。
4. **决策区** — 不使用独立选项条/按钮条；选择项在事件流内联。
5. **焦点** — 整个 TUI 同一时刻只有一个键盘焦点区（composer 或流中"上一次激活的选择行"）。
6. **等待期** — 存在 live choice 时，composer 隐藏并禁用；不接收任何字符。
7. **选择交互** — Up/Down 切换选项，Enter 确认。
8. **正常输入** — Enter 发送，Shift+Enter 换行，Up/Down 浏览历史。
9. **滚动** — 鼠标滚轮是首选，PageUp/PageDown 不是主要交互。
10. **中文输入** — 必须工作（IME、宽字符、UTF-8 全链路）。
11. **消息不丢** — 任何流上的事件在 UI 中要么显式可见，要么有明确"折叠/收起"指示，禁止静默丢弃。
12. **状态栏** — 紧凑一行，不做大面板。
13. **时间戳** — 每条消息都显示。
14. **活动指示** — 当前 turn 显示 spinner、已用时长、当次 token 用量。
15. **用量** — LM Studio 兼容的 OpenAI 风格 `usage` 帧到达时，必须在状态栏与活动行反映。
16. **协议边界** — TUI 不导入 `xbotv2.core` / `xbotv2.llm` / 任何插件；唯一对话面是 JSONL。
17. **可诊断** — 关键事件可被 trace，记录为 UTF-8 JSONL。
18. **三态权限 ask 结果** — `once` / `session` / `always`，与运行时 `SandboxPolicy` 一致；**不**采用 OpenCode 的 `reject`（拒绝由"主动 deny"承担，不算"ask 的结果"之一）。
19. **不引入 OpenCode 的多 dialog 体系** — 用 slash 命令 + 内联选择 + 紧凑状态栏覆盖大多数场景；侧栏、命令面板、主题切换 v1 不实现。
20. **不引入 OpenCode 的 plugin / slot 体系** — TUI 仅消费协议；扩展通过协议事件达成。
21. **传输层可选 stdio 或 HTTP/SSE**（详见 §10.5） — TUI 与 server 之间的 wire 协议：**v1 默认 HTTP + SSE**（`aiohttp` 单依赖），`stdio` JSONL 保留为测试/回放/向后兼容后端；CLI 显式 `--transport {stdio,http}` 切换。
22. **TUI 与 server 解耦** — HTTP 模式下 TUI 与 server 是独立进程；server 长驻，TUI 可 attach/detach；TUI 退出不杀 server。
23. **远端访问需显式开启** — HTTP server 默认绑定 `127.0.0.1:4096`；`--bind 0.0.0.0` 暴露 LAN 时**必须**配合 `--server-token` Bearer 鉴权。

---

## 4. 设计原则

1. **流是一等公民** — 所有"对用户可见的事"都在一条时间线上；任何跳出流的内容必须有充分理由。
2. **状态机是真相** — UI 不基于"上一次看到了什么事件"猜测；模式由 `Mode` 控制器持有。
3. **零双投** — 任何回执/确认/通知在协议层和 UI 层都只发一次；本地回执按 `request_id` 去重。
4. **少即是多** — 不引入 OpenCode 的多 dialog、命令面板、主题切换、插件视图、leader key。差异点用斜杠命令暴露。
5. **可被脚本化** — 任何渲染都能通过驱动协议帧被回放测试。
6. **错误显眼但不阻塞** — 错误用独立行展示，但永远不要让状态栏/历史被错误刷屏。
7. **键盘优先，鼠标增强** — 任何鼠标可达的功能都必须先有键盘等价。
8. **一个文件一个职责**（取自 OpenCode 的 `dialog-*.tsx` 拆分） — 每个 dialog/组件独立成文件，长度上限 600 行（OpenCode 实际最大单文件 ~30KB，对应到 Python 约 600–800 行）。
9. **颜色走变量** — 所有颜色来自 `tokens.py` 中的命名变量；将来主题切换不需要重写组件。

---

## 5. 信息架构与屏幕分区

### 5.1 屏幕（自上而下）

```
┌────────────────────────────────────────────────────────────────────────┐
│ #status_bar  1 行                                                       │  ← 状态栏
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  #transcript   1fr                                                      │  ← 事件流（整体可滚）
│  ······                                                                │
│                                                                        │
├────────────────────────────────────────────────────────────────────────┤
│ #completion_popup  0/N 行  (slash 补全；仅 / 前缀时显示)                │  ← 补全
│ #composer_hint  0/1 行  （live choice / pending / 普通）                │  ← 提示
│ #input         3..N 行（仅 COMPOSING 时显示）                          │  ← 输入框
└────────────────────────────────────────────────────────────────────────┘
```

要点（用户 2026-06-05 明确确认）：

- **状态栏** 始终显示，**单行**。不与"右侧大面板"共存。
- **事件流** 占满中段。**整体可滚**（`VerticalScroll`），但**单个 entry 不许带内嵌滚动条**——每个消息 / 工具结果完全平铺。
- **completion_popup** 仅在 composer 文本以 `/` 开头时显示；高度按候选数自适应。
- **composer 提示行** 高度为 0 或 1，承载当前模式提示文字。
- **composer 输入框** 仅在 `COMPOSING` 模式可见。`CHOOSING` / `SUBMITTED` 模式隐藏。
- 不存在 "右侧工具/事件" 子区域；不存在 "任务列表" 边栏；不存在 "工具按钮条"。

### 5.2 视觉示例（常规对话）

```
┌────────────────────────────────────────────────────────────────────────┐
│ XBotv2 ●Ready  sess:9d1a/agent  agent:XBotv2  turn:2  ↻ 4.1s  ⌃0  ◌12.4k│
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  12:01:03  You                                                          │
│  给这个项目写一份 README                                                 │
│                                                                        │
│  12:01:04  XBotv2                                                       │
│  好的，我先看一下仓库结构…                                               │
│                                                                        │
│  ── tool  filesystem_list   0.3s  ✓                                    │
│     args: {"path": "."}                                                 │
│                                                                        │
│  12:01:09  XBotv2                                                       │
│  已读完结构，下面是初稿：                                                │
│  …                                                                     │
│                                                                        │
│  12:01:10  ↻ turn 2 working  4.1s  in:1.2k  out:312  total:1.5k         │
│                                                                        │
├────────────────────────────────────────────────────────────────────────┤
│ ▸ Message XBotv2  …  (Enter to send · Shift+Enter newline · ↑/↓ history)│
└────────────────────────────────────────────────────────────────────────┘
```

### 5.3 视觉示例（live permission choice）

```
┌────────────────────────────────────────────────────────────────────────┐
│ XBotv2 ●Approval required  …  turn:3  ⌃0  ◌3.2k                        │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  12:02:11  XBotv2                                                       │
│  我要执行 shell：npm test                                                │
│                                                                        │
│  12:02:11  approval request                                             │
│  Run shell command? "npm test"                                          │
│    > Allow once                                                         │
│      Allow session                                                      │
│      Always allow                                                       │
│      Deny                                                               │
│                                                                        │
├────────────────────────────────────────────────────────────────────────┤
│ Use ↑/↓ to choose, Enter to confirm                                    │
└────────────────────────────────────────────────────────────────────────┘
```

`CHOOSING` 模式下：

- composer 隐藏（`display: none`，不参与布局）。
- 焦点可被显式设在选择行上（或流容器整体保持非聚焦），但 Up/Down 仍工作。
- 提示行从 "Message XBotv2" 切到 "Use ↑/↓ to choose, Enter to confirm"。

### 5.4 视觉示例（ask_user）

```
┌────────────────────────────────────────────────────────────────────────┐
│ XBotv2 ●Waiting for user  …  turn:3                                    │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  12:02:40  XBotv2                                                       │
│  我有几个风格问题想确认                                                  │
│                                                                        │
│  12:02:40  question                                                     │
│  你希望 README 用哪种风格？                                              │
│    > 简洁工程风                                                          │
│      营销文案风                                                          │
│      (或者直接键入你的偏好)                                              │
│                                                                        │
├────────────────────────────────────────────────────────────────────────┤
│ Use ↑/↓ to choose, Enter to confirm                                    │
└────────────────────────────────────────────────────────────────────────┘
```

无预定义选项时，仅显示问题行 + 提示文字；不渲染选项行。

---

## 6. 视觉规范

### 6.1 色板（Tokyo Night 暗色为基，对应 OpenCode 风格）

| 角色 | hex | 借鉴自 | 用法 |
| --- | --- | --- | --- |
| `bg.screen` | `#0f1115` | 自定 | Screen 背景 |
| `bg.sunken` | `#171a21` | 自定 | 状态栏、输入框、卡片底 |
| `border.subtle` | `#2d3440` | 自定 | 输入框默认边 |
| `border.focus` | `#7aa2f7` | Tokyo Night blue | composer 聚焦边 |
| `fg.default` | `#d6dae2` | 自定 | 普通文本 |
| `fg.dim` | `#8b95a7` | 自定 | 元信息、已结束状态 |
| `fg.user` | `#7dcfff` | Tokyo Night cyan | 用户名/时间戳 |
| `fg.assistant` | `#9ece6a` | Tokyo Night green | Agent 名/时间戳 |
| `fg.notice` | `#bb9af7` | Tokyo Night magenta | 通用通知/回执 |
| `fg.tool` | `#e0af68` | Tokyo Night yellow | 工具行 |
| `fg.activity` | `#7aa2f7` | Tokyo Night blue | 进行中活动 |
| `fg.error` | `#f7768e` | Tokyo Night red | 错误 |

- 所有颜色集中在 `xbotv2/tui/tokens.py` 的命名变量。
- v1 仅实现暗色；变量化是为将来主题切换不留大改。
- OpenCode 通过 `context/theme.tsx`（31KB）+ 11 个子目录的主题定义文件做主题；XBotv2 v1 不引入此机制。

### 6.2 排版

- 等宽字体（终端默认 mono），**禁用** Rich 解释用户文本为标记（`markup=False`）。这一条是 OpenCode 同样强制的——它的 prompt 内部用 rich text 渲染，但任何用户输入都不进 markup 路径。
- 角色标签加粗（`bold` + 角色色）；时间戳 dim + 角色色。
- 工具/活动行不使用大写装饰，遵循 `tool  name  status`。
- 严禁使用 Nerd Font 字符作为必需信息（失败回退为 ASCII：`|/-`\`，`✓`，`✗`，`◌`）。

### 6.3 间距

- 横向内边距 2 列；流内每个 entry 之间纵向间距 1 行。
- composer 上下各 1 行内边距，左右 1 列。
- 状态栏左右各 1 列内边距。

### 6.4 主题

- 默认主题：暗色（`#0f1115` 系）。
- 亮色主题：本版本不提供，但所有颜色必须来自命名变量；新增主题时仅替换变量值。
- 主题与 `provider` / `personality` 解耦；不引入"按 provider 换主题"特性。
- 不实现 `diff_style`、`scroll_acceleration`、attention 系统——OpenCode 提供的这些特性 XBotv2 v1 不复刻。

---

## 7. 组件规范

> 借鉴 OpenCode 的"一个文件一个职责"：`textual_client.py` 当前 844 行包含所有内容，需要按职责拆分。目标文件结构：

```
xbotv2/tui/
├── __init__.py
├── app.py                # XBotTextualApp + createTuiApp 入口
├── render_log.py         # 单一 RenderItem 列表与投影
├── mode.py               # ModeController
├── composer.py           # ComposerTextArea + 路由
├── status_bar.py         # 状态栏
├── transcript.py         # 事件流容器
├── entries/
│   ├── message.py
│   ├── tool.py
│   ├── activity.py
│   ├── notice.py
│   └── choice.py
├── command.py            # slash 命令注册
├── trace.py              # 已有
├── terminal.py           # 已有（JSONL 客户端）
├── client.py             # 已有（curses 旧）
├── textual_state.py      # 已有（共享协议状态）
└── tokens.py             # 颜色/排版变量
```

### 7.1 `StatusBar`（`#status_bar`）

固定 1 行。内容从左到右用 `  ` 分隔：

| 段 | 含义 | 例 |
| --- | --- | --- |
| `XBotv2` | 应用名 | `XBotv2` |
| 状态徽标 | 当前模式 | `●Ready` / `●Running` / `●Approval required` / `●Error` |
| 会话 | `sess:<sid-short>/<thread>` | `sess:9d1a/agent` |
| Agent | `agent:<name>` | `agent:XBotv2` |
| Turn | `turn:N` | `turn:3` |
| Activity | 活跃时 `↻ E.EEs` | `↻ 4.1s` |
| Queue | `queued:K` | `queued:0` |
| Usage | `in:X out:Y total:Z req:R` | `in:1.2k out:312 total:1.5k req:2` |

实现：

- 单一 `Static` 组件；`update()` 全量替换文本。
- 由 `App._refresh_status()` 在以下时机调用：
  - 任何状态字段变化；
  - 活动 timer 触发（0.5s）以刷新 spinner 与 elapsed；
  - `usage` 帧到达；
  - 队列入/出。

### 7.2 `TranscriptScroll`（`#transcript`）

- `VerticalScroll`，`can_focus=False`，永不参与键盘焦点（与 OpenCode 的"Transcript 不抢键位"原则一致）。
- 鼠标滚轮滚动。
- 自动 `scroll_end(animate=False)` 行为：
  - 当用户位于"末尾"时，新条目追加后自动跟随；
  - 当用户向上滚动后，**不**自动滚回（保留其位置），并显示 "↓ N new" 浮动提示。
- 内部容纳 `entry` 类子节点（见 7.3–7.6）。

### 7.3 `MessageEntry`（按角色）

```
HH:MM:SS  <role>             ← .meta  角色色
<正文>                         ← .body  fg.default
```

- `user`：`role = "You"`，色 `fg.user`。
- `assistant`：`role = state.agent_name`，色 `fg.assistant`。
- 同一秒内的连续消息可省略时间戳，**但每条消息**仍必须有一行可见元信息（保证"每条消息都有时间戳"硬约束的最低表达）。
- 消息体若为空（仅空白），**不渲染**该行；这避免了 OpenCode 客户端之外的"空白 agent 块"问题。
- 长内容自动按宽度换行；不做 Rich 标记解释（`markup=False`）。

### 7.4 `ToolEntry`

```
HH:MM:SS  tool  <name>  <status>          ← .meta  fg.tool
args: <preview>                            ← 可选
result: <preview>                          ← 可选
```

- `status ∈ {pending, running, ok, error, denied, cached}`。
- `args_preview` / `summary` 来自 `TuiTool.args_preview` / `TuiTool.summary`（最长 120 字符、超长省略号）。
- 状态变更（来自 `tool_result`）原地 `update()` 元信息与结果行，不重新 mount。
- OpenCode 的对应位置在 `routes/session` 子目录（未在文档中读取但目录存在），展示形式类似但更复杂（折叠/展开、参数高亮、diff 视图）；XBotv2 v1 暂不复制折叠/diff 能力。

### 7.5 `ActivityEntry`（`#activity`）

- 每个 turn 起始 mount 一次；turn 结束原地 `update()` 为 `done`。
- 文案：
  - 进行中：`↻ turn N working  E.EEs  in:X out:Y total:Z`
  - 完成：`✓ turn N completed  E.EEs  in:X out:Y total:Z`
  - 出错：`✗ turn N failed  E.EEs  in:X out:Y total:Z`
- spinner 字符序列：`|/-`\`，500ms 切换。
- 用量缺省显示 0，不写 `0/0/0`。

### 7.6 `NoticeEntry`（含内联选择）

通用通知：

```
HH:MM:SS  <kind label>          ← .meta  fg.notice
<text>                            ← .body
```

`permission_request` 与 `user_input_required` 是有内联选择的特殊通知：

```
HH:MM:SS  approval request / question    ← .meta
<reason / question>                       ← .body
> Allow once                              ← .choices  （可激活态反显）
  Allow session
  Always allow
  Deny
```

- 选中态：`reverse bold` 背景反白。
- 未选中态：`dim`。
- 已被选定（key 已 resolve）：整行 `dim` + 文本 `selected: <label>`。
- 选项渲染**作为文本字符串**（`rich.text.Text`），不引入 `Button` 控件，避免抢焦点（与 OpenCode 的"dialog 内不使用可聚焦按钮造成多焦点"经验一致——OpenCode 用 `dialog-select.tsx` 全屏覆盖）。

### 7.7 `ComposerHint`（`#composer_hint`）

单行 `Static`，内容随模式：

| 模式 | 文本 |
| --- | --- |
| 普通 | `Enter to send · Shift+Enter newline · ↑/↓ history` |
| `pending_user_input` | `Type an answer` |
| `pending_permission` | `Type allow/deny` |
| `CHOOSING` | `Use ↑/↓ to choose, Enter to confirm` |
| `SUBMITTED` | `Waiting for response` |

### 7.8 `ComposerTextArea`（`#input`）

- 继承 `Textual.widgets.TextArea`。
- 默认高度 3；按可见行数（`text.count("\n") + 1`）增长，封顶 `screen.height - 8`。
- 中文/IME 由 Textual 默认 IME 通道承载；TUI 不接管 IME，**关键**是所有 IME 提交走 `TextArea.Changed` 事件（不是裸 `Key`），从而避免把 IME 中间态当成本地字符。
- 触发：
  - `Enter` → `submit_composer()`
  - `Shift+Enter` → 插入 `\n`
  - `↑`（空文本或已在浏览历史）→ `history_previous()`
  - `↓`（在浏览历史时）→ `history_next()`
  - `Esc` → `action_clear_input()`
- 在 `CHOOSING` 模式中，`ComposerTextArea` 隐藏并禁用；不响应任何键。
- 借鉴 OpenCode 的 `addons.registerManagedTextareaLayer` 思想：键位按"焦点是否在 composer"切换；Textual 自身已有焦点机制，这里只保证 `on_key` 的 `event.key` 是真实按键而非 IME 合成。

---

## 8. 状态机

### 8.1 模式

```
                 +----------+
   on_mount ---> | COMPOSING| <----------------------+
                 +----+-----+                        |
                      | user.message 发出            |
                      v                              |
                 +----------+    turn_finished       |
                 | RUNNING  | ---------------->     |
                 +----+-----+                        |
                      | permission_request           |
                      | user_input_required          |
                      v                              |
                 +----------+                        |
                 | CHOOSING |                        |
                 +----+-----+                        |
                      | Enter / option confirm       |
                      v                              |
                 +----------+    recorded frame      |
                 | SUBMITTED| ---------------->     |
                 +----+-----+                        |
                      | error (recoverable)          |
                      v                              |
                 +----------+                        |
                 | ERROR    | --- recover --------> -+
                 +----------+
```

不变式：

- `CHOOSING` 时，composer `display=False` 且 `disabled=True`。
- `SUBMITTED` 时，composer 仍隐藏，直到服务器给出 `*_recorded` / `permission_denied` / `turn_finished`。
- 同一 `request_id` 的回执**只**通过 `_submitted_interaction_ids` 去重一次。
- 模式变化由显式 `set_mode()` 触发；不允许散落的局部 `if mode == ...` 改 UI。

### 8.2 输入路由

`submit_composer()` 的判定顺序：

1. 若 `CHOOSING` 激活 → 忽略本次提交（Enter 应走 `confirm_active_choice` 而非 composer）。
2. 取 `composer.text.strip()`；trace `tui.submit` 记录原文与 `repr()`。
3. 调 `route_submitted_text()`：
   - 命中 `pending_user_input` → 投递到 `_answers`；
   - 命中 `pending_permission` → 解析后投递到 `_permission_decisions`；
   - 否则入 `_outbound_messages`。
4. 清空 composer、记录历史、刷新 UI。
5. 若 `_outbound_messages` 不空且 turn worker 未运行 → 启动 `run_worker(_drain_message_queue, ...)`。

`route_submitted_text()` 单元测试要点：

- 文本中以 `/` 开头的"命令式回复"（如 `allow`/`deny`）不应当被识别为新消息（已被状态机的 `pending_permission` 拦截）。
- 任何提交即使没有 pending request，也必须入队成功（消息不丢）。

### 8.3 选择状态

- `_active_choice_key`：当前可见的待选项 key。
- `_active_choice_index`：0-based 选中下标。
- `_choice_payloads[key]`：list[`InlineChoice`]`。
- `_choice_request_ids[key]`：对应协议的 `request_id`。
- `_resolved_choice_keys`：已回执 key 集合。
- `_submitted_interaction_ids`：**协议层**已发送回执的 `request_id` 集合。

不变式：

- `confirm_active_choice()` 调用前必须检查 `_submitted_interaction_ids`，避免重复发包。
- 一个 key 在 resolve 之后不再进入 `_active_choice_key`。

### 8.4 错误与重置

- 任何未捕获异常 → `_record_error()`：
  - `state.status = "Error"`
  - `state.errors.append(str(exc))`
  - `errors` 列表新增条目 → 自动产生一个 `entry.error`。
- `ERROR` 模式下 composer 行为由恢复性决定：
  - `ConnectionError` / EOF → 不可恢复，状态栏显示 `●Error`，可按 `Ctrl+C` 退出。
  - 协议级 `error` 事件 → 标记一次失败 turn，但 composer 应能恢复可用（除非协议明确要求关闭会话）。
- `Ctrl+C` 始终退出（`BINDINGS`）。

---

## 9. 键位与命令

### 9.1 XBotv2 默认键位表

> 不引入 leader key。所有快捷键是裸键。借鉴 OpenCode 同样的"按命令命名 + 集中可替换"原则，集中到 `command.py` 注册表。

| 命令 ID | 键位 | 行为 | 模式限制 |
| --- | --- | --- | --- |
| `app_exit` | `Ctrl+C`, `Ctrl+D` | 退出 | 任意 |
| `input_clear` | `Esc` | 清空 composer | 仅 `COMPOSING` |
| `input_submit` | `Return` | 提交 composer | 仅 `COMPOSING` |
| `input_newline` | `Shift+Return` | 换行 | 仅 `COMPOSING` |
| `history_previous` | `Up` | 历史上一条 | `COMPOSING` 且文本空或已在浏览 |
| `history_next` | `Down` | 历史下一条 | `COMPOSING` 且在浏览 |
| `choice_prev` | `Up` | 选项上一项 | `CHOOSING` |
| `choice_next` | `Down` | 选项下一项 | `CHOOSING` |
| `choice_submit` | `Return` | 选项确认 | `CHOOSING` |
| `scroll_up` | （鼠标）滚轮上 | 事件流上滚 | 任意 |
| `scroll_down` | （鼠标）滚轮下 | 事件流下滚 | 任意 |
| `slash_help` | `/help` | 追加帮助 | `COMPOSING` |
| `slash_status` | `/status` | 追加状态摘要 | `COMPOSING` |
| `slash_clear` | `/clear` | 清空事件流 | `COMPOSING` |
| `slash_exit` | `/exit`, `/quit` | 退出 | `COMPOSING` |

**明确不绑定**（与 OpenCode 对照）：

- `PageUp` / `PageDown` — 用户硬约束。
- `Tab` / `Shift+Tab` — 不做 agent 切换（XBotv2 v1 单 agent）。
- `Ctrl+T` — 不做 variant 切换。
- `Ctrl+R` — 不做 session 重命名/历史（与 shell 冲突）。
- `Ctrl+P` — 不做命令面板（v1）。
- Leader key `Ctrl+X` — 不引入。
- `Shift+I`, `<leader>+i` 系列 — 无插件系统。

### 9.2 斜杠命令（v1）

| 命令 | 行为 | 阶段 |
| --- | --- | --- |
| `/exit` / `/quit` | 退出 TUI | v1 |
| `/clear` | 清空事件流（保留 session/thread） | v1 |
| `/help` | 在流底部追加帮助文本（每条命令独立一行） | v1 |
| `/status` | 在流底部追加当前状态（运行态/会话/用量/最近 5 条消息） | v1 |

v1 之外的命令当前可显示 "not implemented in this build"，但**必须**被解析为命令而非作为普通消息发送。

### 9.2.1 命令搜索与补全（v1.1）

v1.1 在 v1 之上加两层命令发现：

| 入口 | 触发 | 行为 |
| --- | --- | --- |
| 内联补全 | composer 文本以 `/` 开头 | `CompletionPopup` 显示在 composer 上方；高亮当前匹配；`Tab` 接受，`Up`/`Down` 移动，`Esc` 关闭 |
| 命令面板 | `Ctrl+P`（v1.1 取代 Textual 默认行为） | 模态 `CommandPalette` 全屏；输入即搜索（模糊匹配 `short_label` 的子串）；`Up`/`Down` 移动；`Enter` 执行；`Esc` 关闭 |

实现要点：

- `xbotv2/tui/command.py:search_commands(query)` 是两层入口的唯一算法：
  - 文本以 `/` 开头时按 `spec.name` 的大小写不敏感前缀匹配（`rank 0`），降级到 `short_label` 的子串匹配（`rank 2`）。
  - 否则按模糊匹配：所有空白分词都必须在 `short_label` 中以子串出现。
  - `_SEARCH_ORDER = ("help", "clear", "status", "exit")` 保证默认顺序稳定。
- `complete_command(prefix)` 取 `search_commands` 的第一个匹配，用于 `Tab` 接受。
- `CompletionPopup` 故意**不**用 `rich.text.Text`（Textual 的 layout 阶段会调 `visual.get_height()`，Text 没有这个方法）；改用 `Container` + 每行一个 `Static`，状态用 CSS class `active` 标记。
- `CommandPalette` 复用了同一 `search_commands`；其选择也走 `app._handle_slash_command(spec)`，确保与"在 composer 中键入 `/help` + Enter"是**同一条代码路径**（包括 trace `tui.slash` 事件）。
- Textual 自带 `Ctrl+P` 的命令面板与 OpenCode 撞名；`XBotTextualApp.ENABLE_COMMAND_PALETTE = False` 显式关掉默认行为，让我们自己的 `CommandPalette` 接管。
- 关键不变量：composer 永远是唯一键盘焦点区；`CompletionPopup` 与 `CommandPalette` 自己处理键位、不抢焦点。

设计依据：OpenCode 的 `command_list = ctrl+p` 已经在 §2.3.1 调研列出；v1.1 与之对齐但**只暴露**我们自己的 4 个 slash 命令，避免 OpenCode 那种"全命令面板"对 v1 单 TUI 来说过重。

### 9.3 鼠标与可访问性

- **滚轮滚动 = 首要滚动方式**。不绑定 PageUp/PageDown。
- **不**默认"选中即复制"——这是与 OpenCode 最显著的差异。理由：
  1. 用户硬约束：XBotv2 文档 §3 第 20 条强调不抢文本选择。
  2. OpenCode 的"选中即复制"是 Win32/macOS 终端原生行为之上叠加的；许多用户在终端里会通过 OS 选区复制到剪贴板（macOS iTerm2 / WezTerm 都支持），劫持会破坏工作流。
- v1.1 可选提供 "shift+选中 → 复制" 的实验模式（在 `tokens.py` 中预留 `EXPERIMENTAL_COPY_ON_SELECT = False`）。
- 颜色对比：所有正文对比度 ≥ WCAG AA（4.5:1）。状态栏 dim 文字除外，但 ≥ 3:1。
- 不引入 attention 通知/音（OpenCode 的 `attention.*` 配置项 XBotv2 v1 不复刻）。

### 9.4 与 OpenCode 键位差异小结

| OpenCode | XBotv2 v1 | 原因 |
| --- | --- | --- |
| `PageUp/PageDown` 翻页 | 不绑定 | 约束 9：滚轮为主 |
| `<leader>+X` 系列 | 全部不引入 | 减面 |
| 命令面板（`Ctrl+P`） | `/` 前缀命令 | 约束 3：单流 |
| 主题切换 (`/themes`) | 不暴露 | 单主题 |
| 插件视图 | 不暴露 | 协议边界 |
| Tab/Shift+Tab 切 agent | 不暴露 | 单 agent |
| `Ctrl+R` 重命名 session | 不绑定 | 与 shell 冲突 |
| `Ctrl+T` 切 variant | 不暴露 | 单模型 |
| 选中即复制 | 不启用 | 保留原生选择 |
| `attention.*` 通知/音 | 不引入 | 减面 |

---

## 10. 协议集成

### 10.1 事件→Render log 映射

| 协议事件 | Render log 项 | 备注 |
| --- | --- | --- |
| `turn_started` | `activity` 起始 | mount 活动行 |
| `assistant_message`（非空） | `message.assistant` | 内容为空时**不**新增 message 行 |
| `assistant_message`（空） | （过滤） | 关联 tool_calls 仍以 `tool` 行呈现 |
| `tool_calls_started` | `tool` (pending) | 按 `tool_call_id` 去重 |
| `tool_result` | `tool` 状态更新 | 原地 `update()` |
| `usage` | `activity` + status bar | turn 用量累加；总用量覆盖 |
| `status` | status bar | 文本替换 |
| `client_message` | `notice.client_message` | 紫色 |
| `permission_request` | `notice.permission_request` | 含内联选项 |
| `permission_denied` | `notice.permission_denied` | 红色 |
| `user_input_required` | `notice.user_input_required` | 含内联选项（若提供） |
| `user_input_recorded` | `notice.user_input_recorded` | dim |
| `permission_response_recorded` | `notice.permission_response_recorded` | dim |
| `turn_finished` | `activity` finalize | 文案改 `done`/`failed` |
| `error` | `entry.error` | 红色 |
| `hello_ok` | （不渲染） | 仅更新 status |
| `session_ready` | （不渲染） | 仅更新 status / agent_name |
| `shutdown_ok` | （不渲染） | 更新 status |

### 10.2 用户→协议

| 来源 | 帧 | payload |
| --- | --- | --- |
| 普通消息 | `user.message` | `{"content": text}` |
| 权限回执 | `permission.response` | `{"request_id": ..., "decision": "allow"\|"deny", "scope": "once"\|"session"\|"always"}` |
| 提问回执 | `user.input` | `{"request_id": ..., "answer": ...}` |
| 会话握手 | `hello` / `session.open` | `TerminalSession.connect()` 处理 |
| 关闭 | `shutdown` | `TerminalSession.disconnect()` 处理 |

### 10.3 权限结果集与 OpenCode 的对照

| 协议域 | OpenCode 含义 | XBotv2 v1 含义 | 一致？ |
| --- | --- | --- | --- |
| `read`, `edit`, `glob`, `grep`, `bash`, `task`, `skill`, `lsp`, `question`, `webfetch`, `websearch`, `external_directory`, `doom_loop` | 同左 | 复用 | ✓ |
| ask 默认 | `ask` | `ask`（与 runtime 协议一致） | ✓ |
| `read` 对 `.env*` | 默认 `deny` | 同 | ✓ |
| ask 结果枚举 | `once` / `always` / `reject` | `once` / `session` / `always`（"deny" 是显式动作而非"ask 的结果"） | 偏 XBotv2（语义更贴） |
| `always` 作用范围 | 当前 OpenCode session 期间 | personality 级（写 personality 配置） | 偏 XBotv2（更持久） |
| `session`（XBotv2 新增） | — | 当前 XBotv2 session 期间 | 独有 |
| `doom_loop` 触发条件 | 同 input 三次重复 | 同 | ✓ |
| `external_directory` 默认 | `ask` | `ask` | ✓ |
| 工具通配语法 | `git *`, `*.env` | 复用 | ✓ |
| `~` / `$HOME` 展开 | 支持 | 支持 | ✓ |

UI 文案必须与运行时实际行为严格对应：

| UI 标签 | 实际 scope 值 | 实际行为 |
| --- | --- | --- |
| `Allow once` | `once` | 本次工具调用 allow |
| `Allow session` | `session` | 本次 XBotv2 session 内同 (tool, 模式) allow |
| `Always allow` | `always` | 写 personality 配置；以后同 (tool, 模式) allow |
| `Deny` | `deny` | 拒绝本次（与 ask 结果正交） |

不允许出现"标签=always 但实现=session"。

### 10.4 trace

环境变量 `XBOTV2_TUI_TRACE=<path>` 启用 JSONL 诊断。必须记录的最小事件集合：

| stage | 触发点 | 关键字段 |
| --- | --- | --- |
| `tui.submit` | `submit_composer()` | `text`, `repr`, `mode` |
| `tui.choice_confirm` | `confirm_active_choice()` | `request_id`, `payload`, `key` |
| `tui.choice_navigate` | 选择 Up/Down | `index`, `key` |
| `protocol.send` | `ProtocolClient.send()` | `frame`（完整） |
| `protocol.recv` | `ProtocolClient.read_frame()` | `frame`（完整） |
| `tui.render` | 任意 mount | `entry_kind`, `entry_key` |
| `tui.error` | `_record_error()` | `error` |

所有 JSONL 写入使用 `ensure_ascii=False`；写入失败静默吞掉但**不**影响主路径。

### 10.5 传输层（stdio → HTTP/SSE）

> 决策日期：2026-06-05。stdio JSONL 仍然保留作为测试与回放后端，但 v1 **默认传输**改为 HTTP + SSE，与 OpenCode 架构一致。

#### 10.5.1 为什么升级

| 维度 | stdio JSONL（现状） | HTTP + SSE（v1） |
| --- | --- | --- |
| 每帧成本 | `Process.stdin.write` + `drain` + `readline`（每次 syscall） | 单 TCP 连接 / turn；事件以 SSE 流式 push |
| 服务端可寻址 | 只能 spawn 子进程 | `http://127.0.0.1:4096`；可 SSH 隧道跨主机 |
| TUI 生命周期 | 与 server 强绑定（父死子亡） | TUI 可 attach/detach，server 长驻 |
| 调试 | `strace` / `tee` 进程 stdio | `curl` / `wscat` / 浏览器 devtools |
| 协议成熟度 | 自定义 | RFC 9110 + WHATWG SSE（成熟标准） |
| OpenCode 一致性 | 不一致 | 一致（OpenCode 内部即 HTTP + SSE） |
| 复杂度 | 极低 | 中（要管 server 进程/端口/认证） |

OpenCode 自身的 `thread.ts` / `worker.ts` 就是这个模式：worker 跑 server（Hono HTTP），TUI 通过 `createWorkerFetch`（进程内 fetch）或真实 HTTP 调用，事件流通过 `createEventSource`（SSE）。OpenCode 内部无论 transport 都是 HTTP 语义；我们采用同一思路但用 Python 生态的 `FastAPI` + `httpx`（见 §10.5.2 选型）。

#### 10.5.1.1 为什么不是 stdio——更深一层的理由

> 物理上 `stdin` / `stdout` 是两个独立 fd，理论可双向独立读写；stdio 本身**不强制**串行。但**当前 stdio JSONL 协议在设计上把一切塞进同一条线性事件流**，造成三个难以解决的问题：

1. **业务事件和控制请求共用同一条 stdout**
   - engine 事件（`turn_started` / `assistant_message` / ...）与 live interaction 帧（`permission_request` / `user_input_required`）由 server 端两个**并发**任务（engine event loop + live interaction sink）通过 `asyncio.Lock` 互斥写到同一 stdout。
   - 这导致：在一次 turn 进行中，**无法**再发一个"中断"或"切换模型"等控制命令——现有协议根本没有这种帧类型，stdio 也无法在不断流的情况下插入。
2. **多个 in-flight 请求无法并发**
   - 假设同一 session 允许多 turn 并行（OpenCode 实际不做，但 server 端做得到），stdio pipe 只能按到达顺序处理，无法把两个 turn 的事件流区分开。
   - HTTP + 多个 SSE 连接天然支持这种并发：每个 turn 一个独立请求 → 一个独立 SSE 响应。
3. **多 client attach 不可行**
   - stdio pipe 是 1-to-1 的；server 端只能服务一个 stdin/stdout。多个 client 想看同一 server 状态时，stdio 根本不支持。
   - HTTP 让 server 端按 n-to-1 共享状态，每个 client 独立开 SSE。

**这正是 OpenCode 选 HTTP + SSE 的根本原因**（不是性能）：

- `thread.ts` 的 `createWorkerFetch`（in-process 包装）即便在 worker 与 TUI 同进程时也走 HTTP 语义；
- 事件流用独立的 `createEventSource`（SSE），与控制请求**不在同一条流**；
- 取消 turn 走 `session.interrupt` 端点（keybind `escape`），是独立 POST，与当前 turn 的 SSE 流完全解耦。

**因此 v1 决策：stdio 完全移除**，不再做"stdio 向后兼容"。所有现有 stdio 测试需改写为 HTTP 集成测试。

#### 10.5.2 选型

| 角色 | 选 | 理由 |
| --- | --- | --- |
| Server | **FastAPI** | 依赖中已有 pydantic；FastAPI 自动 OpenAPI 文档；uvicorn 异步；社区大、SSE 文档丰富 |
| Client | **httpx** | async-native；与 FastAPI 同生态（`httpx` 是 FastAPI TestClient 底层）；HTTP/1.1+HTTP/2 都支持 |
| SSE 解析 | 手写（约 80 行） | SSE 协议足够简单；避免引入额外依赖 |
| 协议帧 | 现有 `ProtocolFrame` JSON | 内部 `payload` 不变，仅包一层 HTTP/SSE 头 |
| 鉴权 | **v1 不实现**；仅 `--bind 127.0.0.1`（loopback）；`--bind 0.0.0.0` 在 v1 **被拒绝**（v2 再加 Bearer Token） | 简化首版，依赖 OS 进程隔离保证安全 |

> 约束变更（v1 决定）：
> - **stdio 完全移除**。`--transport` 选项不再有；`xbotv2 --mode server` 永远是 HTTP；不存在 "stdio 兼容模式"。
> - **TUI 默认 HTTP**。`python main.py --mode tui` 通过本地 4096 与 server 通信；server 由 TUI 自动 spawn（除非 `--server URL` 指定远端）。
> - **不实现鉴权**。`--bind` 只能是 `127.0.0.1`；传 `0.0.0.0` 直接报错退出。
> - `ProtocolClient` / `RuntimeServer` 的 stdio 路径**不删除源码**（保留在 git 历史），但当前 v1 不再 import/调用。后续若做 IDE 嵌入式等场景再复活。

#### 10.5.3 HTTP API 规约

所有 endpoint 接受/返回 `application/json`，**除** SSE 通道为 `text/event-stream`。请求/响应体里的 `payload` 沿用现有 `ProtocolFrame.payload` 字段名（key 名稳定）。

| Method + Path | 用途 | 请求体 | 成功响应 |
| --- | --- | --- | --- |
| `GET /health` | 健康检查 | — | `200 {"status":"ok","version":"xbotv2.v1","uptime_s":42}` |
| `POST /hello` | 握手 | `{"client_name": "tui", "session_id"?, "thread_id"?, "personality_id"?}` | `200 {"server_name":"xbotv2","protocol_version":"xbotv2.v1","session_id","thread_id"}` |
| `POST /sessions` | 打开/恢复 session | `{"session_id","thread_id"}` | `200 {"agent_name","status":"ready"}` 或 `200 {"status":"recovered"}` |
| `POST /sessions/{sid}/messages` | 发送 user 消息，返回 SSE 流 | `{"content","request_id","client_ts"}` | `200 text/event-stream` (见 10.5.4) |
| `POST /sessions/{sid}/interactions/permission-response` | 权限回执 | `{"request_id","decision","scope"}` | `200 {"request_id","recorded":true,"pending_interactions":[...]}` |
| `POST /sessions/{sid}/interactions/user-input` | 提问回执 | `{"request_id","answer"}` | `200 {"request_id","recorded":true,"pending_interactions":[...]}` |
| `POST /sessions/{sid}/shutdown` | 关 session | — | `200 {"status":"closed"}` |

错误约定：

- `400 invalid_request` — 参数不合法
- `404 session_not_found` / `409 session_conflict`
- `410 interaction_no_longer_pending` — request_id 已不在 pending 集合
- `503 engine_busy` — session 正在处理别的 turn
- `5xx` — 服务端异常；body 必有 `{"code","message"}`

#### 10.5.4 SSE 事件流格式

`POST /sessions/{sid}/messages` 的响应是 `text/event-stream`，每条事件形如：

```
event: turn_started
id: <monotonic seq>
data: {"type":"turn_started","payload":{"turn":1}}

event: assistant_message
id: 2
data: {"type":"assistant_message","payload":{"content":"…","tool_calls":[…]}}

event: usage
id: 3
data: {"type":"usage","payload":{"delta":{…},"total":{…}}}

event: turn_finished
id: 4
data: {"type":"turn_finished","payload":{"turn":1}}

```

SSE 字段规约：

- `event`：与 `data.type` 同名（`turn_started` / `assistant_message` / `tool_calls_started` / `tool_result` / `usage` / `permission_request` / `permission_denied` / `user_input_required` / `client_message` / `error` / `turn_finished` 等）。
- `id`：单调递增的整型 seq；客户端断线重连时可携带 `Last-Event-ID` 头来从断点续传（best-effort，至少优于完全重头）。
- `data`：JSON 字符串；客户端按 `data` 一行解析（按 SSE 规范，`data:` 后多个连续的 `data:` 行合并为一个事件，content 以 `\n` 拼接——我们保证 server 端每个事件只用一行 `data:`，避免歧义）。
- 流结束：服务端写入 `event: end` `data: {"status":"ok"}` 后关闭；或流中某条事件的 HTTP 等价物 `event: error` 携带 code/message。

#### 10.5.5 客户端协议抽象

`xbotv2/tui/transport.py`（新模块）定义 `Transport` 协议类，concrete 实现分两个：

```
tui/
├── transport.py              # Transport Protocol
├── transport_stdio.py        # StdioTransport (现有 ProtocolClient 改名)
├── transport_http.py         # HttpTransport (新)
└── transport_factory.py      # 根据配置/CLI 创建 transport
```

接口（asyncio 视角）：

```python
class Transport(Protocol):
    async def hello(self, payload: dict) -> dict: ...
    async def open_session(self, session_id: str, thread_id: str) -> dict: ...
    def send_message(
        self, session_id: str, content: str, request_id: str,
    ) -> AsyncIterator[dict]: ...   # yields one event per SSE frame
    async def send_permission_response(
        self, session_id: str, request_id: str, decision: str, scope: str,
    ) -> dict: ...
    async def send_user_input(
        self, session_id: str, request_id: str, answer: str,
    ) -> dict: ...
    async def interrupt(self, *, session_id: str) -> dict: ...   # v1.2: ESC support
    async def shutdown(self, session_id: str) -> dict: ...
    async def close(self) -> None: ...
```

`TerminalSession`（`tui/terminal.py`）持有 transport；`send_message_with_input()` 把 transport 的事件流喂给现有 `input_provider` / `permission_provider` 回调，**完全不用改 `textual_client.py`**。

#### 10.5.6 服务端进程模型

`--mode server --transport http` 行为：

1. 启动 `aiohttp` app，绑定 `--bind 127.0.0.1 --port 4096`（默认）。
2. 进程内持有一个 `RuntimeServer` 形态的 `HttpRuntimeServer`（与现有 `RuntimeServer` 并列，共享核心调度逻辑，但**没有**子进程概念）。
3. 提供 `attach <url>` 子命令，模仿 OpenCode `opencode attach`：纯客户端，连接远端 server。
4. 优雅退出：收到 `SIGTERM` / `SIGINT` 时，等所有 in-flight SSE 流关闭（最长 2s）再退出。

`--mode server --transport stdio`（现有行为，不变）：保留为测试和回放后端。

#### 10.5.6.1 取消协议（v1.2：ESC → `POST /interrupt`）

按 ESC 取消正在运行的 turn 是 OpenCode `session_interrupt = escape` 的核心交互（§2.3.1）。完整链路：

```
TUI ESC key
  └─ action_clear_input()                  # textual_client.py:340
      └─ (turn_active or _turn_worker_running) → action_interrupt_turn()
          └─ run_worker(_do(), name="tui_interrupt", exclusive=False)
              └─ await self.session.transport.interrupt(session_id=...)  # Transport Protocol
                  └─ HttpTransport.interrupt → POST /sessions/{sid}/interrupt
                      └─ http_server.py: POST /sessions/{sid}/interrupt
                          └─ SessionContext.request_interrupt() → turn_task.cancel()
                              └─ Engine.run_turn 捕获 CancelledError
                                  └─ yield {"type":"turn_cancelled", "data":{...}}
                                      └─ Dispatcher._drain_engine_into_bus → SSE
                                          └─ HttpTransport 解析 SSE 帧
                                              └─ TerminalSession.send_message iterator
                                                  └─ TUI _collect_response
                                                      └─ state.apply_event("turn_cancelled")
                                                          └─ state.status = "Interrupted"
                                                      └─ _handle_stream_event("turn_cancelled")
                                                          └─ _refresh_status()  # 状态栏显示 "Interrupted"
```

关键设计点：

| 关注点 | 决策 |
| --- | --- |
| `Transport.interrupt` 同步返回 | 返回 `{"status": "interrupting", "cancelled": True}`；客户端不阻塞等 turn 真正结束，SSE 流自然关闭即可。 |
| TUI 状态先标 `Interrupting…` 再标 `Interrupted` | 客户端先把"我在请求中断"显式给用户（避免按 ESC 没反应的错觉），SSE 收到 `turn_cancelled` 后切到终态。 |
| 失败兜底 | 客户端 `try/except Exception` 吞 `interrupt()` 的网络错误（worker 不能 raise）；`_record_error` 加 `is_mounted` 守卫，避免 teardown 期间的 `NoMatches` 覆盖 `Interrupted`。 |
| `Engine.run_turn` 的 `CancelledError` 处理 | 必须 yield `turn_cancelled` **再** re-raise——只有这样 SSE 帧才能送出去；不 re-raise 则 dispatcher 不知道要关流。 |
| worker 命名 `tui_interrupt` + `exclusive=False` | 不与 `turn` worker 互斥（drain 仍要继续处理 `turn_cancelled` 事件）；命名唯一防止 ESC 连按叠加 worker。 |
| `BINDINGS` 不含 `escape` | OpenCode 的 ESC 走 `keymap.tsx` 中的 `session_interrupt` 显式注册，Textual 这边我们让 `ComposerTextArea._on_key` 在 `event.key == "escape"` 时先转发给 App。 |
| 测试 session 必须提供 `session_id` + `transport` | 缺一个就 AttributeError 被 `try/except Exception` 吞掉——按了 ESC 静默无反应。这条写进测试 docstring 防止回归。 |

#### 10.5.7 端口与绑址（v1：loopback only，无鉴权）

- 默认端口：`4096`（与 OpenCode 一致）。
- 默认绑址：`127.0.0.1`（loopback only；不暴露给 LAN）。
- **v1 不支持 `--bind 0.0.0.0`**：传 `0.0.0.0` 直接 `ValueError("remote bind not supported in v1")` 退出。v2 再加 Bearer Token。
- 依赖 OS 进程隔离保证安全：本机任意进程可连 `127.0.0.1:4096`，但这与现有 stdio 行为（任何本机用户都能看到 process 命令行）一致。
- 端口冲突：若 `4096` 被占用，server 启动失败抛错；TUI 不自动选端口（避免 magic）。

#### 10.5.8 TUI 端行为（v1：HTTP only）

`python main.py --mode tui` 默认行为：

1. 若传 `--server http://127.0.0.1:4096`，直接连接（适用于"server 已起"的场景，比如 `attach`）。
2. 否则 TUI 自动 spawn 一个 HTTP server 子进程（`xbotv2 --mode server`），等 `GET /health` 通后连接；TUI 退出时**不**自动 kill server（除非显式 `--shutdown-server-on-exit`）。
3. `--server URL` + `--shutdown-server-on-exit` 用于"用完即关"场景。
4. **不存在 stdio 路径**：TUI 永远是 HTTP 客户端。

`xbotv2.tui.TerminalSession` 接受 `transport: Transport` 参数；v1 只实现 `HttpTransport`，但 `Transport` Protocol 保留以备未来扩展。

#### 10.5.9 性能对比与目标

| 操作 | stdio 现状（实测/估算） | HTTP 目标 |
| --- | --- | --- |
| 一次 turn 含 5 个 tool call | ~50–80 ms 开销（10 次 pipe 往返 + 序列化） | < 20 ms（单 TCP + SSE push） |
| 中文长消息（4 KB） 提交 → 第一帧 | ~25 ms | < 10 ms |
| 端到端流式渲染第一个 token | 受 pipe 缓冲拖累 | SSE 即时 |

具体数字待 Phase E 完成后通过 benchmark 测得（见 §14.5）。

#### 10.5.10 实时事件 → TUI 状态同步

TUI 不仅在 `turn_finished` 时刷新状态栏/活动行；每个有"持续观测价值"的事件都立即驱动一次刷新。

| 事件 | 触发 | TUI 副作用 |
| --- | --- | --- |
| `turn_started` | Engine 准备开跑 | `state.turn_active = True`，`state.turn += 1`；status → `Running` |
| `usage{delta, total}` | 每次 LLM 调用结束（**不等** `turn_finished`） | `state.turn_usage` 累加 delta，`state.usage` 同步；`_update_activity()` + `_refresh_status()`；状态栏立即出现 `in:N out:M total:K req:R` |
| `tool_call` | LLM 决定调用工具 | 新增 `TuiToolEntry`，body 渲染 `name(args)`；活动行 status → `Running tool: name` |
| `tool_result` | 工具执行完毕 | 工具 entry 显示 stdout/stderr 全文（v2.5 全平铺） |
| `assistant_message` | LLM 流式文字片段 | 转录区 `MessageEntry` 追加（v1 不做 token-by-token 动画，只做行级追加） |
| `turn_finished` | 全部结束 | `state.turn_active = False`；活动行 status → `Idle` |
| `turn_cancelled` | ESC 中断或上游取消 | `state.turn_active = False`，`status = "Interrupted"` |
| `error` | 引擎/工具抛错 | `state.status = "Error"`，`state.errors` 追加；转录区插入红条 `TuiNotice(kind="error")` |
| `user_input_required` / `permission_request` | 工具需要用户输入 | placeholder 切到 `Answer the request…` / `Choose an inline approval option…`；活动行 status → `Awaiting user input` / `Awaiting permission` |

**实时刷新原则**：

- `usage` 事件**不**做 debounce；一个 turn 内 LLM 可能调用多次，token 计数应立刻反映在 status bar。
- 活动行（`#activity`）是 status bar 之外**唯一**展示"我在做什么"的地方；任何会让用户产生"卡住了？"疑问的事件之后都应被刷新。
- 转录区新内容用 `call_after_refresh` 延迟到布局完成后 `scroll_end`，避免 layout 阶段（`visual.get_height()` 调不到）出错。

**工具调用错误特别处理**：

LangChain 端在协议不匹配时（如 `An assistant message with 'tool_calls' must be followed by tool messages`）会 yield `error` 事件。TUI 的职责**只是显眼展示**——这不是 TUI bug，是 Engine/LangChain 端协议 bug。表现：

- 转录区出现 `TuiNotice(kind="error", text="An assistant message with 'tool_calls' must be followed by tool messages…")`。
- status bar 切到 `Error` 红字。
- 该 turn 结束，composer 重新可输入。
- 用户可继续提交新 turn；如果反复出现，建议切到 mock provider 或 `XBot_PROVIDER=mock` 排除 Engine bug。

---

## 11. 国际化（i18n）

- 文案默认 **英文**（与 OpenCode/CLI 习惯一致），但所有面向用户的字符串必须经过 `i18n.t("...")` 包装。
- v1 必备 `en`、`zh-CN` 资源。
- 不做运行时语言切换（TUI 内不暴露），但文案资源路径必须支持将来 CLI 加 `--lang`。
- **关键**：composer 内 IME 输入的中文/多字节字符，必须以**字符串**而非"按字节"被处理：
  - 不在 TUI 内做 length-based 截断用户文本。
  - 时间戳格式 `HH:MM:SS` 与语言无关。
  - 日志/trace 使用 UTF-8 全程。

---

## 12. 性能与可扩展性

- 渲染：流上 `mount` 是 O(新条目数)；存量条目只 `update()`。
- 状态栏：`update()` 在 timer(0.5s) 之外只增不频繁。
- 历史：`_input_history` 在客户端进程内，进程退出即丢弃（不持久化；服务端 `messages.jsonl` 仍是真相）。
- 大工具结果：截断到 120 字符；完整内容在服务端 artifact 中（`tool_result_cached` 事件）。
- 回放压力：1000 条消息下首屏布局 < 200ms（粗略目标，需用 SVG 截图测试验证）。
- 大文件结构：参照 OpenCode 的"一个文件一个职责"约束，单文件 ≤ 600 行；`textual_client.py` 844 行必须按 7.1 节拆分。

---

## 13. 验证策略（DoD）

### 13.1 静态 / 协议回放

1. **空 assistant 不渲染消息行** — 单元测试。
2. **空 assistant 中的 tool_calls 仍能渲染** — 单元测试。
3. **同一 `request_id` 的回执只发一次** — 单元测试 + 协议 spy。
4. **CHOOSING 中 Enter 触发 `confirm_active_choice` 而非 `submit_composer`** — 单元测试。
5. **CHOOSING 中字符键不改变 composer 文本** — 单元测试（`ComposerTextArea._on_key` 阻止）。
6. **scope 解析** — 单元测试覆盖 `allow once` / `always allow` / `session deny` 等。
7. **mode 转换图** — 状态机所有合法/非法转移都被覆盖。

### 13.2 SVG/PIL 渲染快照

通过 Textual `App.export_screenshot()` 截屏（SVG），断言：

- 状态栏元素全部出现。
- 流的第 N 条消息文本与回放序列一致。
- 没有任何"空白 assistant 行"出现在 DOM 中。
- 内联选择行按选中态反白。
- `CHOOSING` 模式下 composer 不存在（`display:none`）。

### 13.3 真机手测

记录在 `docsv2/verification/tui-vYYYYMMDD.md`，至少覆盖：

| 用例 | 期望 |
| --- | --- |
| 中文直接键入（IME） | 流上 "You" 行字节级一致；`XBOTV2_TUI_TRACE` 中 `tui.submit.text` 与 `protocol.send` 中 `user.message.content` 字节级一致。 |
| 多行输入 | Shift+Enter 换行，Enter 发送。 |
| `up`/`down` 历史 | 空文本时上下浏览；非空时不抢光标。 |
| 权限决策 | Up/Down 切换、Enter 提交；二次 Enter 不重发。 |
| 滚轮 | 上下滚动；停留中部时新条目出现"↓ N new"。 |
| 文本选择（macOS Terminal、iTerm2、WezTerm） | 鼠标拖选文本应进入 OS 选区，不被劫持。 |
| 错误注入 | 在回放中插 `error` 帧，UI 出现红色错误行，状态栏 `●Error`。 |
| `/help` `/status` `/clear` `/exit` | 命令解析正确，回执符合预期。 |

### 13.4 trace 诊断剧本

中文输入失效时按下列顺序比对：

1. `tui.submit.text` 是否已被 IME 正确提交？
2. `protocol.send` 中 `user.message.content` 与上一步是否一致？
3. `protocol.recv` 中 `assistant_message.content` 与上一步是否一致？
4. 渲染层用 `markup=False` 渲染？

定位规则：

- 步骤 1 坏 → Textual/终端 IME 通道问题。
- 步骤 1 好，2 坏 → 客户端序列化。
- 2 好，3 坏 → 服务端回声链路。
- 3 好，4 坏 → 渲染。

### 13.5 与 OpenCode 一致性自检

| 项 | OpenCode | XBotv2 | DoD 检 |
| --- | --- | --- | --- |
| 流是单一时间线 | 是（外加 dialog 覆盖） | 是（无 dialog） | SVG 检查 |
| 状态栏存在 | 是（compact） | 是 | SVG 检查 |
| 消息可滚动 | 是 | 是（鼠标） | 手测 |
| 选择不抢焦点 | 是 | 是 | 手测 |
| 选中文本不自动复制 | 否（OpenCode 默认复制） | 是（按 §3 第 20 条） | 手测 |
| 时间戳 | 是 | 是 | SVG 检查 |
| spinner / 活动指示 | 是 | 是 | 录屏 |
| 权限 ask 三选项 | once/always/reject | once/session/always | 回放测试 |
| 中文 IME | 默认支持 | 默认支持 | trace 对齐 |
| 配置 schema | 30+ 字段 | 单 `tokens.py` | 静态 |

---

## 14. 实施计划

> 路径以"先窄后宽"组织：先把单流 + 紧凑状态栏 + 内联选择 + 必要 trace 做对，再做命令、键位可配、主题；**传输层升级（Phase E）是 v1 的硬性前置**——只有 transport 抽象出来后，§7.1 的文件拆分、§13 的回放测试、§10.4 的 trace 完整性才能有干净测试点。

### Phase A — 收敛（**部分已完成**）

> 进度（2026-06-05）：已落地 `mode.py` / `command.py` / status bar markup 修复 / `Allow once` 标签；其余项继续推进。

1. 统一 Render log 数据结构 `RenderItem(kind, key, ts, payload)`，覆盖 7.3-7.6 所有类型。
2. 引入 `ModeController` 显式持有 `Mode`；删除散落的 `if self._choice_mode_active()` 反复判断。**【已部分完成：`_current_mode()` 已存在；未替换所有散落判断】**
3. `confirm_active_choice` 与 `submit_composer` 单一入口；`_submitted_interaction_ids` 唯一去重集合。
4. 移除"双 mount"路径（活动行/工具行不应与 transcript 各自 mount；统一进 render log）。
5. 所有 `Static(body, ..., markup=True)` 改为 `markup=False`，避免用户输入被当标记。**【已完成：status bar 与所有 body Static 均为 markup=False】**
6. 拆分 `textual_client.py` 为 §7.1 列出的多文件。

### Phase B — 可诊断

1. trace 覆盖 10.4 全表。
2. 增加 `TUI_DEBUG_DUMP=<path>`：定期把 `state.to_dict()` 写入 JSON，便于在 CI 中 diff。
3. 一组 replay fixture：用户消息 → 工具调用 → 权限询问 → 工具结果 → 最终回复。
4. SVG 截图测试。

### Phase C — 可用

1. 斜杠命令：v1 四个。**【已完成：`/exit` `/clear` `/help` `/status` 全部实现】**
2. 键位表集中到 `command.py`；为 v2 的 JSON 化预留接口。**【v1.1 已完成 `search_commands` / `complete_command` 集中】**
3. 滚轮"末尾跟随"行为 + "↓ N new" 提示。**【v1.2 决定：用户偏好"全平铺、无内嵌滚动"，整体 transcript 滚到末尾，**不**实现 ↓ N new 浮动提示。】**
4. 主题变量化（不暴露切换）。

### Phase F — 命令搜索与补全（v1.1，**已完成**）

> 详见 §9.2.1。本阶段落地 4 个未完成项：内联补全、命令面板、bug 修复、真实交互测试。

1. **command.py 扩展**（`xbotv2/tui/command.py`）
   - `CommandSpec.short_label`：紧凑短描述，供 popup / palette 渲染。
   - `_SEARCH_ORDER`：保证 4 个命令的展示顺序稳定（help/clear/status/exit）。
   - `search_commands(query)`：slash 前缀 + 模糊匹配；返回 `list[CommandSpec]`。
   - `complete_command(prefix)`：Tab 接受的最佳匹配。
2. **CompletionPopup**（`xbotv2/tui/completion_popup.py`）
   - `Container(Vertical)` + 每行 `Static`；高亮态用 `active` CSS class。
   - **不**用 `rich.text.Text`（Textual layout 调 `visual.get_height()` 失败）。
3. **CommandPalette**（`xbotv2/tui/command_palette.py`）
   - `ModalScreen`，`Input` + 候选列表。
   - `Ctrl+P` 触发；`Up`/`Down` 移动；`Enter` 走 `app._handle_slash_command`。
   - 关闭 Textual 默认 `Ctrl+P` 命令面板（`ENABLE_COMMAND_PALETTE = False`）。
4. **composer 端接线**（`xbotv2/tui/textual_client.py`）
   - composer `Tab` / `Up` / `Down` / `Esc` 在 popup 可见时被拦截。
   - `_cmd_help` 每条命令独立一行（之前是 `"  "` 拼接，挤一行）。
   - `_current_mode` 改名 `_current_tui_mode`（避免与 Textual `App.current_mode` 冲突）。

### 完成定义（v1.1，§9.2.1）：
- [x] `search_commands` / `complete_command` 单元测试覆盖 slash + 模糊两种模式。
- [x] 真实交互测试通过 Textual `Pilot` 验证 popup/palette/中文/clear/status。
- [x] 错名 bug 修复（`_current_mode` → `_current_tui_mode`）。
- [x] Textual 内建 `Ctrl+P` 命令面板被关闭，v1.1 自己的 `CommandPalette` 接管。

### Phase D — 扩展

1. `/model` / `/theme` / `/sessions`。
2. 键位 JSON 配置（`tui.json` 风格，与 OpenCode 同形）。
3. 回放与脚本化（`--replay fixtures/xxx.jsonl`）用于无 LLM 演示与回归。

### Phase E — HTTP/SSE 传输（**v1 决定：FastAPI + httpx，stdio 完全移除**）

> 详见 §10.5。**本阶段完成后所有 stdio 路径从 import 树消失**；现有 32 个 stdio 测试需要全部改写为 HTTP 集成测试（用 `httpx.AsyncClient` + `httpx.ASGITransport` 跑 FastAPI app）。
>
> 进度（2026-06-05）：**已完成**。`f6a5b13` FastAPI 服务端 + dispatcher 骨架；`583f6eb`（前）slash 命令 + mode + markup；`d0b7c9f` CLI 改造 + stdio 测试删除；当前 commit 加 bench 与验证文档。288/288 测试通过；50 turn 平均 3.6ms / p95 5.5ms（见 `docsv2/verification/transport-bench-v20260605.md`）。

1. **加依赖**（`pyproject.toml`）：`fastapi` / `uvicorn[standard]` / `httpx`。**【已完成】**
2. **HTTP 服务端**（`xbotv2/protocol/http_server.py`）：FastAPI app + dispatcher。**【已完成】**
3. **HTTP 客户端 + Transport 抽象**：Transport Protocol + HttpTransport + TerminalSession 改写。**【已完成】**
4. **删除 stdio 路径**：`xbotv2.tui.terminal` 不再有 `ProtocolClient`；`__init__.py` 不再 export。**【已完成，CI grep 验证】**
5. **CLI 改造**（`xbotv2/__main__.py`）：`--mode server` 启 uvicorn；`--mode tui` auto-spawn + 连接；`--bind 0.0.0.0` 报错；`attach <url>` 子命令。**【已完成】**
6. **测试改写**：stdio 12 个 subprocess 测试删除；新增 8 个 HTTP 集成测试。**【已完成】**
7. **回放与 bench**：`tests/bench/test_http_latency.py` + `docsv2/verification/transport-bench-v20260605.md`。**【已完成】**

### 不在 v1

- 右侧面板、命令面板、主题切换 UI、插件视图、多 session 并列、leader key、attention 通知/音、选中即复制、diff 内联、滚动加速、which-key 弹层。
- mTLS、双向认证、服务端 metrics/Prometheus 端点（v2 候选）。
- 多 server 集群 / 负载均衡（v3+ 候选）。

---

## 15. 风险与权衡

| 风险 | 缓解 |
| --- | --- |
| 终端兼容差异（iTerm2 / WezTerm / Windows Terminal / tmux） | 在 CI 中跑 Textual 自带 `TestPilot`；不依赖任何图形/真彩能力；用 256-color 回退色名而非 #hex。 |
| 中文 IME 在某些终端下被劫持 | trace 步骤 1 优先；提供 "type a-z then submit" 的对比用例。 |
| 大消息/长工具结果导致布局抖动 | 截断到 120 字符（可见），完整文本放 artifact；流不做回填。 |
| Textual `TextArea` 在多行下 IME 失灵 | v1 默认高度 3；v1.1 再扩展多行。 |
| 协议扩展前缀 `__meta__:...` 与未来语义冲突 | 在 `protocol.md` 集中登记；与 OpenCode 不共享。 |
| 单流布局在大屏浪费空间 | 不补右侧面板；用更宽松的横向内边距 + 状态栏扩展字段填补信息密度。 |
| OpenCode 功能过强，分支容易回流 | 实施计划按 Phase A→D 推进；不在 v1 内引入"看起来很 OpenCode"的多余元素。 |
| "不抢文本选择"被用户期望为"有" | 在 `tokens.py` 中预留 `EXPERIMENTAL_COPY_ON_SELECT = False`，v1.1 用 `Shift+选中` 实验。 |
| 单文件 `textual_client.py` 体积膨胀 | §7.1 拆分后单文件 ≤ 600 行；CI 加 line count 检查。 |

---

## 16. 验收清单（DoD）

满足下列全部项即视为 v1 完成：

UI / 状态机（Phase A、C）：

- [ ] 流是单一时间线；无右侧面板。
- [ ] `CHOOSING` 时 composer 隐藏且不接收键。
- [ ] 同一 `request_id` 回执只发一次。
- [ ] 空 assistant 不渲染空消息行。
- [ ] 中文键入/提交/显示字节级正确（trace 对齐）。
- [ ] 鼠标滚轮可滚动；不抢文本选择。
- [ ] 状态栏始终显示，单行，包含 §7.1 全部分段。
- [ ] 每条消息有时间戳（行内显式或同秒聚合）。
- [ ] 活动行有 spinner / elapsed / 用量。
- [ ] 状态机有显式 `Mode` 与去重集合；不存在散落状态判断。
- [ ] `XBOTV2_TUI_TRACE` 记录 §10.4 中全部 stage。
- [ ] 关键交互均有 SVG 截图回归。
- [ ] 无 `_render_new_transcript_entries` / `_append_activity` 双轨 mount；统一进 Render log。
- [ ] 所有 `Static(..., markup=...)` 用 `markup=False` 渲染用户文本。
- [ ] 提交/提交路径与 OpenCode 风格一致：单 composer、底部一行、状态栏不抢戏。
- [ ] `textual_client.py` 拆分为 §7.1 所列多文件。
- [ ] 权限 ask UI 提供 `Allow once` / `Allow session` / `Always allow` / `Deny` 四项，scope 与运行时严格对应。
- [ ] `/exit` `/clear` `/help` `/status` 四个 slash 命令工作。
- [ ] 不引入 leader key、命令面板、plugin slot、theme switcher。

传输层（Phase E，**v1 必过**）：

- [ ] `pyproject.toml` 加入 `fastapi` / `uvicorn[standard]` / `httpx`。
- [ ] `xbotv2 --mode server` 启动后 `GET /health` 返回 200。
- [ ] `xbotv2/tui/transport.py` 定义 `Transport` Protocol（接口见 §10.5.5）。
- [ ] `xbotv2/tui/transport_http.py` 实现 `HttpTransport`（`httpx.AsyncClient`）；手写 SSE 解析；`Last-Event-ID` 重连；UTF-8 全程。
- [ ] `xbotv2/protocol/http_server.py` 暴露 §10.5.3 的 endpoints；`GET /health` 返回 200；错误码按 §10.5.3 错误约定。
- [ ] 端到端：TUI → HTTP server → engine → SSE → TUI 完成一个含 tool call + permission 的 turn。
- [ ] 中文消息在 HTTP 通道下 trace 对齐（与 stdio 历史数据字节级一致）。
- [ ] 全部 stdio 测试改写为 HTTP 测试并通过（目标 32 → 35+）。
- [ ] `--bind 0.0.0.0` 启动失败并提示 "remote bind not supported in v1"。
- [ ] `xbotv2 attach <url>` 子命令工作。
- [ ] bench 结果记录到 `docsv2/verification/transport-bench-vYYYYMMDD.md`。
- [ ] 旧 `ProtocolClient`（stdio）从 import 树消失（源码可在 git 历史恢复，CI grep 不到 `xbotv2.tui.terminal` 的 stdio 引用）。

---

## 17. 变更记录

- v1（2026-06-05）：从"以审计/批评为主"重写为"以设计/规约为主"的可执行规范。
- v2（2026-06-05）：补充对 OpenCode 仓库的逐项调研——目录结构、`app.tsx` 1113 行结构、`keymap.tsx` 实现、`thread.ts` 启动流、`win32.ts` 平台处理、`event.ts` 类型、官方 docs 完整键位/命令/权限表；据此修正权限 ask 枚举（once/session/always 而非 once/always/reject），明确不引入 leader key、不引入选中即复制、不引入 plugin/slot。
- v2.1（2026-06-05）：
  - **Phase A 部分落地**（commit 583f6eb）：`mode.py`（显式 `Mode` 枚举）、`command.py`（4 个 v1 slash 命令 + 别名 + unknown 分类）、status bar 切到 `markup=False` + `Text` 渲染器、内联权限选择 `Allow` → `Allow once` 对齐 §10.3。
  - **新增 Phase E：HTTP/SSE 传输**（§10.5）：stdio 子进程仍保留（向后兼容测试），v1 默认推荐 HTTP；选 `aiohttp` 作为 server+client 单依赖；新增 §10.5.3 endpoint 表、§10.5.4 SSE 帧格式、§10.5.5 `Transport` Protocol、§10.5.7 鉴权与端口约定；§14 实施计划补全 Phase E 七步；§16 DoD 拆分为"UI/状态机"和"传输层"两组。
- v2.2（2026-06-05）：根据用户反馈**推翻 v2.1 的 stdio 保留决定**：
  - 选型改 **FastAPI + httpx**（`pyproject.toml` 加 `fastapi` / `uvicorn[standard]` / `httpx`）。
  - **stdio 完全移除**：不是"先兼容再升级"，而是从 v1 起 TUI 就是 HTTP-only；现有 stdio 测试需全部改写为 HTTP 集成测试。
  - **v1 不做鉴权**：`--bind` 只能是 `127.0.0.1`，传 `0.0.0.0` 报错退出。
  - **新增 §10.5.1.1**：把"为什么 HTTP 而不是 stdio"的根因（业务事件与控制请求共享 stdout → 串行化；多 in-flight 请求无法并发；多 client attach 不可行；OpenCode 同因）写进文档，作为后续回归的判断依据。
  - §10.5.7/§10.5.8/§14 Phase E/§16 全部按以上三点重写。
- v2.3（2026-06-05）：Phase E 落地完成。
  - `f6a5b13` FastAPI 服务端 + dispatcher。
  - `d0b7c9f` CLI 改造 + stdio 测试删除 + attach 子命令。
  - `transport.py` + `transport_http.py` 实现；`xbotv2.tui.terminal` 不再导出 `ProtocolClient`。
  - 288/288 测试通过；50 turn 平均 3.6ms / p95 5.5ms（详见 `docsv2/verification/transport-bench-v20260605.md`）。
  - `__main__.py` 改造：`--mode server` 启 uvicorn；`--mode tui` auto-spawn + 连接；`--bind 0.0.0.0` 报错；`attach <url>` 工作。
- v2.4（2026-06-05）：Phase F 命令搜索与补全（v1.1，**用户报告**）。
  - 修复 `_collect_response` 还在调用 `send_message_with_input`（Transport 重命名后留下的 bug），导致每条用户消息都报 `AttributeError`，状态栏显示 "Error" —— 用户实际报的 bug。
  - 修复 `CompletionPopup` 在 `compose()` 中被使用但未 import（用户报的二次 NameError）。
  - 修复 `_cmd_help` 用 `"  "` 拼接，挤一行；改为 `\n` 分行。
  - 新增 `xbotv2/tui/command.py:search_commands` / `complete_command`（slash 前缀 + 模糊两种模式）。
  - 新增 `CompletionPopup`（Tab/Up/Down/Esc，**用 `Container`+`Static` 不用 `Text`**，因为 Textual layout 阶段会调 `visual.get_height()` 而 `Text` 没有这个方法）。
  - 新增 `CommandPalette`（`Ctrl+P` 触发，模糊搜索；显式 `ENABLE_COMMAND_PALETTE = False` 关掉 Textual 自带的面板，**避免和 OpenCode 风格撞名**）。
  - `_current_mode` 改名 `_current_tui_mode`，与 `App.current_mode`（Textual mode 系统的字符串属性）解耦。
  - 真实交互测试 15 个：completion popup 显隐、Tab 接受、Esc 关闭、中文 IME 端到端、`/help` 独立分行、未知命令提示、`/clear` 保留 session、`/status` 输出 mode 字段、`Ctrl+P` 打开 CommandPalette。
  - 324/324 测试通过。
- v2.5（2026-06-05）：**用户要求"全平铺无内嵌滚动"**。
  - 修复 body widget 渲染：`_entry_widget` 改为显式构造 `rich.text.Text` 并传给 `Static`（避免 `markup=False` 在 Textual 0.86 某些布局路径下让 body 不可见但 Ctrl-V 仍可复制的 bug）。
  - 修复 `_preview`：不再把 `\n` 替换为空格，工具结果保持多行（如 `df -h` 输出）。
  - 修复 CSS：`.entry` / `.body` 显式 `height: auto; width: 1fr`，单条 entry 自然撑到全文高度，不带内嵌滚动条。
  - **回退**工具结果 8 行截断 + "↓ N new" 浮动提示（用户明确反对内嵌控件）。
  - transcript 仍然 `VerticalScroll` 整体可滚；新内容通过 `call_after_refresh` 延迟到布局完成后 `scroll_end`。
  - 新增 `test_long_body_does_not_truncate_or_inner_scroll`：40 行消息 + 40 行工具结果，断言 state 保留全文 + DOM 无 `VerticalScroll` 子节点 + body 渲染 `visual.plain` 包含首/中/尾行。
  - 327/327 测试通过。
- v2.6（2026-06-05）：**ESC interrupt + 实时 token usage + 工具调用错误展示**（用户报的两个增强需求 + 一个长期隐患）。
  - **ESC interrupt 完整链路落地**（§10.5.6）：
    - `Transport.interrupt(session_id=...)` 写入 `Transport` Protocol；`HttpTransport.interrupt` → `POST /sessions/{sid}/interrupt`。
    - FastAPI `POST /sessions/{sid}/interrupt` 调用 `SessionContext.request_interrupt()`，后者 `turn_task.cancel()`。
    - `Engine.run_turn` 捕获 `asyncio.CancelledError` 后 yield `turn_cancelled` 事件再 re-raise；`Dispatcher._drain_engine_into_bus` 把事件送入 SSE 队列。
    - TUI `BINDINGS` 没有 ESC 绑定；走 `ComposerTextArea._on_key` 路径 → `action_clear_input` 检查 `state.turn_active or _turn_worker_running` → `action_interrupt_turn` → `run_worker(_do(), name="tui_interrupt", exclusive=False)`。
    - TUI 状态机：`turn_cancelled` 事件 → `TuiState.apply_event` 设 `status = "Interrupted"`；`_handle_stream_event` 同步再设一次 + `_refresh_status()`。
  - **实时 token usage**（§10.5.9）：Engine 在 LLM 调用结束时 yield `usage{delta, total}` 事件；TUI 在 `_handle_stream_event` 中调 `_update_activity()` 与 `_refresh_status()`，**不等** `turn_finished`。这样 status bar 每一轮都能看到 `in:N out:M total:K`，且活动行实时翻牌。
  - **`_record_error` 加 `is_mounted` 守卫**：`turn_cancelled` 之后的 UI 刷新会和 teardown 抢 DOM（headless 测试或 Ctrl-C 退出时容易撞上 `NoMatches`），不让这种"事后 DOM miss"覆盖 `"Interrupted"` 状态。
  - **测试加固**（`tests/integration/test_tui_interrupt_and_usage.py`，3 个新测试）：
    - `test_esc_during_running_turn_calls_transport_interrupt`：按 ESC 后断言 `session.interrupt_calls == ["s"]`。
    - `test_turn_cancelled_event_drives_status_to_interrupted`：断言 `state.status == "Interrupted" && state.turn_active is False`。
    - `test_usage_event_updates_status_bar_in_realtime`：断言 status bar 提前出现 `in:100 out:25 total:125`，不等 `turn_finished`。
  - **测试 session 形状**：`tests/integration/test_tui_interrupt_and_usage.py` 的 `_InterruptibleSession` 必须提供 `session_id`、`transport` 两个属性——TUI 走 `self.session.transport.interrupt(session_id=self.session.session_id)`，缺一个就 AttributeError 静默被 worker 的 `except Exception` 吞掉，导致"按了 ESC 啥也没发生"假象。这一条写进 docstring 防止后续回归。
  - **引擎侧 tool call 错误**：LangChain 端在 `An assistant message with 'tool_calls' must be followed by tool messages` 这类 400 时会 yield `error` 事件；`TuiState.apply_event("error")` 把消息塞进 `state.errors` 并设 `status = "Error"`；转录区会插入一条红条 `TuiNotice(kind="error", text=...)`。**注意**：这是 engine 端协议 bug，TUI 只负责显眼展示。
  - 337/337 测试通过（324 + 5 tool dispatch timeout + 3 interrupt/usage + 5 long-body 散落）。
- v2.7（2026-06-05）：**代码 review 后修复**（见 `docsv2/code_review_v1.2.md`）。
  - **P0 修复**：
    - `engine.py:601`：LLM `ainvoke` 加 `asyncio.wait_for(..., timeout=120s)`；超时 yield error 事件。
    - `engine.py:230`：`InteractionDisconnected` handler 补 `_backtrack_orphan_tool_calls()`，与 `CancelledError` 路径一致。
    - `dispatcher.py:84-91`：`request_interrupt()` 用局部变量 `task` 快照消除 TOCTOU 竞态。
    - `dispatcher.py:290-307`：删除 `_drain_engine_into_bus` 中重复的 `turn_cancelled` 合成（Engine 已产出），去重。
  - **P1 修复**：
    - `textual_client.py:96-146`：删除重复 CSS 块（`#transcript`/`.entry`/`.meta`/`.body` 各定义了两次）。
    - `textual_client.py:532`：新增 `_safe_query_one()` helper（`is_mounted` + `try/except NoMatches`），`_accept_completion` 和 `_append_activity` 改用此方法。
  - **P2 修复**：
    - 删除死代码：`_status_badge()`（textual_client.py）和 `MODE_BADGE`（mode.py）。
    - `hooks/types.py`：删除本地 `SessionInfo` 重复定义，统一 import `xbotv2.core.state.SessionInfo`。
    - `engine.py:841`：`turn_finished` 处追加 `_backtrack_orphan_tool_calls()` 作为一致性守卫（19 个早期退出点不再有遗漏风险）。
  - 345/345 测试通过。
- v2.8（2026-06-05）：**交互体验三连修**（用户报告：权限 UI 不弹、usage 不刷新、thinking 不渲染）。
  - **权限 UI 不弹（根因）**：`terminal.py:_send_message_impl` 在收到 `permission_request` 时先调 `permission_provider` 再 `yield event`——provider 阻塞，TUI 永远看不到 `apply_event("permission_request")`，status bar 一直 "Running"，inline 选择永远不会出现。**修复**：对 `permission_request` 和 `user_input_required` **先 yield 再 block**。
  - **usage 不刷新（根因）**：Engine 发送 `{"type":"usage","data":{"input_tokens":N,...}}`（flat format），但 `TuiState._apply_usage` 只认 `data.delta` + `data.total` 子键格式。`data.delta` 为 None 导致 `turn_usage` 永不加和，activity row 始终显示 0。**修复**：`_apply_usage` 在没有 `delta` 子键时用 flat data 本身作为 delta 累加。
  - **thinking 不渲染（根因）**：`apply_event("assistant_message")` 在 `content.strip()` 为空时不写入 transcript。当 LLM 返回纯 tool_calls（无 content 文本）时，transcript 无任何提示，用户看到工具 pending 但不知道为什么。**修复**：若有 tool_calls 但 content 为空，插入 `"Thinking…"` 占位。
  - 测试 +5（permission 状态、usage 累加 ×2、thinking 占位 ×2），350/350 通过。
- 后续：每条设计变更都更新本文件相应章节，并提交到 `docsv2/tui_opencode_requirements.md`。

---

## 附录 A — OpenCode 文档与源码完整引用

| 引用 | 链接 |
| --- | --- |
| TUI 文档 | `https://opencode.ai/docs/tui/` |
| Keybinds 文档 | `https://opencode.ai/docs/keybinds/` |
| Permissions 文档 | `https://opencode.ai/docs/permissions/` |
| TUI 目录树 | `https://github.com/anomalyco/opencode/tree/dev/packages/opencode/src/cli/cmd/tui` |
| `app.tsx` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/app.tsx` |
| `keymap.tsx` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/keymap.tsx` |
| `thread.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/thread.ts` |
| `worker.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/worker.ts` |
| `attach.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/attach.ts` |
| `event.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/event.ts` |
| `win32.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/win32.ts` |
| `validate-session.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/validate-session.ts` |
| `layer.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/layer.ts` |
| `config/tui.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/config/tui.ts` |
| `config/keybind.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/config/keybind.ts` |
| `config/tui-schema.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/config/tui-schema.ts` |
| `context/route.tsx` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/context/route.tsx` |
| `context/theme.tsx` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/context/theme.tsx` |
| `util/clipboard.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/util/clipboard.ts` |
| `util/selection.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/util/selection.ts` |
| `util/transcript.ts` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/util/transcript.ts` |
| `routes/home.tsx` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/routes/home.tsx` |
| `ui/dialog.tsx` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/ui/dialog.tsx` |
| `ui/dialog-select.tsx` | `https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/tui/ui/dialog-select.tsx` |
