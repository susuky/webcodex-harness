# WebCodex Harness

English | [Traditional Chinese](README.zh-TW.md)

Connect an MCP client to repositories and desktop applications on your own machines. A self-hosted Server manages authentication and saved tasks; outbound Runners execute tools on the machines you register.

## What it does

- Read, search and edit project files; use Git, commands, tests and interactive terminals.
- Import original image and GIF files from ChatGPT into a project.
- Resume saved tasks across conversations with `task_id`, including running commands and their output.
- Create or register projects under explicitly allowed parent directories.
- Load project skills and plugins with persistent MCP connections.
- Add desktop capture and control, semantic control selection, or browser automation through optional dependencies.

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git and ripgrep (`rg`). Run from this source checkout:

```sh
uv sync --locked
uv run webcodex init --project /absolute/path/to/your/repo
```

On Windows, use a project path such as `'C:\src\my-repo'`. Configuration is stored outside the project in `~/.config/webcodex-harness/` or `%LOCALAPPDATA%\webcodex-harness\`. `init` refuses to overwrite an existing configuration.

To start without an existing project, use `init --project-root /absolute/path/to/projects` with an existing parent directory. For an existing installation, add a parent directory with `uv run webcodex allow-root /absolute/path/to/projects`, then restart Server and Runner. See [Project setup](docs/PROJECTS.md).

Start each command in its own terminal:

```sh
uv run webcodex serve
uv run webcodex runner
```

Connect your MCP client to `http://127.0.0.1:8765/mcp` using the Bearer token in the generated `client.json`, or use `uv run webcodex bridge` for stdio. Follow [Connections](docs/CHATGPT.md) for ChatGPT and other clients, and [Operations](docs/OPERATIONS.md) to create your first task.

To try the command workflow from the same installation:

```sh
uv run webcodex call projects_list
uv run python examples/same_turn_demo.py --project local/project
```

The example creates a task, runs a command, reads its output and saves the result. You can open the task in `/manage`. See [the walkthrough](docs/DEMO.md).

### Desktop and browser tools

Desktop control requires the optional `computer` dependencies and explicit permissions. It operates the desktop on the task's Runner machine. To control Windows, start a native Runner in that Windows interactive desktop and select a project registered by it. See [Computer use](docs/COMPUTER_USE.md), [semantic desktop control](docs/SEMANTIC_DESKTOP.md) and [Windows setup](docs/WINDOWS_RUNNER.md).

Windows can run Server and Runner independently. If you use the Windows keepalive task, [`Start-WebCodex.ps1`](examples/windows/Start-WebCodex.ps1) starts services with the saved configuration and key.

Browser automation uses a separately installed Node.js/Chrome provider, with isolated sessions per task and optional login persistence. See [Browser use](docs/BROWSER_USE.md).

## Task center

Open `http://127.0.0.1:8765/manage` and sign in with your configured username and password. Installations without a management login use the token in `client.json`.

![WebCodex task center with example tasks, source filters and task details](docs/images/management.png)

The task center brings Windows, WSL and Ubuntu tasks into one page. Search by task name, project or ID; filter by source, project and status. Select a task to read its summary, start and last-operation times, work steps and command output, or copy instructions for continuing in another conversation.

The default view shows unfinished tasks. Status filters also cover running or queued work, tasks needing attention, unconfirmed execution status, completed tasks and archived tasks. With write permission, you can mark a task complete or reopen it. Archiving hides a task from your default view and can be undone; it keeps the task's records and running processes.

Multiple Servers can appear in one task center through configured sources. See [Task center setup](docs/INSTALLATION.md#統一任務中心). The task center and task card interfaces use Traditional Chinese.

## Task card in ChatGPT

Call `task_present(task_id)` in an MCP Apps-compatible client to open the interactive task card.

![WebCodex task card showing an example task, work steps and file changes](docs/images/chatgpt-task-card.png)

- Follow task progress, work steps and connection status.
- Inspect the project's current uncommitted changes and individual file diffs.
- Read this task's recent command results, exit codes and output.
- Continue work manually, or enable automatic completion messages with a per-task limit and interval. Automatic messages start off and reset on reconnect or takeover.

For long commands, keep using `job_get` and `read_process` in the same turn until the original command finishes. See [Task cards and continuation](docs/CHATGPT.md#任務卡片與跨回合續接) for client requirements and [image imports](docs/CHATGPT.md#從-chatgpt-匯入圖片與-gif) for transferring files from ChatGPT.

## Resume tasks across conversations

A `task_id` identifies the goal, progress, jobs, processes and results saved by the Server. It persists independently of a conversation. Start new work with a new task ID; use the original ID to continue existing work.

1. Find the task with `task_list` and read its current progress with `task_get(task_id)`.
2. Call `task_takeover(task_id)` or use the task card's takeover button.
3. Continue with `work_on_project(task_id=...)`, keeping the original job and process IDs. Use `job_get` and `read_process` to read commands already running.

Resuming requires the same WebCodex client identity (owner) and authorization for the task's project. Each task has one active card controller; takeover transfers card messaging authority to the new conversation. See [the handoff workflow](docs/CHATGPT.md#跨對話接棒).

Connection names identify the MCP connection; the selected project and Runner determine where tools run. Another ChatGPT account needs a connection authorized for the Server. Another independent Server needs its own connection. See [Account and computer setup](docs/CHATGPT.md#換帳號與換電腦).

## Maintenance

```sh
uv run webcodex doctor --share --output diagnostics.json
uv run webcodex backup
uv run webcodex update-check
uv run webcodex retention --days 30 --max-mib 1024 --save-policy
```

`doctor --share` redacts private paths and excludes credentials. `backup` saves configuration and state. `update-check` lists running process and delivery IDs to check before restarting a Runner. Retention previews cleanup of finished output and attachments; add `--apply` to perform it. See [Installation and maintenance](docs/INSTALLATION.md).

## Permissions and compatibility

- Authorized project creation, commands, Git, plugins and desktop operations run directly. Client and Runner settings control the allowed projects and tools. See [Authorization and execution](docs/EXECUTION.md).
- Shells, plugins and desktop input use the Runner account's permissions, with **no OS sandbox**. Desktop access covers the shared screen, including content outside the project. Keep configuration and state private.
- Desktop control supports Windows interactive desktops and Ubuntu X11. Windows supports multiple monitors and mixed display scaling. macOS desktop control is experimental and uses the primary display; structured Git and patch helpers are unavailable on macOS. Wayland/WSLg and locked desktops are unsupported.
- Subagents are coordinated by the MCP client. Starting Codex or Work workers through WebCodex is prohibited. See [Subagent use](docs/SUBAGENTS.md).
- If ChatGPT asks for tool authorization again, confirm it in the original conversation. Jobs already sent to the Runner continue independently. See [Client authorization prompts](docs/CHATGPT.md#chatgpt-平台已知限制再次詢問工具授權).

## Guides

Detailed guides are in Traditional Chinese:

- Usage: [Installation and management](docs/INSTALLATION.md) · [Walkthrough](docs/DEMO.md) · [Operations](docs/OPERATIONS.md) · [Computer use](docs/COMPUTER_USE.md) · [Browser use](docs/BROWSER_USE.md) · [Connections](docs/CHATGPT.md) · [Extensions](docs/EXTENSIONS.md)
- Project: [Architecture](docs/ARCHITECTURE.md) · [Platform support](docs/PLATFORMS.md) · [Roadmap](docs/ROADMAP.md)
- Development: [Contributing](CONTRIBUTING.md) · [Security reporting](.github/SECURITY.md) · [Releases](docs/RELEASING.md)

An independent Python implementation inspired by [yyjeqhc/webcodex](https://github.com/yyjeqhc/webcodex). Your MCP client provides the reasoning and conversation.

## Copyright

Third-party dependencies retain their respective licenses.

```less
Copyright © 2026 Hao-Ping Lin (@susuky). All rights reserved.
```
