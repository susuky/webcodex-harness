# WebCodex Harness

English | [繁體中文](README.zh-TW.md)

Let an MCP client work with repositories and desktop applications on your Ubuntu or Windows machine. A Server manages authentication and tasks; outbound Runners execute tools locally.

An independent Python implementation inspired by [yyjeqhc/webcodex](https://github.com/yyjeqhc/webcodex). Your MCP client provides the reasoning and conversation.

## What it does

- Read, search and edit files with hash checks; use Git, commands, tests and interactive terminals.
- Resume saved tasks, monitor processes and approve sensitive operations.
- Load project skills and plugins with persistent MCP connections.
- Optionally capture and control the desktop, including Unicode text input.

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git and ripgrep (`rg`). Run from this source checkout:

```sh
uv sync --locked
uv run webcodex init --project /absolute/path/to/your/repo
```

On Windows, replace the project path with a path such as `'C:\src\my-repo'`. Configuration stays outside the project in `~/.config/webcodex-harness/` or `%LOCALAPPDATA%\webcodex-harness\`; existing configuration is never overwritten.

Start each command in its own terminal:

```sh
uv run webcodex serve
uv run webcodex runner
```

Connect to `http://127.0.0.1:8765/mcp` using the Bearer token in the generated `client.json`, or use `uv run webcodex bridge` for stdio. See [Connections](docs/CHATGPT.md) for client and ChatGPT setup, and [Operations](docs/OPERATIONS.md) to create your first task.

Desktop tools require the optional `computer` dependencies and explicit permissions. Follow [Computer use](docs/COMPUTER_USE.md) for installation, enabling an existing setup and the screenshot → input → verification workflow.

## Permissions and limits

- Commands, Git writes, extension installation and desktop input require operator approval by default. Inspect requests with `uv run webcodex approvals`.
- Shells, plugins and desktop input use the Runner account's permissions, with **no OS sandbox**. Desktop access covers the shared screen, including content outside the project. Keep configuration and state private; screenshots and typed text have no automatic expiry.
- Ubuntu X11 control has been tested on a virtual desktop. Interactive Windows desktop validation is pending; Wayland/WSLg, locked desktops and macOS are unsupported.

## Guides

Detailed guides are in Traditional Chinese:

- Usage: [Operations](docs/OPERATIONS.md) · [Computer use](docs/COMPUTER_USE.md) · [Connections](docs/CHATGPT.md) · [Extensions](docs/EXTENSIONS.md)
- Project: [Architecture](docs/ARCHITECTURE.md) · [Platform validation](docs/PLATFORMS.md) · [Feature roadmap](docs/ROADMAP.md)
- Development: [Contributing and tests](CONTRIBUTING.md) · [Security reporting](.github/SECURITY.md) · [Release readiness](docs/RELEASING.md)

```less
Copyright © 2026 Hao-Ping Lin (@susuky). All rights reserved.

This repository is publicly accessible for viewing and evaluation only.
No license is granted to use, copy, modify, redistribute, sublicense, or use this software commercially without prior written permission.
```
