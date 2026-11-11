# WebCodex 連線與效能檢查

檢查日期：2026-11-11

這次找到兩組直接影響穩定性的問題：Runner 會被慢速結果回傳拖住，Windows 的 SSH client 則實際發生程序崩潰。另有資料庫查詢、心跳寫入頻率、HTTP 連線重用與 Windows 啟動順序的問題。

Ubuntu 的 Server、Runner 與任務中心已更新並重新量測，WSL Runner 也已更新並上線。Windows 的啟動、重連與 WSL 控制流程已更新；Windows 核心完整套件與 Git SSH 設定已準備，需由管理員啟動更新程序。

## 主要問題與修正

| 問題 | 原本的行為 | 修改後 |
| --- | --- | --- |
| 結果回傳阻塞 Runner | 每輪取工作之前等待結果回傳；已完成命令仍占執行名額 | 獨立、由事件喚醒的回傳迴圈；命令完成釋放名額，心跳繼續更新 |
| 空閒時反覆寫 SQLite | 每 100 毫秒檢查工作都提交一次心跳 | 每 5 秒保存心跳，工作仍每 100 毫秒檢查 |
| 大任務的程序查詢昂貴 | 對完整程序觀察紀錄執行兩個 window functions | 使用現有索引找程序起點，只讀取要顯示的 100 個程序最新狀態 |
| 管理頁反覆開來源連線 | 每個來源請求建立新的 HTTPX client | 依檢視者、來源、網址與憑證重用連線；服務結束時關閉 |
| Windows 入口修復被拖住 | 先啟動 SSH／WSL，再檢查主要 Tunnel | Server、Runner 與主要 Tunnel 優先恢復，選配元件各自處理錯誤 |
| SSH 結束後等待排程 | helper 退出，等待每分鐘排程 | SSH 結束後 5 秒重試；重連期間持有單一實例鎖 |
| WSL 啟動命令無等待上限 | 同步等待 wsl.exe，可能一直占用啟動鎖 | 每次控制命令最多 15 秒，逾時結束該次前景控制程序 |
| SSH 關閉 pipe 的清理卡住 | Windows 的 EINVAL 或仍存活的 child 可能讓 helper 無法回復 | 處理 Windows 關閉 pipe 的錯誤，先回收自己啟動的 SSH child |
| AST 檢查污染 Runner 日誌 | 靜態解析把使用者程式的 SyntaxWarning 寫入服務日誌 | 只在解析期間抑制警告，實際執行的程式照常輸出警告 |
| 正式版本落後 repo | 正式服務固定載入舊 release，repo 更新未生效 | Ubuntu 三個服務改為已測的新 release，並確認實際執行路徑 |

### Runner 結果傳送

回歸測試把 /api/runner/complete 故意卡住，並將同時執行工作上限設為 1。修改前，第二個工作無法執行；修改後，第二個工作完成，解除阻塞後兩份結果都送達。

另一個案例模擬 Server 已收到結果，但 acknowledgement 遺失。重送沿用原本的工作 ID，命令只執行一次。結果先保存在既有 SQLite journal，網路恢復後繼續送出。

### 心跳寫入量

15 秒的測試由 150 次 SQLite 提交降為 3 次，減少 98%。離線判斷仍使用原本的 15 秒時限；新工作領取與執行中狀態都有回歸測試。

## 效能實測

正式 Server 資料庫約 5,975 MiB，最大任務超過十萬筆工作紀錄。舊正式 release 缺少 repo 既有的 pending_jobs partial index，單次 task_activity() 約 346–364 毫秒，其中約 241 毫秒耗在待執行工作的查詢。

新 release 讓既有索引 migration 生效，並使用本次改寫的程序查詢。部署後，102,468 筆工作紀錄的任務連續五次量測為：

67.08、66.70、72.96、72.70、68.28 毫秒。

中位數為 **68.28 毫秒**，相對原本約 350 毫秒減少約 **80%**。

### 程序查詢的獨立比較

使用正式資料庫的同一份唯讀 SQLite snapshot，新舊查詢在五個任務回傳的結果相同。

| 任務中的工作紀錄數 | 原查詢 | 修改後 |
| ---: | ---: | ---: |
| 102,204 | 52.58 ms | 15.49 ms |
| 71,692 | 19.03 ms | 6.27 ms |
| 36,288 | 28.53 ms | 8.08 ms |
| 23,525 | 12.63 ms | 4.50 ms |
| 2,512 | 1.69 ms | 5.06 ms |

大任務的成本明顯下降；小任務本輪增加約 3 毫秒。執行中程序的總數仍涵蓋畫面前 100 筆以外的工作。

### 連線與記憶體

真實 TCP 測試的六次請求原本建立六條連線，修改後建立三條，分別屬於三組檢視者／來源。測試確認 Authorization 分離、來源憑證更換與 client 關閉的行為。

每個來源 pool 最多 20 條連線，其中最多保留 5 條閒置連線；keep-alive 為 30 秒，涵蓋管理頁的 10 秒更新週期。錯誤訊息能區別連線逾時、來源回應逾時與其他連線失敗。

Ubuntu Runner 更新後量到約 67 MiB RSS；更新前約 446 MiB。這組記憶體數值包含重新啟動的影響，長時間使用後的變化可從後續服務觀察繼續追蹤。主要確定改善是查詢成本、心跳寫入量及連線重用。

## Windows 斷線的直接證據

### 原生 SSH 程序崩潰

Windows Application Event 1000 在當地時間 21:34:16、21:54:07、21:56:49、21:56:56 記錄了四次 Windows 內建 ssh.exe 崩潰，例外碼均為 0xC0000374。Microsoft 將它定義為 STATUS_HEAP_CORRUPTION。

當時的 Windows SSH 為 OpenSSH_for_Windows_9.5p2，AdGuard 的 adgnetworksockdrv 正在執行。AdGuard 上游 #6123 記錄了相同 SSH 版本、SockFilter 與「IO is still pending on closed socket」訊息的組合。這支持優先避開該 client 組合；本次沒有改動 AdGuard 設定。

同一台電腦現有 Git for Windows 提供 OpenSSH 10.6p1。使用相同帳號、既有金鑰、known_hosts 與 StrictHostKeyChecking=yes 做隔離實測：

| client | 遠端執行 true | 結束訊息 |
| --- | ---: | --- |
| Windows 原生 SSH | exit 0，357 ms | 出現 IO is still pending on closed socket |
| Git SSH | exit 0，313 ms | stderr 空 |
| Git SSH，使用正式 launcher 的反斜線路徑 | exit 0，315 ms | stderr 空 |

helper 新增可選的 --ssh-executable，launcher 明確傳入路徑。正式設定已準備在下次啟動時使用 Git SSH，範圍限 WebCodex 的任務中心通道。

### TCP 卡住與回復

22:04 左右的唯讀觀察發現兩條舊 SSH 連線已約 348 秒與 538 秒收不到確認，TCP 重傳間隔都退到 120 秒。當時兩種 SSH client 都曾遇到 8 秒連線逾時，之後又恢復；普通 TCP 與 SSH banner 測試成功約需 33 毫秒。現有觀察尚無法在網路鏈路、Tailscale 與過濾驅動間確定這段停滯的原因。

因此保留快速偵測與持續重試：ServerAliveInterval 由 30 秒調為 10 秒、ServerAliveCountMax 保持 3，無回應偵測約由 90 秒縮為 30 秒；SSH 結束後 5 秒重試。若每次連線都用滿 8 秒逾時，嘗試間隔約 13 秒。遠端既有 heartbeat watchdog 仍會釋放失去心跳的專用 session。

受控關閉一次專用 SSH session 後，已部署的新 helper 在 **8.8 秒**恢復，Windows 來源的 healthz 回傳 200。這次量測使用當時仍在執行的 Windows 原生 SSH；Git SSH 正式切換待管理員更新啟用。

## 部署狀態

| 元件 | 狀態 |
| --- | --- |
| Ubuntu Server | 已更新、重啟、確認健康與正式查詢效能 |
| Ubuntu Runner | 已更新、重啟、確認能領取及完成工作 |
| Ubuntu 任務中心 | 已更新、重啟，使用來源連線 pool |
| Windows 啟動順序、WSL 控制與 SSH 重試 | 已部署，原生測試及實際啟動通過 |
| WSL Runner 核心 | 已更新完整套件，重新登錄並確認上線 |
| Windows 核心完整套件 | 已測並準備更新，待管理員套用 |
| Windows 任務中心改用 Git SSH | 設定已準備，待重新啟動既有 Session 0 helper |

Ubuntu 三個服務使用 20261111-connection-reliability release。更新時保留原有 12 個獨立背景程序；更新後再確認它們仍在執行。

Windows Server 和 SSH helper 位於 Session 0。目前 Runner 的一般權杖對其查詢與終止權限均收到 WinError 5 / AccessDenied。管理員更新腳本只處理既有 WebCodex 元件，啟動時沿用原本 Limited 權限的排程。

WSL 更新前確認沒有執行中的程序或 delivery，服務 cgroup 也只有 Runner 本身。完整套件的本機切換與啟動共 1.337 秒；新 Runner 約 16 秒後完成重新登錄。重新登錄後的日誌沒有新警告，更新後約 60.4 MiB RSS，工作 journal 仍為空。

Windows 與 WSL 的舊套件已逐檔比對到 repo 既有版本，沒有額外客製程式。Windows 更新採完整的 28 個套件檔案，讓 import_image 等既有功能的相依模組一起對齊。

Windows 更新腳本會先列出尚在執行的工作並退出；工作結束後可再次執行。它在停止 Server、阻止新工作進入後會再次檢查，才更新 Runner。更新後至少等待超過舊的 15 秒心跳租約，確認兩個 Runner 的新心跳，以及 Git SSH 的實際 listener。

原生唯讀預檢已實際觸發有工作時的延期分支，回傳預期的 exit 2。先前的 51510 預覽於 22:09:05 按原設定的 3,600 秒時限結束；更新腳本依執行當下的 journal 判斷工作狀態。

### Windows 套用方式

以系統管理員身分開啟 PowerShell，執行安裝目錄下 updates/20261111-reliability/apply-core-update.ps1。完整套件與 README.md 已放在同一目錄。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<安裝目錄>\updates\20261111-reliability\apply-core-update.ps1"
```

同一命令加上 -CheckOnly 可先做唯讀檢查。腳本沿用既有 Limited Keepalive 排程啟動服務。套用細節見 [CORE_UPDATE.md](../../examples/windows/CORE_UPDATE.md)。

## 驗證

| 範圍 | 結果 |
| --- | --- |
| Linux 完整測試套件 | 528 passed，16 skipped |
| Windows 核心隔離測試 | 80 passed |
| 後續 Windows SSH／啟動原生測試 | 9 passed |
| Windows image_import 相依性測試 | 3 passed |
| Windows 管理員更新腳本 | 原生 parser 通過；唯讀預檢成功列出工作；獨立程式審閱通過 |
| 真實 WSL 控制呼叫 | hostname -I 成功；引號相容性已修正 |
| 靜態與格式檢查 | Ruff check、Ruff format、git diff --check 通過 |
| 套件建置 | uv build 通過 |
| 來源 HTTP 連線 | 真實 TCP 重用、不同憑證分離、關閉與逾時測試通過 |
| 正式 Ubuntu 更新 | 三個服務 active，原有背景程序保留，實際工具呼叫成功 |
| 正式 WSL 更新 | 新 Runner active，上線心跳確認，0 個執行中工作 |

Windows 原生案例涵蓋重連、單一實例鎖、關閉 pipe、SSH 執行檔路徑，以及 WSL 成功、失敗、逾時與子程序清理。更新腳本的實際管理員套用結果留待該步驟執行後記錄。

## 最值得增加的工具

建議增加 Server 端唯讀的 **connection_status**。

目前 CLI 的 doctor 已能檢查 Server、認證、Runner 與桌面依賴，MCP 還缺少一次查看各條連線的入口。新的工具應直接從 Server 回傳：

- Server 與已授權的各個管理來源是否可達、回應時間及逾時類型。
- 各 Runner 最後心跳與距今秒數。
- 結果待傳數、目前載入的程式路徑與版本。
- 任務中心 SSH／WSL 的最新啟動結果。

這次 Windows 工具入口與管理頁來源曾有不同狀態，統一顯示這些資訊能直接指出斷在哪一段。實作可重用現有 doctor、資料庫與 Sources client pool，從 Server 回應，避免依賴已離線的 Runner 派發診斷命令。本輪先完成連線與效能修正，這個工具列為後續功能。

## 相關程式與來源

- [Runner 與結果傳送](../../src/webcodex/runner.py)
- [資料庫與工作狀態查詢](../../src/webcodex/store.py)
- [管理頁來源連線](../../src/webcodex/management_sources.py)
- [Windows 常駐啟動](../../examples/windows/start-windows-webcodex.ps1)
- [SSH heartbeat client](../../examples/windows/task-center/ssh-heartbeat-client.py)
- [HTTPX 非同步 client 與連線重用](https://www.python-httpx.org/async/)
- [OpenSSH 連線存活檢查](https://man.openbsd.org/ssh_config)
- [Microsoft：STATUS_HEAP_CORRUPTION](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-erref/596a1078-e883-4972-9bbc-49e60bebca55)
- [AdGuard #6123：原生 OpenSSH 與 SockFilter](https://github.com/AdguardTeam/AdguardForWindows/issues/6123)
