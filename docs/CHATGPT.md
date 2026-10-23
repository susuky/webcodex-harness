# 連接 ChatGPT 與其他 MCP 用戶端

## 先確認本機可用

啟動 Server 與 Runner，執行 `webcodex call projects_list` 確認專案出現，並完成一個 `read_file` 或測試命令。如果尚無專案，先用 `project_roots_list` 確認允許的父目錄，再依 [建立專案](PROJECTS.md) 建立。Ubuntu、Windows 原生與 WSL2 的完整命令見 [操作教學](OPERATIONS.md)。

一般用戶端可直接使用 `/mcp` 與 `client.json` 的 Bearer token，也可以啟動 `webcodex bridge --config /absolute/path/client.json`。bridge 對外是 stdio MCP，並在本機讀取 token；它不會把 token 放進工具參數、URL 或 stdout。

例如支援 stdio 的 MCP 用戶端可設定：

```json
{
  "mcpServers": {
    "webcodex": {
      "command": "/absolute/path/webcodex/.venv/bin/webcodex",
      "args": ["bridge", "--config", "/absolute/path/client.json"]
    }
  }
}
```

Windows 原生用戶端的範例，請把帳號與路徑換成實際值：

```json
{
  "mcpServers": {
    "webcodex": {
      "command": "C:\\src\\webcodex\\.venv\\Scripts\\webcodex.exe",
      "args": ["bridge", "--config", "C:\\Users\\YOUR_USER\\AppData\\Local\\webcodex-harness\\client.json"]
    }
  }
}
```

Windows 用戶端要連到 WSL2 裡的 Harness，可以讓它啟動 `wsl.exe`：

```json
{
  "mcpServers": {
    "webcodex": {
      "command": "wsl.exe",
      "args": ["--distribution", "Ubuntu", "--exec", "/home/YOUR_USER/src/webcodex/.venv/bin/webcodex", "bridge", "--config", "/home/YOUR_USER/.config/webcodex-harness/client.json"]
    }
  }
}
```

WSL2 範例要求 Server、Runner 與 client 設定都在相同 Ubuntu 發行版內。這些設定供支援 stdio 的本機 MCP 用戶端使用；ChatGPT 遠端連線使用下方的 Secure MCP Tunnel 或 HTTPS 入口。Windows 原生及 WSL2 內的 stdio bridge 均已通過實機測試；Windows 用戶端透過 `wsl.exe` 啟動的組合仍待驗證。2026-10-23 已透過實際 ChatGPT 外掛完成 Windows 原生 Tunnel 連線與三螢幕桌面操作，結果見[平台驗證](PLATFORMS.md)。

## ChatGPT：Secure MCP Tunnel

OpenAI 官方文件提供從私有網路主動連線的 Secure MCP Tunnel，支援 stdio 與 HTTP MCP。它需要你帳號下的 tunnel ID、執行用 API key，以及對應工作區權限。[官方設定指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

1. 在 Platform 的 tunnel 設定建立或選擇 tunnel，確認已關聯要使用的 ChatGPT workspace。
2. 依官方指南安裝 `tunnel-client`，在本機安全設定 `CONTROL_PLANE_API_KEY`。不要把值貼入對話、repo 或命令輸出。
3. 使用本專案已驗證的 stdio bridge 作為 tunnel 的本機 MCP command：

```bash
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile webcodex \
  --tunnel-id YOUR_TUNNEL_ID \
  --mcp-command "/absolute/path/webcodex/.venv/bin/webcodex bridge --config /absolute/path/client.json"

tunnel-client doctor --profile webcodex --explain
tunnel-client run --profile webcodex
```

4. 在 ChatGPT「外掛程式 → 新增 → 新增自訂 MCP 伺服器」建立連線，選擇「通道」與對應 tunnel。使用上述本機 bridge 時，連線的身分驗證選「無需身分驗證」；bridge 仍使用本機 client 憑證驗證 Server。完成建立與連接，並保留本機 Server、Runner 和 tunnel-client 運作。
5. 確認探索到的工具清單後，開啟對話並加入這個連線；先試 `projects_list`，再建立任務。介面可用性由帳號及工作區政策決定。[ChatGPT 官方連線步驟](https://developers.openai.com/plugins/deploy/connect-chatgpt)

Server 的 Bearer 驗證由 bridge 處理，接入後仍保持啟用。

本機 `tunnel-client` 主動建立對外連線，讓 ChatGPT 的工具請求經 tunnel → stdio bridge → Server → Runner 到達專案工具。檔案操作由本機 Runner 執行，範圍由專案與父目錄設定決定。[Secure MCP Tunnel 運作方式](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

## 換帳號與換電腦

### 連線名稱與執行位置

ChatGPT 中的 `WebCodex ll1` 等名稱是連線標籤。連線指向 WebCodex Server；Server 再把工作交給任務所屬專案的 Runner。換一台電腦開啟 ChatGPT，仍會操作原本選定的 Runner。

| 情境 | 是否需要另一個外掛或連線 |
| --- | --- |
| 同一 ChatGPT 帳號與工作區，改用 Windows 或另一台電腦開啟 ChatGPT 網頁 | 選用原本已安裝的外掛 |
| 換另一個 ChatGPT 帳號 | 在新帳號安裝可用的外掛或建立自訂 MCP 連線，完成連線授權 |
| 讓工具實際在另一台 Windows 電腦執行 | 將該電腦的 Runner 與專案登記到共用 Server，授權用戶端使用該專案；可沿用同一外掛 |
| 另一台電腦使用獨立的 WebCodex Server | 另外連接該 Server 的入口，可取不同名稱區分 |

Server 可管理多個 Runner。需要在 Windows 執行命令或操作桌面時，選擇 Windows Runner 登記的專案；安裝與桌面設定見 [Windows 安裝指南](WINDOWS_RUNNER.md)。

### 新帳號的外掛與 tunnel 授權

自訂 MCP 外掛建立在個人帳號或指定工作區，新帳號需有可安裝、連線及使用它的權限。可沿用適用的既有 Server 與 Runner；在新帳號建立連線，不需要重建後端。[OpenAI 自訂 MCP 連線說明](https://developers.openai.com/api/docs/guides/custom-mcp-server)

使用 Secure MCP Tunnel 時，還需確認：

1. tunnel 已關聯新帳號要使用的 ChatGPT 工作區。
2. 建立連線的使用者具有所屬 Platform 組織的 Tunnels Read + Use 權限。
3. `tunnel-client` 與原 Server／Runner 持續運作。

同一個 tunnel 可關聯多個 Platform 組織與 ChatGPT 工作區，沿用相同 `tunnel_id`。若新帳號看不到原 tunnel，先檢查工作區關聯及權限；需要另一個 tunnel 時，仍可將它連到相同的 WebCodex Server。[OpenAI tunnel 關聯與權限](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#associate-tunnels-with-the-right-organizations-and-workspaces)

### 換帳號後接續原任務

接續既有任務需保留原 Server 的任務資料庫、相同的 WebCodex 用戶端身分，以及原專案授權。

WebCodex 依 Server 中的用戶端身分隔離任務。stdio bridge 使用 `client.json` 的 Bearer 憑證驗證；兩個 ChatGPT 帳號若共用同一組 bridge／client 憑證，就會取得相同的 WebCodex 用戶端身分與已授權任務。

共用同一台 Runner 不會自動共用任務。需要隔離使用者時，設定不同的 WebCodex 用戶端身分與對應的 bridge／client 憑證，各自依專案授權與任務紀錄工作。

新帳號完成連線後：

1. 呼叫 `projects_list`，確認連到預期的專案與 Runner。
2. 呼叫 `task_list` 找出原任務，或直接以已保存的任務 ID 呼叫 `task_get`。
3. 依目前進度接續原任務，並以 `task_present` 顯示卡片。卡片控制權規則見 [任務卡片與跨回合續接](#任務卡片與跨回合續接)。

將 `TASK_ID` 換成原任務 ID，在新對話輸入：

> 請用 WebCodex 讀取任務 TASK_ID，確認最新進度並接續原工作，顯示任務卡片。

## 公開 HTTPS 入口

若你已有公開網域與反向代理，可將 HTTPS `/mcp` 轉發至 Server，並將 `server.json` 的 `public_url`、`allowed_hosts` 設為實際入口。其他機器的 Runner 必須使用 HTTPS；憑證驗證保持開啟。

本專案目前實作靜態 Bearer 驗證，**沒有 OAuth authorization server**。支援 Bearer 的 MCP 用戶端可直接使用；要求 OAuth 的 ChatGPT 連線或公開 plugin，需加上符合 OAuth 2.1、資源探索與 PKCE 契約的入口。[官方驗證文件](https://developers.openai.com/plugins/build/auth)

如果只需要自己的 ChatGPT 操作本機 repo，前述 stdio bridge／Secure MCP Tunnel 是已準備好的私有接入路徑。

## 建議的第一個請求

建立全新專案：

> 請列出 WebCodex 允許建立專案的父目錄，在我指定的位置建立 my-app，並初始化 Git。等建立與登記完成後，再以回傳的專案 ID 建立任務並開始開發。

操作既有專案：

> 請列出 WebCodex 專案，選擇我指定的專案建立任務。先讀取專案規則與 Git 狀態，執行相關測試；需要修改時保留其他變更，最後顯示 diff 與真實測試結果。把任務 ID、摘要與下一步保存，讓下一次對話可以延續。

下次對話先使用 `task_list` 和 `task_get` 讀取同一個任務，從 Server 保存的工作摘要與證據接續。

恢復時以 `task_get.progress` 的待處理工作與最新執行結果判斷下一步。`summary_stale: true` 表示摘要後又有工作更新，應讀取最新工作狀態。`status: succeeded` 的工作若同時回傳 `outcome: failed`，代表命令失敗，應查看輸出處理錯誤。依 `next_action`／`next_actions` 及其 `condition` 執行下一步；只剩等待時交接至卡片，仍有獨立工作時可用 `observation_action`／`observation_actions` 更新原工作的觀察結果。欄位與範例見 [授權與執行](EXECUTION.md#恢復任務時先讀取目前進度)。

## 等待中的進度回報

用戶端在工具呼叫帶入 `progressToken` 時，Server 會在該次呼叫尚未回覆期間，每約 5 秒送出一次進度通知，例如「正在確認工作狀態，已等候 5 秒」。數值表示本次等待的秒數，呼叫結束後停止通知。[MCP 進度通知規範](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress)

`/mcp` 使用 Streamable HTTP 的 SSE 回覆傳遞通知及最終結果；stdio bridge 會依原請求的進度識別轉送通知，因此經 bridge 的連線也能收到。一般 REST 呼叫仍回傳 JSON。通知不包含命令參數、憑證或原始輸出，原工作與程序仍依既有 ID 查詢。[MCP 傳輸規範](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

這項機制需要用戶端要求並支援呈現進度通知。Harness 的初始化指引也要求用戶端在長操作開始前、狀態改變時，或等待約 30 秒後提供簡短可見回覆。跨回合續接由下方的任務卡片透過用戶端支援的訊息介面請求。若畫面沒有更新，先查 `task_get` 的實際工作與程序狀態。

## 任務卡片與跨回合續接

`work_on_project(project, title, request_id)` 可一次建立任務並讀取初始工作目錄。接續既有工作時傳入 `task_id`，不另開任務；已完成任務也不會被隱式重新開啟。回傳的 `next_action` 會指定 `task_present(task_id)`，供目前回合顯示任務卡片；工作目錄仍在讀取時，再依 `workspace.next_action` 等待。

任務卡片的「檢查成果」可切換「修改內容」與「執行結果」。點選檔案會讀取差異，包含新增但尚未追蹤的檔案；沒有 Git 的專案則顯示目前檔案內容。命令結果來自該任務實際程序紀錄，顯示成功、失敗、結束碼與輸出。清單、差異與輸出都有長度限制，較長內容可在對話中指定檔案或繼續讀取程序紀錄。

卡片以計畫中的完成步驟顯示進度，兩個頁籤分別標示檔案數及最近命令數。首次讀取會自動開啟第一個檔案；重新整理會保留檔案選擇與命令展開狀態。頁尾「進度更新」表示任務狀態的讀取時間，「成果更新」表示檔案與命令結果的讀取時間。按「重新整理」更新成果；讀取失敗時保留上次結果並標示狀態。「重新連線」可重試卡片初始化或狀態連線。

卡片連線與自動續接分開顯示。只有成功建立 `task_wait` 後，卡片才顯示「已安排自動續接」；尚未交接時顯示「尚未安排自動續接」，不顯示暫停按鈕。接續訊息若被用戶端拒絕，會保留未確認狀態，不標示已送出，也不自動重送。

也可直接用 `task_review(task_id)` 檢查，指定 `path` 讀取個別檔案。尚未完成時，以相同 `job_id` 與 `path` 等待原 review 工作。完成後仍可讀取檔案與檢視結果；要繼續修改，必須先明確用 `task_update` 重新開啟任務。畫面顯示整個專案目前尚未提交的變更，可能包含其他工作；驗證結果需對照命令執行時的修改內容。

`task_present(task_id)` 顯示一張持續更新的任務卡片。卡片是 `ui://webcodex/task-progress/v1/<內容雜湊>.html` 的 MCP App，使用現有用戶端連線讀取進度；不含 Server token，也不直接存取專案。Server 啟動時從 HTML 內容產生資源版本，工具 metadata 與資源清單使用同一個位址。舊版 `v1` 位址與同格式的版本位址仍可讀取相容的目前卡片。stdio bridge 同時轉送資源內容與工具呼叫中的視窗識別。[MCP Apps 規範](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)

建議流程如下：

1. 每個由使用者發起、開始或恢復較長工作的回合，都呼叫一次 `task_present` 顯示目前任務卡片；先前回合已顯示過同一任務，仍依此流程呈現。同回合的後續工具呼叫，以及由 `task_resume` 自動續接的回合，沿用已有卡片。
2. 確認只剩既有工作需要等待時，依主要 `next_action`／`next_actions` 交接。可交接的等待回覆會把 `task_wait`、精確 job ID 與穩定 `request_id` 放在這一層，並標示 `condition=only_when_no_independent_work`；`continuation.handoff` 保留完整交接狀態。成功登記後再說明目前工作並結束回合。仍有獨立工作時先繼續執行，需要查詢原工作時使用 `observation_action`／`observation_actions`。尚未讀完的輸出會優先保留原 cursor 的讀取動作；已完成的結果也保留供模型檢查。
   卡片未綁定或連線租約到期時，等待回覆會提供 `task_present` 作為修復動作；確認卡片綁定後再讀取原 job 並登記等待。暫停、權限不足、缺少對話識別、其他視窗控制中、等待項目超過上限或通知結果未確認時，維持相應限制。尚未成功 `task_wait` 時不宣稱會自動續接。
3. 卡片在前景約每 3 秒、背景約每 15 秒確認進度。仍在執行的程序只透過 `read_process` 觀察，不會重跑命令。成功、失敗或結果不確定都會成為需要模型檢查的結果。
4. 等待結束且卡片在前景時，先保存通知狀態，再呼叫一次用戶端的 `ui/message`。模型收到後必須先以原參數呼叫 `task_resume`；僅第一次成功消費通知時接續工作，重複消費不得另啟流程。

模型以 `task_resume` 成功消費通知後接續工作。回覆遺失、卡片更換或送出結果不明時，卡片顯示「尚未確認是否接續」並停止自動重送。使用者明確要求繼續後，模型先查 `task_get` 與原工作；若仍需新的等待，可在 `task_wait` 指定確切的 `replace_wait_id`。這會使舊通知失效，不會重跑原命令。已完成任務會取消待送通知。

同一任務只有一個有效卡片控制者，租期為 120 秒。同一視窗重新開啟卡片會取代舊卡片；其他視窗在租期內只顯示進度。若控制連線已到期，同一對話中仍存活的舊卡片可以重新綁定，並取得新的控制世代；有效控制者與跨對話的舊卡片不受此恢復規則取代。背景卡片會更新與續租，但不自動送出訊息。關閉後的等待保存在 SQLite，重新開啟才恢復觀察。暫停按鈕只暫停自動續接，已啟動的工作仍會執行。

目前視窗識別使用用戶端提供的 `openai/session`，缺少此資訊時不啟用自動續接。用戶端還必須支援 MCP App、卡片工具呼叫及 `ui/message`，並允許訊息產生後續模型回合。更新 Server 後需重新啟動長駐 bridge／tunnel，在 ChatGPT 連線設定按 Refresh／Reload，確認工具 metadata 已更新，再開新對話驗證卡片。新對話可用原 `task_id` 接續；先前訊息中已呈現的卡片不會因此原地換版。[OpenAI 更新流程](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)

OpenAI 將 UI 資源位址作為快取鍵，因此只重啟服務或在舊卡片按「重新整理」，仍可能看到舊 HTML。新版卡片資源會隨內容改變位址；卡片內的「重新整理」則更新檔案與命令結果。[UI 資源版本](https://developers.openai.com/plugins/build/chatgpt-ui)

後續對話需要卡片時，對同一個 `task_id` 再呼叫 `task_present`。若 ChatGPT 回覆區顯示連線中斷，先用 `task_get` 及 `read_process` 確認原工作，再依紀錄接續。WebCodex 沒有一小時的 session 有效期；Runner 的 `max_process_seconds` 預設為 3600 秒，限制的是單一命令的執行時間。

## 「思考失敗」與診斷日誌

ChatGPT 顯示「思考失敗」時，已送到 Runner 的命令與子任務仍可繼續執行。先以原 `task_id` 查詢 `task_get`、`job_get` 或 `read_process`，依實際結果接續工作。

自動續接適用於已成功呼叫 `task_wait` 的等待。有效卡片在前景、等待工作已有結果時，會送出一次 `ui/message`，由模型以 `task_resume` 接回原任務。若模型在建立等待前中斷，或已接回後再次失敗，WebCodex 尚無可用通知能判定該回合失敗。官方公開的 UI 介面提供 follow-up 訊息，未提供重試既有失敗回合的操作。[OpenAI UI 介面](https://developers.openai.com/plugins/build/chatgpt-ui)

通知狀態為 `accepted` 或 `unknown` 時，保留原通知與防重送紀錄。單憑一段時間沒有工具請求，無法判定模型是否已中斷；系統不會據此重送命令或重建等待。

### 判讀卡片上的活動狀態

卡片將連線與活動分開顯示：

| 欄位 | 資料來源 |
| --- | --- |
| 卡片已連線 | 最近一次卡片工具請求成功回覆 |
| ChatGPT 工具 | 同一任務、同一對話識別最近一筆 MCP 工具請求的開始、回覆或錯誤時間 |
| 工作電腦 | Runner 最近連線時間，以及已保存的工作數量與觀察時間 |
| 自動續接 | 已登記等待、結果就緒或通知處理狀態 |

卡片輪詢、成果檢視與操作員的 REST 查詢不會刷新 ChatGPT 工具活動。工作數量會附上原觀察時間；卡片重新整理時間另外顯示在頁尾。

未安排等待且超過三分鐘沒有對話工具活動時，卡片會顯示最近回應時間，提示使用者在原對話確認「思考失敗」或計時停止的情況。已安排的等待與待回覆事項保留各自狀態。按「檢查狀態」會重新查詢原任務；原對話的失敗回合使用 ChatGPT 的「重試」接續。

卡片連續 45 秒未取得新狀態時，會標示目前為上次紀錄。重新連線成功後，才恢復最新連線狀態。

Server 會把新增的結構化診斷紀錄寫入 `<state_dir>/diagnostics/server.jsonl`。每份上限 5 MiB，保留目前檔案及三份備份，總容量約 20 MiB。bridge 的相同格式紀錄寫入 stderr；Linux systemd 部署可從 tunnel 服務的 journal 查閱。

| 紀錄 | 用途 |
| --- | --- |
| HTTP 請求起訖、狀態碼、耗時、取消與斷線 | 確認請求是否到站、何時回覆或中斷 |
| 工具名、task／job 關聯、結果狀態 | 區分工具成功回傳、命令仍在執行及命令失敗 |
| bridge 與 Server 共用的追蹤編號 | 對照工具請求經過的兩層紀錄 |
| RPC 識別與視窗識別的雜湊 | 關聯同一請求或視窗，避免保存原始值 |
| 續接的準備、送出結果、消費狀態及錯誤碼 | 區分通知逾時、RPC 錯誤、宿主拒絕及已接回的工作 |
| 卡片是否取得控制、控制世代及交接阻擋原因 | 直接辨識卡片被取代、連線到期或尚未綁定 |

新增診斷採欄位白名單，不寫入原始工具參數、指令、檔案內容、輸出、憑證或續接消費 token。例外只記錄類別；寫入日誌失敗不影響原工作。健康檢查與 Runner 正常輪詢略過成功紀錄，保留錯誤與斷線紀錄。

追蹤編號由 WebCodex 產生；ChatGPT 模型回合的內部錯誤碼仍需由平台提供。排查時可搭配 ChatGPT 顯示的時間、工具操作紀錄與截圖，對照 Server／bridge 紀錄。[OpenAI 排查指南](https://developers.openai.com/plugins/deploy/troubleshooting#when-to-escalate)

bridge 對每筆 MCP 回覆設定 60 秒等待上限。回覆串流提早結束，或持續收到進度卻缺少最終結果時，該次呼叫會回報逾時；同一 bridge 的其他呼叫仍可使用。已送出的工作保留在原 job，可用 `job_get`／`read_process` 查詢，bridge 不會自動重送命令。

ChatGPT 出現「訊息傳送逾時」或 `/backend-api/ecosystem/call_mcp` 的 HTTP 400 時，分別保存瀏覽器請求的時間、狀態碼與回應內容，再對照 Server／bridge 的完成紀錄。HTTP 狀態碼與 MCP 回覆中的 `isError` 屬於不同層；回應內含正常任務資料時，仍需保留瀏覽器顯示的 HTTP 錯誤供追查。

這些紀錄由長駐程序載入，更新後需重啟 Server 與 bridge 才會啟用；Runner 可繼續運行。新版卡片仍依前述連線 metadata 更新流程載入。

## 升級後接續舊版等待工作

新版已移除逐筆核准與專案信任設定。Server 啟動時會重新檢查舊版 `pending_approval` 工作的用戶端、專案與工具權限；仍有權限且任務尚未結束的工作會沿用原 job 排入佇列，其他工作會取消。

先用 `task_get` 確認原工作，再依回傳的 `next_actions` 接續。`projects_list` 的相容欄位 `approval_mode` 固定為 `automatic`，無須另外設定。完整流程見 [授權與執行](EXECUTION.md)。

## 工具看得到，但無法執行

工具註冊、目前對話的執行權限、Runner 狀態需分別檢查。schema 顯示工具 metadata；呼叫 `projects_list` 可確認對話能否執行工具，並從 Server 讀取最近的 Runner 心跳。這項查詢不建立 job，也不直接操作 repo；`online` 表示 15 秒內有心跳。

| 觀察到的證據 | 可判定的狀態 | 下一步 |
| --- | --- | --- |
| 只有工具名稱／schema | 工具已註冊；執行與 Runner 狀態未確認 | 呼叫一次 `projects_list` |
| `FORBIDDEN: This conversation does not support developer MCPs` | 依錯誤訊息判讀為用戶端拒絕此對話執行；本次無法取得 Runner 狀態 | 停止重試相同呼叫，檢查下方對話與權限設定 |
| `projects_list` 成功且 `online: true` | 已取得 Server 回覆，該 Runner 心跳有效 | 使用回傳的 project ID 建立／延續任務 |
| `projects_list` 成功且 `online: false` | 已取得 Server 回覆，該 Runner 心跳過期 | 檢查 Runner 及其至 Server 的連線 |
| `projects_list` 成功但清單為空 | 此 client 看不到已註冊專案 | 新專案改查 `project_roots_list`；既有專案比對授權與登記設定，不能直接標成離線 |
| Server 回覆 `401 Unauthorized` | Bearer 驗證失敗，Runner 狀態未確認 | 檢查所選 client 設定與憑證種類 |
| Server 回覆 `403 Origin is not allowed` | Server 拒絕請求的 Origin，Runner 狀態未確認 | 檢查實際入口與 `public_url`；這與 developer MCP 對話限制不同 |
| 逾時、DNS／TLS 或 transport 錯誤 | 連線未完成，Runner 狀態未確認 | 檢查發生錯誤的網路或 tunnel 路徑 |

若用戶端在請求送出前拒絕執行，需檢查目前對話與工作區權限。Server 回覆的 HTTP 403，則依實際錯誤內容排查。

### Developer MCP 對話限制的排查

1. 在相同 Server 與 client 設定下執行 `uv run webcodex call projects_list`；使用自訂設定時加上 `--config /absolute/path/client.json`。這能驗證 CLI → Server 的工具呼叫；ChatGPT → MCP／tunnel 需在對話中呼叫驗證。`webcodex doctor` 檢查本機環境。
2. 依官方介面確認 Settings → Security and login → Developer mode；其可用性受帳號與工作區政策影響。更新連線 metadata 後使用 Refresh，再開啟新對話並從工具選單加入該連線，重新測試 `projects_list`。[官方連線與更新步驟](https://developers.openai.com/plugins/deploy/connect-chatgpt)
3. 若仍被拒絕，請工作區管理者確認 plugin／MCP server 對該角色可用，以及連線的 action 權限。工具可被看到與具備執行權限是不同層次。[官方權限說明](https://learn.chatgpt.com/docs/enterprise/apps-and-connectors)
4. 保留用戶端介面／模式、時間與完整錯誤文字，供工作區管理者或用戶端支援管道排查。`projects_list` 不建立 job；請透過反向代理或 tunnel 日誌確認請求是否到達。

### 驗收案例

保留「schema 可取得，但呼叫被 host 拒絕」作為用戶端驗收情境。預期回報為「工具已註冊；目前對話不允許執行；Runner 狀態未確認」，並停止無效重試。

本機自動測試透過真實 stdio bridge 驗證初始化指引、工具說明，以及相同工具清單下的 Runner 上線、心跳過期與無可見專案結果。ChatGPT 的 host 拒絕需要在出錯的對話環境重測。
