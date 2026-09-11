# 操作教學：Ubuntu、Windows 原生與 WSL2

本指南從安裝、啟動、建立任務，到讀改檔案、測試、Git、外掛與延續工作。**Ubuntu 24.04、Windows 11 原生及 WSL2 Ubuntu 22.04 均已實機測試通過，包含實際啟動 Server／Runner、命令核准與任務保存。** 詳細環境與重現步驟見 [平台驗證](PLATFORMS.md)。

## 1. 準備程式與工作專案

需要兩個路徑：本 Harness 程式所在目錄，以及要交給 ChatGPT 操作的 repo。兩者可以相同，也可以不同；設定與狀態目錄必須在工作 repo 外。

以下的 `C:\src\webcodex`、`C:\src\my-repo`、`~/src/webcodex`、`~/src/my-repo` 都是範例，請換成自己的已存在目錄。本專案尚未提供公開發布網址，請使用你取得的這份獨立專案原始碼；參考用的 yyjeqhc/webcodex 是另一套程式。跨平台搬移只複製原始碼，不複製 `.venv`、憑證或執行中的 SQLite 檔案。

### Windows 原生：PowerShell

目標為 Windows 11／Windows 10 2004+，本機 NTFS 磁碟。開啟 PowerShell，安裝 Git、ripgrep 與 uv；已有的工具可略過：

```powershell
winget install --id Git.Git -e
winget install --id BurntSushi.ripgrep.MSVC -e
winget install --id astral-sh.uv -e
```

關閉再開啟 PowerShell，讓新的 PATH 生效。uv 的其他安裝方法見 [官方安裝說明](https://docs.astral.sh/uv/getting-started/installation/)。

```powershell
Set-Location 'C:\src\webcodex'
uv sync --locked --python 3.12
uv run webcodex doctor
```

不需要執行 `Activate.ps1`。`uv sync` 會建立 Windows 專用 `.venv`，並安裝原生支援所需的 pywin32 與 pywinpty。

### Ubuntu：Bash

在 Ubuntu 終端機執行；範例使用 Python 3.12，符合程式要求的 Python 3.11+：

```bash
sudo apt-get update
sudo apt-get install -y git ripgrep curl build-essential
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
cd ~/src/webcodex
uv sync --locked --python 3.12
uv run webcodex doctor
```

uv 可以取得缺少的 Python，不必替換 Ubuntu 的系統 Python。[uv Python 說明](https://docs.astral.sh/uv/guides/install-python/)

### Windows WSL2：先安裝 Ubuntu，再使用 Bash

以系統管理員開啟 PowerShell：

```powershell
wsl --install -d Ubuntu
```

依提示重新啟動電腦，首次開啟 Ubuntu 時建立 Linux 帳號。之後在 PowerShell 檢查：

```powershell
wsl --list --verbose
wsl --set-version Ubuntu 2
wsl --distribution Ubuntu
```

`--set-version` 只在清單顯示版本 1 時需要。進入 Ubuntu 後，完整執行上面的 Ubuntu 安裝步驟；Windows 已有的 uv、Python 或 Cargo 不代表 WSL2 內已安裝。[Microsoft WSL 安裝文件](https://learn.microsoft.com/en-us/windows/wsl/install)

將程式、repo 與設定放在 WSL 的 `/home/你的帳號/` 內；本指南驗收範圍不包含 `/mnt/c` 等 Windows 磁碟掛載的特殊權限與檔案語意。Windows 編輯器可以透過 WSL 開啟 repo；在 Bash 執行 `explorer.exe .` 可開啟目前資料夾。[Microsoft 檔案系統說明](https://learn.microsoft.com/en-us/windows/wsl/filesystems)

## 2. 第一次初始化

若已初始化過，直接進入第 3 節；重跑 `init` 會拒絕覆寫原有設定。Windows PowerShell：

```powershell
uv run webcodex init --project 'C:\src\my-repo'
```

Ubuntu／WSL2 Bash：

```bash
uv run webcodex init --project "$HOME/src/my-repo"
```

回傳 `config_dir`、`mcp_url` 與 `project: local/project`。預設位置如下：

| 項目 | Windows 原生 | Ubuntu／WSL2 |
| --- | --- | --- |
| 設定目錄 | `%LOCALAPPDATA%\webcodex-harness\` | `~/.config/webcodex-harness/` |
| Server | `server.json` | `server.json` |
| Runner | `runner.json` | `runner.json` |
| 用戶端 | `client.json` | `client.json` |
| 本機 MCP | `http://127.0.0.1:8765/mcp` | `http://127.0.0.1:8765/mcp` |

如果 8765 已被使用，在**第一次**初始化時加 `--port 8787`。已有設定則停機後同步修改 `server.json` 的 `port`／`public_url`，以及 `runner.json`、`client.json` 的 `server_url`。

可用 `--config-dir` 選擇另一個 repo 外目錄。之後每個命令都要用 `--config` 指向對應 JSON，例如 `webcodex runner --config 'D:\settings\runner.json'`；工具呼叫用 `client.json`，核准操作用 `server.json`。

預設命令、Git 寫入與擴充操作會等待你核准。若已決定授予這個用戶端完整的工作機帳號操作權限，可在第一次初始化加 `--trusted`；它不建立 OS 沙箱。三份 JSON 含不同用途的憑證，不要貼進對話或 repo。

## 3. 啟動與確認連線

開啟兩個終端機，都切換到 Harness 程式目錄；三種環境的命令相同：

終端機 A：

```text
uv run --locked webcodex serve
```

終端機 B：

```text
uv run --locked webcodex runner
```

另開終端機 C 操作工具：

```text
uv run webcodex call projects_list
```

確認 `projects` 中有 `local/project` 且 `online` 為 `true`。`doctor` 只檢查本機環境，不會代替 Server／Runner 連線驗收；`projects_list` 才會取得註冊結果。

安裝新工具、調整 PATH 或修改 `runner.json` 後，需要重啟 Runner 才會載入。不要用不同終端機同時啟動兩個共用 state 的 Runner。

## 4. 建立任務與傳入參數

所有範例中的 `TASK_ID`、`JOB_ID`、`PROCESS_ID`、`SHA256` 都要換成實際回傳值。PowerShell 先將管線設定為 UTF-8，避免 Windows PowerShell 5.1 將中文轉成問號：

```powershell
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
'{"project":"local/project","title":"檢查專案並執行測試"}' |
    uv run webcodex call task_create --arguments -
```

Bash：

```bash
uv run webcodex call task_create --arguments '{"project":"local/project","title":"檢查專案並執行測試"}'
```

記下回傳的 `id`，它就是 `task_id`。

接下來統一使用 JSON 參數檔，方便 Windows 與 Bash 共用：在本機文字編輯器建立 `args.json`，存成 UTF-8，填入各步驟提供的 JSON，再呼叫指定工具。這個檔案不需要放在工作 repo；`--arguments` 後的 `@` 代表讀檔，UTF-8 BOM 也可接受。

先填入：

```json
{"task_id":"TASK_ID"}
```

```text
uv run webcodex call workspace_info --arguments '@args.json'
```

結果包含專案規則、Git 狀態、工具鏈、實際 `python_executable`、預設 shell 與終端機類型。Windows 專案內的工具路徑仍用 `/`，例如 `src/main.py`；只有初始化根目錄與 `argv` 的執行檔使用作業系統絕對路徑。

## 5. 看懂 job、核准與程序結果

遠端工具呼叫可能先回傳 `job_id`，狀態為 `pending_approval`、`queued` 或 `running`。不要重送成新工作。需要核准時，在操作員終端機檢視完整命令與參數：

```text
uv run webcodex approvals
uv run webcodex approve JOB_ID
```

不執行該操作則用 `uv run webcodex deny JOB_ID`。核准後，將 `args.json` 改為：

```json
{"job_id":"JOB_ID","wait_seconds":10}
```

```text
uv run webcodex call job_get --arguments '@args.json'
```

若仍為 `queued`／`running`，繼續查同一個 job。`succeeded` 的工具結果在 `result` 內；`failed` 時先讀錯誤。`unknown` 表示結果不能確認，先查看檔案、Git 或程序紀錄，不要直接重做修改。

對 `exec_command`，job 成功代表取得程序控制代碼；程式可能仍在執行。記下 `result.process_id`、`next_cursor`，下一次使用：

```json
{"task_id":"TASK_ID","process_id":"PROCESS_ID","cursor":0,"wait_seconds":10}
```

```text
uv run webcodex call read_process --arguments '@args.json'
```

第一次想重看全部輸出用 `cursor: 0`；後續只想看新增輸出，改成上次的 `next_cursor`。若 `has_more` 為 `true`，繼續讀取剩餘內容。**只有程序結束且 `exit_code` 為 0，才代表命令成功。** `truncated: true` 表示輸出超過保存上限。

停止特定程序：將 `args.json` 填入 `task_id` 與 `process_id`，呼叫 `cancel_process`。取消尚未開始的工作則用 `job_cancel`，參數只有 `job_id`。

## 6. 讀檔、改檔與看 Git diff

把以下參數交給 `read_file`，將路徑換成專案實際檔案：

```json
{"task_id":"TASK_ID","path":"src/main.py"}
```

讀取結果包含整份檔案的 `sha256`。修改時使用 `edit_file`：

```json
{"task_id":"TASK_ID","path":"src/main.py","old_text":"return a - b","new_text":"return a + b","expected_sha256":"SHA256","request_id":"fix-addition-1"}
```

`old_text` 必須精確匹配，預設只能出現一次。過期 hash 會拒絕修改，需重新讀檔。建立新檔案可用 `write_file` 的 `path`／`content`，不傳 `expected_sha256`；覆寫已有檔案則必須傳入 hash。

改完後把參數改回 `{"task_id":"TASK_ID"}`，依序呼叫 `git_status` 與 `git_diff`。新建的 untracked 檔案會出現在 status；一般 diff 不包含未追蹤檔案，需另用 `read_file` 檢查。`git_stage` 和 `git_commit` 是獨立且需相應權限的操作，不會因為改檔自動提交或推送。

需要一次套用多檔變更時，使用 `apply_patch` 的標準 unified Git diff 與所有既有目標檔案 hash；不接受 `*** Begin Patch` 格式。完整 schema 可用 `uv run webcodex tools` 查看。

## 7. 執行 pytest、cargo test 與互動終端

測試依賴要安裝在工作專案自己的環境。Harness 的 `.venv` 不會自動具備工作 repo 的所有套件；使用該 repo 的 Python 絕對路徑最明確。

Windows 的 `exec_command` 範例：

```json
{"task_id":"TASK_ID","argv":["C:\\src\\my-repo\\.venv\\Scripts\\python.exe","-m","pytest","-q"],"request_id":"pytest-1"}
```

Ubuntu／WSL2：

```json
{"task_id":"TASK_ID","argv":["/home/你的帳號/src/my-repo/.venv/bin/python","-m","pytest","-q"],"request_id":"pytest-1"}
```

若專案使用 uv，也可以傳 `argv: ["uv","run","pytest","-q"]`，前提是 Runner 的 PATH 能找到 uv。

Rust 專案先依 [Rust 官方安裝指南](https://doc.rust-lang.org/book/ch01-01-installation.html) 安裝工具鏈；Windows 原生需對應 MSVC linker，Ubuntu／WSL2 使用 Linux 工具鏈。安裝後重開終端機並重啟 Runner：

```text
cargo --version
rustc --version
```

```json
{"task_id":"TASK_ID","argv":["cargo","test"],"request_id":"cargo-1"}
```

這些命令都要依第 5 節核准並讀到程序結束。相同操作因連線問題重試時沿用相同 `request_id` 與參數；修改程式後要重新跑測試，則換成新的 ID，例如 `pytest-2`。

要使用 shell 管線或重導向，`exec_command` 改傳 `command`：Windows 是 PowerShell 語法，例如 `Get-ChildItem`；Ubuntu／WSL2 是 Bash，例如 `ls -la`。`argv` 與 `command` 只能選一個。需要互動時加 `tty: true`，再用 `write_stdin` 傳 `chars`、`resize_terminal` 調整 `rows`／`columns`；環境變數與 `cd` 只在同一個還活著的 shell 程序內延續。

## 8. 安裝 plugin 與讀取 skill

先確認來源內容。如果工作 repo 就是本 Harness，將以下參數交給 `extension_install`：

```json
{"task_id":"TASK_ID","kind":"plugin","source":"examples/plugins/project-tools","request_id":"install-project-tools-1"}
```

如果工作 repo 是別的專案，先將這個範例 bundle 複製到該 repo，並把 `source` 改成它在 repo 內的相對路徑。等待安裝 job 成功後，呼叫 `plugins_list`、`skills_list`，參數都是 `{"task_id":"TASK_ID"}`。

探索範例 provider，呼叫 `plugin_tools`：

```json
{"task_id":"TASK_ID","plugin":"project-tools","server":"project"}
```

真正執行統計，呼叫 `plugin_call`：

```json
{"task_id":"TASK_ID","plugin":"project-tools","server":"project","tool_name":"project_stats","arguments":{}}
```

回傳資料來自實際 repo。每次探索／呼叫也可能需要核准。Skill 則從 `skills_list` 取得完整名稱，再以 `skill_read` 的 `name` 讀取；只有明確呼叫 `skill_run` 才執行指定腳本。

Git 來源安裝必須提供 HTTPS URL、完整 commit SHA 與 `subpath`，不能用浮動 branch／tag。安裝不會代為執行 `npm install`、安裝 provider 依賴或完成外部服務登入。[擴充套件完整格式](EXTENSIONS.md)

## 9. 接入 ChatGPT 與延續工作

本機 MCP 已啟動不代表 ChatGPT 可以直接連到你的電腦。請依 [ChatGPT 連線指南](CHATGPT.md) 設定 stdio bridge／Secure MCP Tunnel 或適合用戶端的驗證入口；本專案目前沒有內建 OAuth authorization server。

連線成功後可以對 ChatGPT 說：

> 請用 WebCodex 列出專案，在 local/project 建立任務。先讀規則與 Git 狀態，檢查測試環境，再修正我指定的問題。命令需要核准時回報 job ID。完成後讀取真正的程序結果、整理 diff，保存 task ID、摘要與下一步。

離開前用 `task_update` 保存狀態，例如：

```json
{"task_id":"TASK_ID","status":"active","summary":"已修正加法並通過相關測試；下一步檢查其他呼叫端。","plan":[{"step":"修正加法","status":"completed"},{"step":"檢查呼叫端","status":"pending"}]}
```

摘要必須符合實際結果。下次對話先呼叫 `task_list`，再以原 ID 呼叫 `task_get`；這會恢復保存的工作紀錄，不會還原 ChatGPT 的私有推理。確定所有工作完成後才把 task 狀態改成 `completed`。

## 10. 停止、重新啟動與常見問題

平常停止時，先在 Runner 終端機按 Ctrl-C，再停止 Server。Runner 會停止它管理的命令並保存結果；下次執行同樣的 `serve`／`runner` 命令，沿用既有設定。停止或重新開機後，舊 shell 的記憶體環境不能接管；任務摘要、結果與已保存輸出仍在 state 中。

備份時先停機，再備份設定目錄、Server state、Runner state 與實際 repo。保留設定檔的存取限制，不要將備份公開。Windows 和 WSL2 是不同執行環境，應使用各自的設定與 state，不能同時共用同一 Runner 身分。

| 情況 | 處理方式 |
| --- | --- |
| `uv`、`git` 或 `rg` 找不到 | 在同一個終端機執行對應的 `--version`，確認 PATH；安裝後重開終端機與 Runner |
| 初始化拒絕已有設定 | 沿用現有設定；另一套獨立環境才使用新的 `--config-dir` |
| 專案沒有出現／offline | 確認 Server、Runner 都在執行，URL 與憑證配對，`projects` 路徑仍存在 |
| 命令一直 pending | 執行 `approvals`，檢視內容後 approve 或 deny，再查原 job |
| 401 | 確認命令使用正確的設定檔；client、Runner、操作員憑證不可混用 |
| Windows JSON 引號錯誤 | 使用 UTF-8 `@args.json`，或先設定 UTF-8 再用 stdin 管線傳入 |
| 過期檔案 hash | 重新讀取目前檔案與 diff，再以新 hash 編輯 |
| Windows reparse point 被拒絕 | 使用本機實體資料夾；junction、連結及雲端占位檔案不屬於檔案工具支援範圍 |
| ConPTY 或 Job 啟動失敗 | 保存錯誤與 doctor 結果，確認目標 Windows 版本及組織程序限制；可先以 `tty: false` 區分終端機問題 |
| Windows Rust 找不到 `link.exe` | 確認已安裝 MSVC C++ 工具與 Windows SDK；Runner 會保留 MSVC 探索需要的 `PROGRAMDATA`。使用自訂 Rust 安裝目錄時，也要在 Runner 的 `environment` 設定對應 PATH、`RUSTUP_HOME`、`CARGO_HOME` |
| `unknown`／`interrupted` | 先看檔案、Git、原程序輸出與 task 紀錄，再決定新操作，不能當作未曾執行 |
| ChatGPT 看不到本機 | 回到連線指南完成帳號、tunnel 或 HTTPS 入口設定；本機啟動不會自動建立 ChatGPT 連線 |

本指南採前景執行。要在登入後自動常駐，需要另行配置 Windows 工作排程器或 Ubuntu systemd；目前不會自動安裝系統服務。
