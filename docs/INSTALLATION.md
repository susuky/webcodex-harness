# 安裝、管理與維護

一台電腦先啟動 Server 與 Runner，完成 `projects_list`，再接 ChatGPT。多台電腦共用 Server，各自啟動 Runner；管理頁與任務紀錄沿用同一套資料。

## 第一次工具呼叫

準備 Git、ripgrep 與 [uv](https://docs.astral.sh/uv/getting-started/installation/)。`uv` 會依專案需求選用 Python 3.11 以上版本。

Ubuntu：

```bash
sudo apt install git ripgrep
git clone https://github.com/susuky/webcodex-harness.git
cd webcodex-harness
uv sync --locked
uv run webcodex init --project /absolute/path/to/repo
```

Windows：安裝 [Git](https://git-scm.com/downloads/win)、[ripgrep](https://github.com/BurntSushi/ripgrep#installation) 與 uv，重新開啟 PowerShell。

```powershell
git clone https://github.com/susuky/webcodex-harness.git
Set-Location webcodex-harness
uv sync --locked
uv run webcodex init --project 'C:\src\my-repo'
```

在兩個終端機分別啟動：

```text
uv run webcodex serve
uv run webcodex runner
```

第三個終端機驗證：

```text
uv run webcodex doctor
uv run webcodex call projects_list
```

成功結果包含 `local/project` 與 `online: true`。接著執行完整示範：

```text
uv run python examples/same_turn_demo.py --project local/project
```

它建立任務、執行四秒命令、沿用原 job／process 取得輸出，再保存完成狀態。輸出包含 `demo started`、`demo finished` 與 `exit_code: 0`；[操作展示](DEMO.md)說明在 ChatGPT 中使用相同流程。

ChatGPT 的連線入口、Bearer token 與 tunnel 設定見[連線指南](CHATGPT.md)。設定預設位於 Ubuntu 的 `~/.config/webcodex-harness/`，Windows 的 `%LOCALAPPDATA%\webcodex-harness\`。已有設定時保留原檔。

## 管理頁

開啟 `http://127.0.0.1:8765/manage`。設定帳密後使用帳號與密碼登入；未設定時沿用 `client.json` 的用戶端 Token。使用遠端 Server 時改成該 Server 的入口網址。

管理頁以任務列表與詳情呈現進度，支援來源、專案與狀態篩選。

預設顯示「未完成」，排除已完成與已封存；「全部任務」包含這兩類。「執行／排隊中」只列目前工具或背景工作，「需要處理」只列待回覆、待核准或專案位置變更，「執行狀態待確認」列尚未確認的背景工作。已完成任務即使保留舊程序資訊，也只歸入完成範圍。

列表直接顯示開始時間與最近操作的完整時間、經過秒數；最近操作取自實際工具呼叫，不因管理頁輪詢而刷新。選取任務先顯示已載入的摘要與狀態，執行紀錄再補入；命令輸出預設收合。背景命令的觀察超過 90 秒或執行端離線時，顯示「待確認」；重新讀取原命令的結果後才更新狀態。沒有目前工具或背景工作時顯示「閒置」，歷史錯誤留在執行紀錄，不會讓任務一直顯示待處理，也不會自動把任務標為完成。

具 `write` 權限的檢視者可「標記完成」或「重新開啟」，由來源的狀態端點呼叫既有 `task_update` 保存原任務狀態，保留摘要、計畫與歷史；來源 Token 也須允許 `write`，並已更新至支援狀態端點的版本。標記完成會停用原任務的自動續接通知，已啟動的工具與背景命令繼續執行。只有 `read` 權限時不顯示狀態修改按鈕。

任務詳情可封存或還原。封存項目預設隱藏，可從狀態選單查看「已封存」；封存只保存這位檢視者在任務中心的顯示偏好，不修改來源任務、停止程序或刪除紀錄。偏好按檢視者、來源及任務分開保存，重新登入或重啟後仍保留。

密碼由 scrypt 雜湊保存；登入後使用 HttpOnly、SameSite=Strict Cookie，有效七天，登出或 Server 重啟後失效。Cookie 僅授權管理頁 API，不能呼叫執行命令、Runner 或管理員 API。舊 Token 模式只把 Token 留在頁面記憶體。兩種方式均保留 client 權限與任務 owner 檢查。

在本機安裝環境產生雜湊（密碼不會顯示在終端或 shell 歷史）：

```bash
uv run python -c 'from getpass import getpass; from webcodex.management_auth import hash_password; print(hash_password(getpass("Password: ")))'
```

將輸出的雜湊填入 `server.json`：

```json
{
  "management_login": {
    "client_id": "viewer",
    "username": "operator",
    "password_hash": "<上一步產生的 scrypt 雜湊>",
    "origins": ["http://ll1:8790"]
  },
  "allowed_hosts": ["127.0.0.1", "localhost", "ll1"]
}
```

`username` 是登入帳號，省略時沿用 `client_id`。`client_id` 須對應既有、具 `read` 權限的 client。`origins` 使用完整的協定、主機與埠；它只擴充管理頁的瀏覽器入口，其他工具 API 仍使用 `public_url`。Tailnet 入口需另將 Tailscale 位址轉送至本機服務；Server 本身可繼續綁定 loopback。

### 統一任務中心

多台 Runner 已共用同一個 Server 時，直接使用該 Server 的管理頁。有多個 Server 時，可在作為任務中心的 `server.json` 中，為檢視者設定 `management_sources`：

```json
{
  "clients": {
    "viewer": {
      "token": "<用戶端 API Token，至少 32 字元>",
      "projects": [],
      "permissions": ["read"],
      "management_sources": {
        "ll1": {
          "label": "ll1 · Ubuntu",
          "url": "http://127.0.0.1:8787",
          "token": "<ll1 原用戶端 Token，至少 32 字元>"
        },
        "windows": {
          "label": "Windows",
          "runner_labels": {"wsl": "Windows · WSL"},
          "url": "http://127.0.0.1:18769",
          "token": "<Windows 原用戶端 Token，至少 32 字元>"
        }
      }
    }
  }
}
```

這是 `clients` 欄位的設定範例；保留 Server 的其餘必要欄位。來源 URL 使用 HTTPS，或已建立 SSH 轉送的 loopback HTTP；上述 Windows 埠只是轉送範例。設定檔須維持私有，來源憑證留在 Server，瀏覽器只取得來源名稱。來源 Token 沿用能讀取原任務 owner 的用戶端身分及專案權限。

`projects` 與 `project_roots` 都為空時，任務中心只列出設定的遠端來源；有本機專案時，也會列出「本機」。新增來源不會搬移任務或重新登記 Runner，舊版 Server 的既有管理 API 也可作為來源。

`runner_labels` 可為同一 Server 的執行環境命名；上例將 `wsl` 顯示為「Windows · WSL」，其他 Runner 顯示為「Windows」。介面依環境分組連線與篩選，來源仍只讀取一次。專案選單使用完整任務目錄，保留不同來源、執行環境的同名專案。

任務依實際背景工作、需處理狀態與最近更新排序，搜尋涵蓋來源內的全部任務。不同來源即使任務 ID 相同，選取、輸出與接手指令仍各自獨立。某個來源斷線時，其已載入資料會標示舊紀錄與最後讀取時間，其他來源仍可操作。

在任務詳情複製「接手指令」，貼到可使用對應 WebCodex 連線的新 ChatGPT 對話。新對話以原 `task_id` 讀取目前進度，再明確接手，沿用原 `job_id` 與 `process_id`；任務中心只產生交接內容，不會移交卡片控制權或重跑命令。舊版來源可提供任務摘要、程序與輸出；新對話用 `task_get` 取得完整交接資訊。

「程序」是 agent 啟動的一次命令，例如測試、下載或網站預覽；同一任務可留下多筆歷史命令。Runner 心跳只代表執行端可連線，命令狀態以該命令最後一次觀察為準。管理頁與卡片的內部狀態查詢不計入新工作的排隊數。

「開始時間」使用首次非內部觀察工作的建立時間；尚無工作時顯示任務建立時間。舊版來源未提供時間欄位時，選取任務後讀取第一筆 `task.created` 事件並快取建立時間；不會將最近幾筆工作的時間推測為開始時間，也不會為列表批量讀取完整任務。事件缺失時維持無日期，來源更新後直接使用原有時間欄位。

摘要沿用原任務內容，支援 Markdown 段落、清單及程式碼；顯示排版不會改寫摘要或自動翻譯。`task_update` 指引要求後續摘要使用使用者語言及短段落。模型是否仍在思考沒有直接訊號，頁面只呈現可觀察的工具與背景命令。這個入口不分配 GPU 或其他運算資源。

## 桌面與瀏覽器依賴

| 用途 | Windows | Ubuntu |
| --- | --- | --- |
| 語意控制 | Windows PowerShell 5.1；內建 `UIAutomationClient`、`UIAutomationTypes`、`UIAutomationClientsideProviders` | `python3-gi`、`gir1.2-atspi-2.0`、`at-spi2-core`；桌面 D-Bus session |
| 截圖與座標控制 | `uv sync --locked --extra computer`；互動桌面 | `uv sync --locked --extra computer`；X11 桌面 |
| Playwright | Node.js、Chrome、已安裝的 browser-tools | Node.js、Chrome、已安裝的 browser-tools |

`doctor` 檢查語意控制依賴能否載入。工具操作與原生驗收見[語意桌面](SEMANTIC_DESKTOP.md)、[桌面操作](COMPUTER_USE.md)與[瀏覽器操作](BROWSER_USE.md)。

## 分享診斷

```text
uv run webcodex doctor --share --output diagnostics.json
```

診斷包含版本、平台、工具可用性、設定檢查、Server HTTP 狀態、client 驗證及 Runner 上線數量。分享模式使用欄位白名單：路徑以標記取代，不匯出 Token、設定原文、環境變數值、命令輸出或原始例外訊息。

| 結果 | 處理方式 |
| --- | --- |
| `server_config`／`runner_config` 失敗 | 按 `fields` 的欄位、錯誤類型與數值限制修正；JSON 語法錯誤會提供 `line`／`column` |
| `ConnectError`／`ConnectTimeout` | 檢查 Server 程序、網址、連接埠及防火牆 |
| HTTP 401 | 使用 `client.json` 的 Token，確認 Server 中仍有對應 client |
| HTTP 403 | 比對瀏覽器 origin 與 `public_url` |
| 專案 0 個 | 檢查 Runner 登記及 client 的 `projects`／`project_roots` |
| Runner 上線 0 個 | 啟動該 Runner，確認其 Server URL 與 Runner Token |
| 語意依賴未載入 | 安裝上表套件，從已登入桌面啟動 Runner |

`doctor --config-dir PATH` 可檢查不同設定目錄。Server 與 Runner 分機時，在各自機器執行；未安裝的設定會顯示缺少的項目。

## 更新與回復

更新前保留設定及 SQLite 快照：

```text
uv run webcodex backup
uv run webcodex update-check
```

`update-check` 只讀 Runner 的 process／delivery journal。`runner_restart_ready: false` 或退出碼 2 時，結果會列出仍在執行的 process ID、task ID 與 delivery ID；等這些工作完成後再更新 Runner。

1. 保存目前來源版本或安裝套件，準備新版本並先執行測試。
2. 保留 `backup` 產生的目錄；其中設定檔與 SQLite 快照採私有權限。
3. Server 與 bridge 可分開更新。Runner 仍有工作時，讓原 Runner 繼續連線。
4. `update-check` 通過後，停止 Runner、更新套件並啟動，再執行 `doctor` 與 `projects_list`。
5. ChatGPT 重新整理連線 metadata，開啟新卡片。

若需回復，停止受影響的 Server／Runner，重新安裝先前套件或切回保存的來源版本，再還原備份中對應的設定與 SQLite 檔案。回復前另做一次當前備份。process log 與 browser profile 留在原 state 目錄；`backup` 的 manifest 列出實際保存的資料庫。SQLite 的 `-wal`／`-shm` 必須在程序停止後隨舊資料庫一起移出，讓回復的資料庫重新建立。

Linux systemd user service 可依部署名稱執行 `systemctl --user restart webcodex-server.service`；Runner 通過檢查後再重啟 `webcodex-runner.service`。Windows 依原本終端機或[常駐啟動腳本](../examples/windows/Start-WebCodex.ps1)重新啟動。

## 容量與保留期限

設定保留三十天、容量目標 1 GiB，先看預覽：

```text
uv run webcodex retention --days 30 --max-mib 1024 --save-policy
```

確認清單後套用：

```text
uv run webcodex retention --apply
```

政策保存在設定目錄的 `retention.json`；每次呼叫依天數與容量挑選最舊資料。清理範圍為已結束 process log、已完成 task 的 browser artifacts、Server job payload 中的輸出與截圖，以及 Runner 已確認送達的副本。正在執行、結果未知、待處理工作、browser profile 與安裝檔都保留；task／job ID、owner、狀態、結束碼與時間也保留。

容量統計針對可清理的 payload、log 與 artifact。保護中的資料與 SQLite metadata 持續保留；保護資料超過目標時，結果顯示 `capacity_blocked_by_protected_data`。`removed_file_bytes` 是已刪除檔案的容量，`reclaimed_payload_bytes` 是 SQLite 可重用的空間。若要縮小 SQLite 實體檔案，在服務停止的維護時段備份後執行 `VACUUM`。

Runner 分機部署時，只能清理本機可驗證的紀錄；完成 task 的附件與 Server payload 清理需可讀取對應 Server state。政策只在執行 `retention` 時套用。

`retention` 目前要求設定目錄內有 `runner.json`；只有 Server 的部署需提供對應的 Runner 設定與可讀取的 state 路徑，才可使用這個清理命令。
