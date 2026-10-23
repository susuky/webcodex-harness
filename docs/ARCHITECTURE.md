# 架構與執行契約

## 分工

Server 不直接讀取專案目錄。它提供 MCP Streamable HTTP 與相同工具的 REST adapter，驗證用戶端身分，保存工作，並將操作交給擁有專案的 Runner。Runner 只主動連線，不需要對外開放入站服務。

Server 與 Runner 目前採 HTTPS long polling；同機開發允許 loopback HTTP。結果完成時立即另送 HTTP 回報，不必等待下一次 polling。MCP 使用官方 Python SDK，並採 stateless transport；業務任務由 SQLite 保存，與 MCP transport session 明確分開。[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)

這個分工參考 [WebCodex 架構](https://github.com/yyjeqhc/webcodex/blob/main/docs/ARCHITECTURE.md)。檔案、命令、工具探索與外部 MCP 是 coding harness 的共同組成；主推理循環留在 ChatGPT／MCP 用戶端。選用的 [子代理](SUBAGENTS.md) 透過 Runner 的 Codex CLI 執行獨立子任務，主對話負責分派與整合。[Claude Code agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop)

## 身分與專案

- 每個用戶端有自己的 token、專案清單、選用父目錄清單與 `permissions`。
- 每台 Runner 使用獨立 token，不能拿它呼叫 MCP 或操作員 API。
- 操作員 token 用於回答外部問題；MCP 工具清單不暴露這個操作。
- `runner_id/project_id` 指向已登錄專案。綁定包含 canonical path、檔案系統裝置與 inode 的雜湊；更換根目錄需要建立新任務。
- 任務與工作紀錄依用戶端身分隔離。共同獲准的專案使用共享的真實檔案系統。

`read`、`write`、`execute`、`git_write`、`extensions` 決定 coding 工具類別。選用的 `projects` 授予建立與登記專案，`computer_observe`／`computer_control` 分別授予桌面觀察與輸入，預設皆不啟用。通過用戶端與 Runner 的授權檢查後直接執行。

`project_create`／`project_register` 不接受 coding task ID。Server 依 client 與父目錄身分重用 `kind=project` 的管理紀錄，沿用工作佇列與去重；一般任務為 `kind=coding`，兩者不能混用工具。Runner 在父目錄內執行操作，將登記寫入自己的 SQLite，再更新 Server 專案清單，最後回報工作成功。根目錄授權涵蓋其中動態登記的專案；不同 client 的任務紀錄仍分開。設定與限制見 [建立專案](PROJECTS.md)。

權限設定在啟動時載入。修改設定並重新啟動 Server 後，尚未交付的工作會在送給 Runner 前重查權限，已撤銷授權的工作會失敗。已交付或執行中的作業不會因設定變更自動回溯取消。

Server 啟動時會將仍有授權的舊待核准工作移回原佇列，保留 job ID 與去重紀錄；其他舊待核准工作會取消。框架不提供逐筆核准或專案信任門檻，升級方式見[授權與執行](EXECUTION.md)。

## 工作狀態

```mermaid
stateDiagram-v2
  [*] --> queued: 授權檢查通過
  queued --> cancelled: 用戶端取消
  queued --> running: 指定 Runner 領取
  running --> succeeded: 收到結果
  running --> failed: 收到確定的錯誤
  running --> unknown: 更換 Runner 或領取結果遺失
  unknown --> succeeded: 原執行實例補交結果
  unknown --> failed: 原執行實例補交錯誤
```

SQLite 的 `UNIQUE(owner, task_id, request_id)` 讓相同請求取得原工作。執行前先在 Runner journal 落盤，再啟動工具；完成結果在收到 Server acknowledgement 前保留。回報重試不能覆寫已接受的終態。

領取回覆遺失、程序崩潰或外部工具斷線可能讓結果不確定；系統標示 `unknown`，不把修改操作自動放回佇列。重連時 Runner 提供已知工作清單，Server 會辨識「已領取，但 Runner 沒收到」的工作。

Runner 的 process lock 防止同一 state directory 同時啟動兩個實例；Server 的 15 秒實例租期避免另一台機器用同一 Runner 身分搶走工作。短暫網路斷線不會終止現有子程序。

## 任務續接

既有 task 可保存一筆尚未消費的等待，記錄原 job 與後續程序觀察 job。觀察採既有工作去重機制，不建立第二次命令執行。任務卡片透過 App 專用 `task_app` 呼叫更新進度；卡片協調仍受任務擁有者、專案及工具權限檢查，不提供核准或任意執行能力。

SQLite 保存控制者、世代、120 秒租期與已退役卡片，避免舊卡片反覆搶回控制。視窗識別、卡片識別與消費憑證以雜湊保存。每次狀態轉移使用短交易；通知先從 `ready` 轉成 `prepared`，再由前景卡片請求 `ui/message`，收到回覆記為 `accepted`，結果不明記為 `unknown`。只有 `task_resume` 驗證確切等待與憑證後才轉成 `consumed`，重複消費會明確標示。

已準備或已送出的通知在控制者失效時轉為 `unknown`，不自動重送。原通知的遲到消費仍可確認同一筆尚未被取代的等待；人工恢復後取代等待會使舊憑證失效。暫停時禁止準備及消費，任務完成時取消未消費等待。卡片關閉期間沒有背景模型或排程工作者，重新開啟後從持久紀錄恢復觀察。

## 檔案與 Git

結構化檔案工具透過 directory fd 和 `O_NOFOLLOW` 開啟檔案；只接受一般且無 hardlink 的檔案，寫入先建立暫存檔，再替換。新檔案採 create-if-absent，既有檔案需要雜湊相符。

上述 directory fd 路徑用於 Ubuntu／WSL2。Windows 原生以 `CreateFile` 的 `OPEN_REPARSE_POINT` 逐層檢查，開啟目錄時不授予 delete sharing，讓祖先目錄在操作期間不能被改名或替換；拒絕 junction、symlink、其他 reparse point、裝置名稱、ADS 與私有目錄別名。新檔案使用 Windows 不覆寫目的地的 rename，覆寫使用替換。Windows 設定目錄以 DACL 限定目前帳號與 SYSTEM；Linux 使用目錄 0700、設定檔 0600。

Runner 對同一專案的結構化寫入加鎖；shell 與外部編輯器仍可同時修改 repo。Workspace 以不跟隨連結的方式開啟根目錄，核對 task 綁定的目錄身份；每次開啟檔案時再核對身份，沿用已開啟的 directory fd 或 Windows pinned handles。專案技能與本地擴充來源的子 Workspace 也保留原任務的根目錄綁定。Patch 先完整檢查再套用，不支援任意二進位、rename／copy 或連結型別的變更。結構化 Git 使用專案外、絕對 PATH 目錄中的執行檔，關閉 hooks、fsmonitor、log 簽章驗證與 Git transport，避免缺失物件觸發下載及外部程式；diff 另關閉外部 diff／textconv。Ubuntu／WSL2 的 Git 程序以已開啟的目錄 fd 對應 `/proc` 路徑啟動，Windows 在 Git 操作期間保留祖先目錄 handles。

`search_files` 安全開啟每頁最多 500 個檔案，串流複製至 Runner 私有目錄中的快照，再以可信 ripgrep 搜尋。每頁快照最多 20 MiB，超過時需縮小 path／glob；回傳保留原始相對檔名與行號。快照保留至程序結束後清除；`next_file_offset` 探索後續檔案，程序輸出游標則讀取同一頁的剩餘結果。

## 命令生命週期

每個命令有獨立程序群組。標準輸出與錯誤合併寫入 Runner log，記錄總位元組數和截斷狀態。逾時、取消或主要程序退出時回收同一程序群組，避免繼承 stdout 的背景子程序讓讀取永久卡住。刻意呼叫 `setsid` 脫離群組的程序不在這個回收保證內。

Windows 命令改用獨立 Job Object，並設定 `KILL_ON_JOB_CLOSE`。可信的 Python bootstrap 先等待事件；Runner 將它加入 Job Object 後才放行目標命令，避免目標程序先產生尚未受管理的子程序。無法加入 Job 時拒絕啟動。取消與逾時直接終止 Job，Runner 崩潰也會關閉其 Job handle。命令以 Runner 帳號權限執行。

PTY 供互動式工具使用，可傳入字元、Ctrl-C、EOF 並調整大小；未實作完整作業控制終端。要延續環境變數或工作目錄，必須繼續使用同一個 live shell process，而非另開一個 `exec_command`。

Windows 的 `tty=true` 使用 pywinpty 的 ConPTY；輸入換行轉成 console Enter，EOF 傳入 Ctrl-Z／Enter。`command` 在 Windows 使用 PowerShell（優先 `pwsh`，其次系統 Windows PowerShell），在 Linux 使用 Bash；`argv` 保留明確的執行檔與參數。Python skill 使用 Runner 的 Python 執行檔；Bash／Node skill 仍需安裝相應工具。

命令環境只繼承明確允許的變數，再套用 Runner 設定中的 `environment`。Windows 額外保留系統目錄、使用者資料與暫存路徑，以及 MSVC 安裝探索需要的 `PROGRAMDATA`；不會把所有登入環境變數傳給命令。自訂工具鏈需要的額外變數應在 Runner 設定中明確提供。

Runner 重啟不嘗試根據舊 PID 接管程序，以免誤操作已被重用的 PID。舊 log 仍可讀，舊 active session 標示為 `interrupted`。

## 模組

桌面工具由同一 Runner 共用一把 thread lock，避免不同專案的輸入互相穿插；等待中的 coroutine 取消不會提早釋放正在執行原生呼叫的鎖。截圖 ID 綁定 task、最新觀察、畫面尺寸及五分鐘期限，一次輸入即消耗；可能已送出輸入的例外使用既有 `OutcomeUnknown`，不自動重送。此機制不隔離人為操作或另一個 Runner。完整契約與平台限制見 [Computer use](COMPUTER_USE.md)。

| 檔案 | 職責 |
| --- | --- |
| `src/webcodex/server.py` | MCP／REST、驗證、權限、操作員 API、任務工具 |
| `src/webcodex/store.py` | SQLite 工作、事件、去重、領取與恢復 |
| `src/webcodex/continuation.py` | 任務等待、卡片租期、通知防重與消費 |
| `src/webcodex/task_app.html` | 任務進度卡片、前景續接與暫停控制 |
| `src/webcodex/runner.py` | 主動連線、journal、工具分派與結果回報 |
| `src/webcodex/runtime.py` | Repo、Git、命令、圖片及擴充工具 |
| `src/webcodex/projects.py` | 父目錄驗證、專案建立與登記、Runner 持久登記恢復 |
| `src/webcodex/computer.py` | 選用桌面後端、截圖、座標檢查與輸入序列化 |
| `src/webcodex/filesystem.py` | 路徑邊界、檔案讀寫與雜湊檢查 |
| `src/webcodex/processes.py` | 子程序、PTY、輸出與停止流程 |
| `src/webcodex/subagents.py` | Codex worker、並行額度、持久狀態與最後回答 |
| `src/webcodex/windows.py` | Windows 檔案 handle、ACL、ConPTY、Job 與啟動閘門 |
| `src/webcodex/extensions.py` | 擴充安裝、skill 探索、持續存在的 MCP provider |
| `src/webcodex/catalog.py` | 從同一份 Python 簽名產生 schema 與工具描述 |
| `src/webcodex/bridge.py` | stdio MCP 與有驗證的 HTTP MCP 之間的轉接 |
| `src/webcodex/cli.py` | 初始化、啟動、問題回覆及工具呼叫 |

## 尚需外部環境驗證

ChatGPT 帳號、Secure MCP Tunnel、公開 TLS、反向代理與第三方 MCP provider 的連線，需以實際帳號、目標與執行環境逐一驗證。

原有 coding 功能曾在 Windows 11、WSL2 Ubuntu 22.04 與 Ubuntu 24.04 驗收；新增桌面功能的當次測試結果、平台版本與待驗證項目見 [平台驗證紀錄](PLATFORMS.md)。
