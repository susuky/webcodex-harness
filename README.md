# WebCodex Harness

English | [繁體中文](README.zh-TW.md)

Let an MCP client work with repositories and desktop applications on your own Ubuntu or Windows machine. A Server handles authentication and durable tasks; outbound Runners execute tools where your projects live.

This is an independent Python implementation inspired by [yyjeqhc/webcodex](https://github.com/yyjeqhc/webcodex), not a distribution of that project. Your MCP client provides the reasoning and conversation.

## What it does

- Read, search and edit files with hash checks; inspect Git and run real commands, tests and interactive terminals.
- Resume tasks and observe long-running processes across client connections.
- Install project skills and plugins, including persistent MCP providers.
- Optionally capture the desktop, click, move, scroll, drag, press keys and type Unicode text on Ubuntu X11 or native Windows.

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git and ripgrep (`rg`). Run from this source checkout:

```sh
uv sync --locked
uv run webcodex init --project /absolute/path/to/your/repo
```

On Windows PowerShell, use `uv run webcodex init --project 'C:\src\my-repo'`. Configuration is stored outside your project in `~/.config/webcodex-harness/` or `%LOCALAPPDATA%\webcodex-harness\`. Initialization never replaces existing configuration.

Start each command in its own terminal:

```sh
uv run webcodex serve
uv run webcodex runner
```

The MCP endpoint is `http://127.0.0.1:8765/mcp`, authenticated with the Bearer token in `client.json`. For a local stdio MCP client, use `uv run webcodex bridge`. For ChatGPT, follow the [connection guide](docs/CHATGPT.md); starting a local server does not create an account connection.

Create a task using a UTF-8 `args.json` containing `{"project":"local/project","title":"Check the project"}`:

```sh
uv run webcodex call projects_list
uv run webcodex call task_create --arguments '@args.json'
```

Use the returned `id` as `task_id` for project tools. Run `uv run webcodex tools` for their schemas, or use `tool_search` from your MCP client.

## Computer use

Install the optional desktop dependencies. On Ubuntu, also install `python3-tk` and `xdotool`, then use an **Ubuntu on Xorg** session.

```sh
uv sync --locked --extra computer
uv run --extra computer webcodex init --project /absolute/path/to/repo --computer-use
uv run --extra computer webcodex doctor
uv run --extra computer webcodex runner
```

Use the Windows project path in PowerShell. Keep the Server running separately. Existing installations should add the permissions described in the [computer use guide](docs/COMPUTER_USE.md), rather than reinitializing.

Call `computer_screenshot`, then use its `snapshot_id` for one input operation. Take a fresh screenshot to verify the result. Coordinates use the original `source_width` / `source_height`, even when the returned image is resized.

Ubuntu X11 has a live virtual-desktop test; the Windows backend still needs validation on an interactive Windows desktop. Wayland/WSLg, locked desktops and macOS are unsupported. Desktop access covers the shared screen and focused application, including content outside the project.

## Permissions and recovery

Commands, Git writes, extension installation and desktop input require operator approval by default: inspect `webcodex approvals`, then use `webcodex approve JOB_ID` or `webcodex deny JOB_ID`. `init --trusted` skips approvals; desktop tools still require the separate `--computer-use` opt-in.

Keep configuration and state private. Shells, plugins and desktop input act with the Runner account's permissions; there is **no OS sandbox**. Screenshots and typed text are retained in private job journals, which currently have no automatic expiry.

- `task_id` identifies saved work. Resume with `task_list` / `task_get`.
- `job_id` identifies one call. Poll `job_get` while queued or running; reuse the same `request_id` and arguments after a disconnect.
- `process_id` identifies a command or terminal. Read it with `read_process`; only its final `exit_code` determines command success.

An `unknown` result may already have changed state: inspect before retrying. After a Runner restart, old processes are marked `interrupted`; saved output remains readable.

## Documentation and development

Detailed guides are currently in Traditional Chinese:

| Guide | Contents |
| --- | --- |
| [Operations](docs/OPERATIONS.md) | Setup, task workflow, approvals and troubleshooting |
| [Computer use](docs/COMPUTER_USE.md) | Ubuntu/Windows setup, tools and desktop limits |
| [Connections](docs/CHATGPT.md) | stdio, ChatGPT and HTTPS |
| [Extensions](docs/EXTENSIONS.md) | Skills, plugins and MCP providers |
| [Architecture](docs/ARCHITECTURE.md) | Execution and authorization contracts |
| [Platform validation](docs/PLATFORMS.md) | Evidence and reproducible tests |
| [Upstream comparison](docs/ROADMAP.md) | Adopted ideas and proposed additions |

```sh
uv run --extra computer pytest -q
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv build
```

Desktop interaction tests are opt-in; see the computer use guide. Keep this README and [README.zh-TW.md](README.zh-TW.md) synchronized when changing setup, features or limitations.
