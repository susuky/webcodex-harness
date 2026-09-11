# WebCodex Harness

[English](README.md) | 繁體中文

讓 MCP 用戶端操作你 Ubuntu 或 Windows 電腦上的 repo 與桌面應用程式。Server 管理驗證與任務；Runner 主動連線，在本機執行工具。

這是參考 [yyjeqhc/webcodex](https://github.com/yyjeqhc/webcodex) 的獨立 Python 實作。推理與對話由 MCP 用戶端負責。

## 功能

- 讀取、搜尋及以雜湊檢查修改檔案；使用 Git、命令、測試與互動終端。
- 延續已儲存的任務、監看程序，並核准敏感操作。
- 載入專案 skills 與 plugins，保留 MCP 連線狀態。
- 選擇啟用桌面截圖與控制，包含 Unicode 文字輸入。

## 快速啟動

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Git 與 ripgrep（`rg`）。在本專案原始碼目錄執行：

```sh
uv sync --locked
uv run webcodex init --project /absolute/path/to/your/repo
```

Windows 請改用 `'C:\src\my-repo'` 等專案路徑。設定放在專案外的 `~/.config/webcodex-harness/` 或 `%LOCALAPPDATA%\webcodex-harness\`，不會覆寫既有設定。

在不同終端機分別執行：

```sh
uv run webcodex serve
uv run webcodex runner
```

使用產生的 `client.json` 內 Bearer 憑證連線至 `http://127.0.0.1:8765/mcp`，或以 `uv run webcodex bridge` 使用 stdio。用戶端與 ChatGPT 設定見 [連線指南](docs/CHATGPT.md)，建立第一個任務見 [操作教學](docs/OPERATIONS.md)。

桌面工具需安裝選用的 `computer` 依賴並明確授予權限。[桌面操作指南](docs/COMPUTER_USE.md) 涵蓋安裝、既有設定啟用，以及「截圖 → 輸入 → 確認結果」流程。

## 權限與限制

- 預設命令、Git 寫入、擴充安裝與桌面輸入需操作員核准。使用 `uv run webcodex approvals` 檢視請求。
- Shell、plugin 與桌面輸入使用 Runner 帳號權限，**沒有 OS sandbox**。桌面存取涵蓋共用畫面，可能包含專案外內容。設定與狀態應保持私有；截圖與輸入文字目前不會自動到期清除。
- Ubuntu X11 控制已在虛擬桌面測試；Windows 互動桌面仍待驗收。Wayland／WSLg、鎖定桌面與 macOS 尚不支援。

## 文件

詳細指南使用繁體中文：

- 使用：[操作教學](docs/OPERATIONS.md) · [桌面操作](docs/COMPUTER_USE.md) · [連線設定](docs/CHATGPT.md) · [擴充套件](docs/EXTENSIONS.md)
- 專案：[架構](docs/ARCHITECTURE.md) · [平台驗證](docs/PLATFORMS.md) · [功能規劃](docs/ROADMAP.md)
- 開發：[參與開發與測試](CONTRIBUTING.md) · [安全回報](.github/SECURITY.md) · [發布準備](docs/RELEASING.md)

```less
Copyright © 2026 Hao-Ping Lin (@susuky). All rights reserved.

This repository is publicly accessible for viewing and evaluation only.
No license is granted to use, copy, modify, redistribute, sublicense, or use this software commercially without prior written permission.
```
