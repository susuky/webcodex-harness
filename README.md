# WebCodex Harness

English | [繁體中文](README.zh-TW.md)

Let an MCP client work with repositories and desktop applications on your Ubuntu or Windows machine. A Server manages authentication and tasks; outbound Runners execute tools locally.

## What it does

- Read, search and edit files with hash checks; use Git, commands, tests and interactive terminals.
- Resume saved tasks, monitor processes and approve sensitive operations.
- Create or register projects under explicitly allowed parent directories, even with no existing task.
- Load project skills and plugins with persistent MCP connections.
- Optionally capture and control the desktop, including Unicode text input.
- Add browser navigation, forms, tabs and page data extraction with the optional Playwright MCP plugin.

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git and ripgrep (`rg`). Run from this source checkout:

```sh
uv sync --locked
uv run webcodex init --project /absolute/path/to/your/repo
```

On Windows, replace the project path with a path such as `'C:\src\my-repo'`. Configuration stays outside the project in `~/.config/webcodex-harness/` or `%LOCALAPPDATA%\webcodex-harness\`; existing configuration is never overwritten.

To start with no existing project, use `init --project-root /absolute/path/to/projects` with an existing parent directory. See [Project creation](docs/PROJECTS.md) for setup and enabling an existing installation.

Start each command in its own terminal:

```sh
uv run webcodex serve
uv run webcodex runner
```

Connect to `http://127.0.0.1:8765/mcp` using the Bearer token in the generated `client.json`, or use `uv run webcodex bridge` for stdio. See [Connections](docs/CHATGPT.md) for client and ChatGPT setup, and [Operations](docs/OPERATIONS.md) to create your first task.

Desktop tools require the optional `computer` dependencies and explicit permissions. Follow [Computer use](docs/COMPUTER_USE.md) for installation, enabling an existing setup and the screenshot → input → verification workflow.

Browser tools use a separately installed Node.js/Chrome provider, with isolated sessions per task. Follow [Browser use](docs/BROWSER_USE.md) for setup and optional login persistence.

## Permissions and limits

- Project creation, commands, Git writes, extension installation and desktop input require operator approval by default. Personal projects can opt into [automatic development operations](docs/APPROVALS.md); pending jobs include the correct approval command and service identity.
- Shells, plugins and desktop input use the Runner account's permissions, with **no OS sandbox**. Desktop access covers the shared screen, including content outside the project. Keep configuration and state private; screenshots and typed text have no automatic expiry.
- Ubuntu X11 control has been tested on a virtual desktop. Interactive Windows desktop validation is pending; Wayland/WSLg, locked desktops and macOS are unsupported.

## Guides

Detailed guides are in Traditional Chinese:

- Usage: [Operations](docs/OPERATIONS.md) · [Computer use](docs/COMPUTER_USE.md) · [Browser use](docs/BROWSER_USE.md) · [Connections](docs/CHATGPT.md) · [Extensions](docs/EXTENSIONS.md)
- Project: [Architecture](docs/ARCHITECTURE.md) · [Platform validation](docs/PLATFORMS.md) · [Feature roadmap](docs/ROADMAP.md)
- Development: [Contributing and tests](CONTRIBUTING.md) · [Security reporting](.github/SECURITY.md) · [Release readiness](docs/RELEASING.md)

An independent Python implementation inspired by [yyjeqhc/webcodex](https://github.com/yyjeqhc/webcodex). Your MCP client provides the reasoning and conversation.

## Copyright

Third-party dependencies retain their respective licenses.

```less
Copyright © 2026 Hao-Ping Lin (@susuky). All rights reserved.
```
