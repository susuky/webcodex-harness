# WebCodex Harness

[English](README.md) | 繁體中文

讓 MCP 用戶端操作你自己 Ubuntu 或 Windows 電腦上的 repo 與桌面應用程式。Server 負責驗證與持久任務；Runner 主動連線，在專案所在機器執行工具。

這是參考 [yyjeqhc/webcodex](https://github.com/yyjeqhc/webcodex) 的獨立 Python 實作，並非該專案的發行版本。推理與對話由 MCP 用戶端負責。

## 功能

- 讀取、搜尋及以雜湊檢查修改檔案；操作 Git，執行真實命令、測試與互動終端。
- 跨用戶端連線延續任務，觀察長時間執行的程序。
- 安裝專案 skills 與 plugins，包含保留連線狀態的 MCP providers。
- 選擇啟用 Ubuntu X11 或 Windows 原生桌面的截圖、點擊、移動、捲動、拖曳、按鍵與 Unicode 文字輸入。

## 快速啟動

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Git 與 ripgrep（`rg`）。在本專案原始碼目錄執行：

```sh
uv sync --locked
uv run webcodex init --project /absolute/path/to/your/repo
```

Windows PowerShell 改用 `uv run webcodex init --project 'C:\src\my-repo'`。設定放在專案外的 `~/.config/webcodex-harness/` 或 `%LOCALAPPDATA%\webcodex-harness\`；初始化不會覆寫既有設定。

在不同終端機分別執行：

```sh
uv run webcodex serve
uv run webcodex runner
```

MCP 入口為 `http://127.0.0.1:8765/mcp`，使用 `client.json` 的 Bearer 憑證。本機 stdio MCP 用戶端可使用 `uv run webcodex bridge`。ChatGPT 請依 [連線指南](docs/CHATGPT.md) 設定；本機啟動不會自動建立帳號連線。

建立 UTF-8 `args.json`，內容為 `{"project":"local/project","title":"Check the project"}`，再建立任務：

```sh
uv run webcodex call projects_list
uv run webcodex call task_create --arguments '@args.json'
```

將回傳的 `id` 作為專案工具的 `task_id`。`uv run webcodex tools` 可查看完整參數，MCP 用戶端也能使用 `tool_search` 探索。

## Computer use

安裝選用的桌面依賴。Ubuntu 另需安裝 `python3-tk`、`xdotool`，並登入 **Ubuntu on Xorg** 工作階段。

```sh
uv sync --locked --extra computer
uv run --extra computer webcodex init --project /absolute/path/to/repo --computer-use
uv run --extra computer webcodex doctor
uv run --extra computer webcodex runner
```

PowerShell 改用 Windows 專案路徑，Server 另開終端機保持執行。既有安裝請依 [桌面操作指南](docs/COMPUTER_USE.md) 加入權限，不必重新初始化。

先呼叫 `computer_screenshot`，再以回傳的 `snapshot_id` 執行一次輸入操作，最後重新截圖確認。座標採原始 `source_width`／`source_height`，即使回傳圖片已縮小也相同。

Ubuntu X11 已有真實虛擬桌面測試；Windows 後端仍需在互動式 Windows 桌面驗收。Wayland／WSLg、鎖定桌面及 macOS 尚不支援。桌面權限涵蓋共用畫面與目前聚焦的程式，可能包含專案外內容。

## 權限與恢復

預設命令、Git 寫入、擴充安裝與桌面輸入需要操作員核准：先用 `webcodex approvals` 檢視，再執行 `webcodex approve JOB_ID` 或 `webcodex deny JOB_ID`。`init --trusted` 省略核准；桌面工具仍需另行指定 `--computer-use`。

設定與狀態應保持私有。Shell、plugin 與桌面輸入以 Runner 帳號權限操作，**沒有 OS sandbox**。截圖與輸入文字會保存在私有工作紀錄，目前沒有自動到期清除。

- `task_id`：保存的工作，使用 `task_list`／`task_get` 延續。
- `job_id`：一次呼叫，排隊或執行中使用 `job_get`；斷線重試沿用同一 `request_id` 與參數。
- `process_id`：命令或終端機，以 `read_process` 讀取；只有最後的 `exit_code` 能判斷命令是否成功。

`unknown` 可能已改變狀態，先查證再決定是否重做。Runner 重啟後，舊程序標為 `interrupted`，保存的輸出仍可讀取。

## 文件與開發

詳細指南目前使用繁體中文：

| 指南 | 內容 |
| --- | --- |
| [操作教學](docs/OPERATIONS.md) | 安裝、任務、核准與故障排除 |
| [桌面操作](docs/COMPUTER_USE.md) | Ubuntu／Windows 設定、工具與限制 |
| [連線設定](docs/CHATGPT.md) | stdio、ChatGPT 與 HTTPS |
| [擴充套件](docs/EXTENSIONS.md) | Skills、plugins 與 MCP providers |
| [架構](docs/ARCHITECTURE.md) | 執行與權限契約 |
| [平台驗證](docs/PLATFORMS.md) | 證據與重現方式 |
| [上游比對](docs/ROADMAP.md) | 已採用概念與後續建議 |

```sh
uv run --extra computer pytest -q
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv build
```

桌面互動測試需明確啟用，見桌面操作指南。安裝方式、功能或限制變動時，請同步更新本文件與 [README.md](README.md)。
