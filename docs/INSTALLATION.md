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

開啟 `http://127.0.0.1:8765/manage`，輸入 `client.json` 的用戶端 Token。使用遠端 Server 時改成該 Server 的入口網址；`public_url` 必須與瀏覽器實際 origin 相符。

管理頁列出該 client 可見的 Runner、任務、程序、上次工具活動、最後輸出與錯誤。選取任務可展開命令輸出；每十秒觀察正在執行的原程序。Runner 斷線或頁面失去連線後，顯示舊紀錄與最後觀察時間。

Token 只留在目前頁面的記憶體，登出後清除。API 使用既有 client 權限與任務 owner 檢查；管理頁不會建立新命令，也不會傳送 ChatGPT 訊息。

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
