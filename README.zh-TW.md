# WebCodex Harness

[English](README.md) | 繁體中文

讓 MCP 用戶端操作你電腦上的 repo 與桌面應用程式。自行架設的 Server 管理驗證與任務紀錄；Runner 主動連線，在你登記的電腦上執行工具。

## 功能

- 讀取、搜尋與修改專案檔案；使用 Git、命令、測試與互動終端。
- 接收 ChatGPT 傳入的圖片與 GIF 原檔，保存至專案。
- 以 `task_id` 跨對話接續任務，包含執行中的命令與輸出。
- 在明確允許的父目錄建立或登記專案。
- 載入專案 skills 與 plugins，保留 MCP 連線狀態。
- 安裝選用依賴後，使用桌面截圖與控制、語意控制項選取或瀏覽器自動化。

## 快速啟動

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Git 與 ripgrep（`rg`）。在本專案原始碼目錄執行：

```sh
uv sync --locked
uv run webcodex init --project /absolute/path/to/your/repo
```

Windows 請使用 `'C:\src\my-repo'` 等專案路徑。設定放在專案外的 `~/.config/webcodex-harness/` 或 `%LOCALAPPDATA%\webcodex-harness\`；`init` 不會覆寫既有設定。

還沒有專案時，改用 `init --project-root /absolute/path/to/projects` 指定既有父目錄。既有安裝可用 `uv run webcodex allow-root /absolute/path/to/projects` 加入父目錄，再重啟 Server 與 Runner。詳見[專案設定](docs/PROJECTS.md)。

在不同終端機分別執行：

```sh
uv run webcodex serve
uv run webcodex runner
```

使用產生的 `client.json` 內 Bearer 憑證，將 MCP 用戶端連線至 `http://127.0.0.1:8765/mcp`，或以 `uv run webcodex bridge` 使用 stdio。ChatGPT 與其他用戶端設定見[連線指南](docs/CHATGPT.md)，建立第一個任務見[操作教學](docs/OPERATIONS.md)。

要從同一安裝環境試用命令流程，可執行：

```sh
uv run webcodex call projects_list
uv run python examples/same_turn_demo.py --project local/project
```

範例會建立任務、執行命令、讀取輸出並保存結果。完成後可在 `/manage` 開啟該任務，詳見[操作展示](docs/DEMO.md)。

### 桌面與瀏覽器工具

桌面控制需安裝選用的 `computer` 依賴並明確授予權限，操作對象是任務所屬 Runner 的桌面。要控制 Windows，就在該 Windows 的互動桌面啟動原生 Runner，並選用它登記的專案。設定見[桌面操作](docs/COMPUTER_USE.md)、[語意桌面控制](docs/SEMANTIC_DESKTOP.md)與 [Windows 安裝指南](docs/WINDOWS_RUNNER.md)。

Windows 可獨立執行 Server 與 Runner。已使用 Windows 常駐排程時，[`Start-WebCodex.ps1`](examples/windows/Start-WebCodex.ps1) 可沿用現有設定與金鑰啟動服務。

瀏覽器自動化使用另外安裝的 Node.js／Chrome provider；各任務使用獨立連線與瀏覽器狀態，也可選擇保存登入。詳見[瀏覽器操作](docs/BROWSER_USE.md)。

## 任務中心

開啟 `http://127.0.0.1:8765/manage`，使用設定的帳號與密碼登入；未設定管理頁帳密時，沿用 `client.json` 的 Token。

![WebCodex 任務中心，以範例任務展示來源篩選與任務詳情](docs/images/management.png)

同一頁可查看 Windows、WSL 與 Ubuntu 任務，以名稱、專案或任務 ID 搜尋，依來源、專案與狀態篩選。選取任務後可讀取摘要、開始與最近操作時間、工作步驟及命令輸出，也能複製在另一個對話接續工作的指令。

預設顯示「未完成」任務，也可篩選執行或排隊中、需要處理、執行狀態待確認、已完成及已封存的任務。具寫入權限時可標記完成或重新開啟。封存會將任務移出你的預設檢視，之後可還原；任務紀錄與執行中的程序都會保留。

設定來源後，可在同一個任務中心查看多個 Server。詳見[任務中心設定](docs/INSTALLATION.md#統一任務中心)。任務中心與任務卡片介面使用繁體中文。

## ChatGPT 任務卡片

在支援 MCP Apps 的用戶端呼叫 `task_present(task_id)`，即可開啟互動式任務卡片。

![WebCodex 任務卡片，以範例任務展示工作步驟與檔案修改](docs/images/chatgpt-task-card.png)

- 查看任務進度、工作步驟與連線狀態。
- 檢視專案目前尚未提交的變更與個別檔案差異。
- 讀取此任務最近命令的執行結果、結束碼與輸出。
- 手動接續工作，或開啟完成後自動傳訊，設定本任務的次數與間隔；自動傳訊預設關閉，重連或接手後也會回到關閉。

執行長命令時，在原回合持續使用 `job_get` 與 `read_process` 讀取原命令，直到結束。用戶端需求見[任務卡片與跨回合續接](docs/CHATGPT.md#任務卡片與跨回合續接)，從 ChatGPT 傳入檔案的方法見[圖片匯入](docs/CHATGPT.md#從-chatgpt-匯入圖片與-gif)。

## 跨對話接續任務

`task_id` 對應 Server 保存的工作目標、進度、job、process 與結果，換對話後仍會保留。新工作建立新的任務 ID；接續既有工作時沿用原 ID。

1. 用 `task_list` 找到任務，再以 `task_get(task_id)` 讀取目前進度。
2. 呼叫 `task_takeover(task_id)`，或在任務卡片按「在此接手」。
3. 用 `work_on_project(task_id=...)` 接續，保留原 job 與 process ID。以 `job_get` 和 `read_process` 讀取已在執行的命令。

續接需使用相同的 WebCodex 用戶端身分（owner），並保有原專案授權。每個任務只有一個有效的卡片控制者；接手會將卡片發訊權移交新對話。詳見[跨對話接棒](docs/CHATGPT.md#跨對話接棒)。

連線名稱用來辨識 MCP 連線；所選專案與 Runner 決定工具在哪台電腦執行。另一個 ChatGPT 帳號需建立獲該 Server 授權的連線；另一個獨立 Server 則需自己的連線。詳見[換帳號與換電腦](docs/CHATGPT.md#換帳號與換電腦)。

## 維護

```sh
uv run webcodex doctor --share --output diagnostics.json
uv run webcodex backup
uv run webcodex update-check
uv run webcodex retention --days 30 --max-mib 1024 --save-policy
```

`doctor --share` 會遮蔽私人路徑並排除憑證；`backup` 備份設定與狀態；`update-check` 列出仍執行的 process 與 delivery ID，供重啟 Runner 前確認。保留期限預設只預覽已結束輸出與附件的清理範圍，加入 `--apply` 才執行。詳見[安裝與維護](docs/INSTALLATION.md)。

## 權限與相容性

- 已授權的專案建立、命令、Git、plugins 與桌面操作會直接執行。可操作的專案與工具由用戶端和 Runner 設定控制，詳見[授權與執行](docs/EXECUTION.md)。
- Shell、plugins 與桌面輸入使用 Runner 帳號權限，**沒有 OS sandbox**。桌面存取涵蓋共用畫面，包含專案外的內容；設定與狀態應保持私有。
- 桌面控制支援 Windows 互動桌面與 Ubuntu X11。Windows 支援多螢幕與混合縮放；macOS 桌面控制屬實驗功能，使用主螢幕，結構化 Git 與 patch helper 尚不可用。Wayland／WSLg 與鎖定桌面不支援。
- 子代理由 MCP 用戶端協調；禁止透過 WebCodex 啟動 Codex 或 Work worker。詳見[子代理使用方式](docs/SUBAGENTS.md)。
- ChatGPT 若再次詢問工具授權，請在原對話確認。已送到 Runner 的工作會獨立執行，詳見[用戶端授權提示](docs/CHATGPT.md#chatgpt-平台已知限制再次詢問工具授權)。

## 文件

詳細指南使用繁體中文：

- 使用：[安裝與管理](docs/INSTALLATION.md) · [操作展示](docs/DEMO.md) · [操作教學](docs/OPERATIONS.md) · [桌面操作](docs/COMPUTER_USE.md) · [瀏覽器操作](docs/BROWSER_USE.md) · [連線設定](docs/CHATGPT.md) · [擴充套件](docs/EXTENSIONS.md)
- 專案：[架構](docs/ARCHITECTURE.md) · [平台支援](docs/PLATFORMS.md) · [功能規劃](docs/ROADMAP.md)
- 開發：[參與開發](CONTRIBUTING.md) · [安全回報](.github/SECURITY.md) · [發布](docs/RELEASING.md)

這是參考 [yyjeqhc/webcodex](https://github.com/yyjeqhc/webcodex) 的獨立 Python 實作。推理與對話由 MCP 用戶端負責。

## 版權

第三方依賴保留各自的授權條款。

```less
Copyright © 2026 Hao-Ping Lin (@susuky). All rights reserved.
```
