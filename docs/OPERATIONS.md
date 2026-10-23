# 操作教學

本指南涵蓋 Ubuntu、Windows 原生與 WSL2 的 coding 工作流程。桌面控制另見 [Computer use](COMPUTER_USE.md)，實測範圍見 [平台驗證](PLATFORMS.md)。

## 安裝

需要 Python 3.11+、Git、ripgrep（`rg`）與 [uv](https://docs.astral.sh/uv/getting-started/installation/)。以下路徑均為範例，請換成本專案原始碼的實際位置。

Windows PowerShell：

```powershell
winget install --id Git.Git -e
winget install --id BurntSushi.ripgrep.MSVC -e
winget install --id astral-sh.uv -e
# 重開終端機後，切換到本專案
Set-Location 'C:\src\webcodex'
uv sync --locked --python 3.12
uv run webcodex doctor
```

Ubuntu Bash：

```bash
sudo apt-get update
sudo apt-get install -y git ripgrep curl build-essential
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
cd ~/src/webcodex
uv sync --locked --python 3.12
uv run webcodex doctor
```

Windows WSL2 先依 [Microsoft 指南](https://learn.microsoft.com/en-us/windows/wsl/install) 安裝 Ubuntu，再在 Ubuntu 內執行上述 Bash 命令。將程式、repo 與設定放在 Linux home；Windows 與 WSL2 使用各自的 `.venv`、工具鏈及 state，不共用執行中的資料庫。

## 初始化與啟動

首次初始化可指定已存在的工作 repo；若要從零建立新專案，使用 `--project-root` 指定既有父目錄，依 [建立與登記專案](PROJECTS.md) 完成設定。

```powershell
uv run webcodex init --project 'C:\src\my-repo'
```

```bash
uv run webcodex init --project "$HOME/src/my-repo"
```

| 設定 | Windows | Ubuntu／WSL2 |
| --- | --- | --- |
| 預設目錄 | `%LOCALAPPDATA%\webcodex-harness\` | `~/.config/webcodex-harness/` |
| 服務設定 | `server.json`、`runner.json`、`client.json` | 同左 |
| MCP 入口 | `http://127.0.0.1:8765/mcp` | 同左 |

設定必須位於 repo 外，`init` 不會覆寫既有設定。首次可加 `--port 8787` 或 `--config-dir`；自訂目錄後，各命令需用 `--config` 指向對應 JSON。`serve`／操作員命令讀 `server.json`，`runner` 讀 `runner.json`，`call`／`tools`／`bridge` 讀 `client.json`。

不同終端機分別執行 `uv run webcodex serve` 與 `uv run webcodex runner`，再以 `uv run webcodex call projects_list` 驗證連線。專案 `local/project` 的 `online` 應為 `true`。`doctor` 用於檢查本機環境。

已授權的命令、Git 寫入與擴充安裝直接執行，命令使用 Runner 帳號權限；桌面權限需另外啟用。設定中的三種憑證各有對應用途。

## 建立任務與呼叫工具

新專案先走 `project_roots_list` → `project_create`／`project_register` → `job_get`，登記成功後才用回傳的 project ID 建立以下任務。這些專案操作不需要預先提供 task ID。

以下統一使用 UTF-8 `args.json`，適用 Bash 與 PowerShell，並接受 UTF-8 BOM。也可用 `--arguments -` 從 stdin 讀 JSON；Windows PowerShell 5.1 需先將 `$OutputEncoding` 設為 UTF-8。

```json
{"project":"local/project","title":"檢查專案並執行測試"}
```

```text
uv run webcodex call work_on_project --arguments '@args.json'
```

保存回傳的 `task.id` 作為 `task_id`，並查看同時回傳的 `workspace`，確認規則、Git 狀態及工具鏈。若仍在執行，依 `workspace.next_action` 取得原工作結果。接續已有任務時，將參數改為 `{"task_id":"TASK_ID"}` 呼叫相同工具。所有範例的 `TASK_ID`、`JOB_ID`、`PROCESS_ID`、`SHA256` 都必須換成實際值。

`workspace_info.is_git_repository` 指登記的專案根目錄是否為可用的 Git 工作樹根。新目錄沒有 `.git` 時回傳 `false`，不產生逾時錯誤，也不自行初始化 Git；一般讀寫仍可繼續。有 Git 中繼資料但查詢失敗時回傳 `null` 與 `git_error`，保留實際結束碼；只有成功取得狀態才回傳 `true`。位於另一個 repository 內的子目錄，不會因此取得操作父 repository 的授權。

| 工具 | `args.json` 範例 |
| --- | --- |
| `read_file` | `{"task_id":"TASK_ID","path":"src/main.py"}` |
| `edit_file` | `{"task_id":"TASK_ID","path":"src/main.py","old_text":"return a - b","new_text":"return a + b","expected_sha256":"SHA256","request_id":"fix-1"}` |
| `git_status`／`git_diff` | `{"task_id":"TASK_ID"}` |
| `exec_command` | `{"task_id":"TASK_ID","argv":["uv","run","pytest","-q"],"request_id":"pytest-1"}` |
| `exec_command`（Rust） | `{"task_id":"TASK_ID","argv":["cargo","test"],"request_id":"cargo-1"}` |

對應呼叫為 `uv run webcodex call TOOL_NAME --arguments '@args.json'`。命令使用工作 repo 的環境；也可在 `argv` 指定其 Python 絕對路徑。Rust 需安裝相應工具鏈，Windows 原生另外需要 MSVC C++ 工具與 Windows SDK。

`read_file` 提供整份檔案 SHA-256。覆寫、編輯或刪除必須帶入 hash；過期時重新讀取，不強制覆蓋。`task_review` 提供包含新檔案的變更清單、逐檔差異與實際命令結果；`git_diff` 本身仍不包含 untracked 新檔。`apply_patch` 接受標準 unified Git diff，不接受 `*** Begin Patch` 格式。完整 schema 由 `webcodex tools` 提供。

`search_files` 每頁搜尋最多 500 個檔案、20 MiB 內容；超過時縮小 `path` 或 `glob`。搜尋使用受保護的私有快照，結果仍顯示原始相對檔名。Git 與 ripgrep 必須安裝在專案／授權父目錄以外的絕對 PATH 目錄；相對 PATH 與專案內同名程式不會用於這些結構化工具。Windows 必須提供原生 `.exe`／`.com`，不使用 `.bat`／`.cmd` wrapper。

結構化 Git 操作不會自動補取 partial clone 的缺失物件；遇到此類錯誤時，先由操作員在可信終端機明確下載所需物件，再重新查詢。HTTPS 擴充安裝的明確 clone／fetch 不受此限制。

## 等待與停止程序

已授權的工具直接執行，設定與升級方式見[授權與執行](EXECUTION.md)。狀態為 `queued`／`running` 時，使用 `job_get`，參數為 `{"job_id":"JOB_ID","wait_seconds":10}`，持續查同一工作。

`exec_command` 的 job 成功只代表已取得程序。用 `read_process` 觀察到結束：

```json
{"task_id":"TASK_ID","process_id":"PROCESS_ID","cursor":0,"wait_seconds":10}
```

**程序已結束且 `exit_code` 為 0，才代表命令成功。** 後續用 `next_cursor` 讀新增輸出；`has_more` 表示同份輸出還有內容，`truncated` 表示超出保存上限。

輸出游標以 byte 計算，分頁會保留完整 UTF-8 字元。若 `max_bytes` 小於第一個字元的長度，單頁最多額外讀取 3 bytes；程序仍在輸出半個字元時，游標會保留在該字元開頭，待後續輸出到齊再讀。

斷線重試同一次操作，沿用相同 `request_id` 與參數；改完程式再跑測試則換新 ID。`unknown` 可能已執行，不應自動重送。停止程序用 `cancel_process(task_id, process_id)`；取消尚未開始的工作用 `job_cancel(job_id)`。

需要 shell 時，`exec_command` 的 `command` 在 Windows 使用 PowerShell，Ubuntu／WSL2 使用 Bash；不能與 `argv` 同時提供。加 `tty: true` 可開啟 ConPTY／PTY，再使用 `write_stdin`、`resize_terminal`。`cd` 與環境變數只在同一個 live shell 內延續。

## 擴充與延續

依 [擴充指南](EXTENSIONS.md) 使用 `extension_install` 安裝、`skills_list`／`skill_read` 讀規則，或透過 `plugin_tools`／`plugin_call` 呼叫外部 MCP。Provider 依賴與外部帳號登入需另行配置。

[連線指南](CHATGPT.md) 提供 stdio bridge、ChatGPT 與 HTTPS 設定。

離開前以 `task_update` 保存真實進度：

```json
{"task_id":"TASK_ID","status":"active","summary":"已修正加法並通過相關測試；下一步檢查其他呼叫端。","plan":[{"step":"修正加法","status":"completed"},{"step":"檢查呼叫端","status":"pending"}]}
```

下次透過 `task_list`／`task_get` 讀同一任務，從保存的摘要與紀錄接續；所有工作完成才設為 `completed`。

## 維運與故障排除

停止時先在 Runner 終端機按 Ctrl-C，再停止 Server。Runner 會停止其管理的命令並保存結果；重啟後舊程序標為 `interrupted`，輸出仍可讀。備份設定、state 與 repo 前先停機，並保留存取限制。Systemd 或 Windows 系統服務需由操作員配置。

| 情況 | 處理方式 |
| --- | --- |
| 找不到命令／工具鏈 | 在 Runner 終端機確認 PATH；安裝工具或修改 `environment` 後重啟 Runner |
| `projects_list` 成功回覆專案 offline | 檢查 Runner、URL、配對憑證、專案路徑；不要啟動共用 state 的第二個 Runner |
| 工具看得到，但出現 developer MCP `FORBIDDEN` | 目前對話拒絕執行，Runner 狀態未確認；依 [連線診斷](CHATGPT.md#工具看得到但無法執行) 檢查用戶端與工作區權限 |
| 401 | 確認 client、Runner 與操作員使用各自的 JSON |
| 工作已排入佇列但尚未執行 | 查同一 `job_id` 與 Runner 心跳；桌面截圖若已過期需重新觀察 |
| Windows JSON 引號錯誤 | 使用 UTF-8 `@args.json` |
| Windows 路徑被拒絕 | 使用本機實體 NTFS 資料夾；junction、reparse point、ADS 與裝置名稱不支援 |
| ConPTY／Job 失敗 | 保存錯誤與 doctor 結果；以 `tty: false` 區分終端機問題 |
| 桌面不可用 | 依 [桌面指南](COMPUTER_USE.md) 檢查 X11、登入工作階段與選用依賴 |
| `unknown`／`interrupted` | 查檔案、Git、桌面或程序輸出後，再決定新操作 |
