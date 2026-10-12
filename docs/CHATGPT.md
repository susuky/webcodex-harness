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

WSL2 範例要求 Server、Runner 與 client 設定都在相同 Ubuntu 發行版內。這些是供支援 stdio 的本機 MCP 用戶端使用的設定，不能直接貼進 ChatGPT 的遠端 URL 欄位。Windows 原生及 WSL2 內的 stdio bridge 均已通過實機測試；Windows 用戶端透過 `wsl.exe` 啟動的組合，以及實際 ChatGPT 帳號連線，仍需在選定的用戶端配置後驗證。

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

4. 在 ChatGPT 啟用 developer mode，新增 MCP 連線，選擇 Tunnel 與對應 tunnel。保留本機 Server、Runner 和 tunnel-client 運作。
5. 確認探索到的工具清單後，開啟對話並加入這個連線；先試 `projects_list`，再建立任務。介面可用性由帳號及工作區政策決定。[ChatGPT 官方連線步驟](https://developers.openai.com/plugins/deploy/connect-chatgpt)

Server 的 Bearer 驗證仍由 bridge 處理，不必為了接入而關閉本機驗證。本專案沒有替你建立 tunnel、使用帳號 key 或宣稱已完成 ChatGPT 端驗收。

這條路徑不需要另外配置 Cloudflare 或 SSH：本機 `tunnel-client` 主動建立對外連線，讓 ChatGPT 的工具請求經 tunnel → stdio bridge → Server → Runner 到達專案工具。ChatGPT 網頁沒有直接掛載你的資料夾；檔案操作由本機 Runner 執行，範圍由專案與父目錄設定決定。[Secure MCP Tunnel 運作方式](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

## 公開 HTTPS 入口

若你已有公開網域與反向代理，可將 HTTPS `/mcp` 轉發至 Server，並將 `server.json` 的 `public_url`、`allowed_hosts` 設為實際入口。其他機器的 Runner 必須使用 HTTPS；憑證驗證保持開啟。

本專案目前實作靜態 Bearer 驗證，**沒有 OAuth authorization server**。對支援 Bearer 的 MCP 用戶端可直接使用；對要求 OAuth 的 ChatGPT 連線或公開 plugin，需加上合規的 OAuth 入口，不能假設填入 Bearer token 就能使用所有 ChatGPT 表面。OpenAI 的公開 plugin 驗證契約包含 OAuth 2.1、資源探索與 PKCE。[官方驗證文件](https://developers.openai.com/plugins/build/auth)

如果只需要自己的 ChatGPT 操作本機 repo，前述 stdio bridge／Secure MCP Tunnel 是已準備好的私有接入路徑。

## 建議的第一個請求

建立全新專案：

> 請列出 WebCodex 允許建立專案的父目錄，在我指定的位置建立 my-app，並初始化 Git。等建立與登記完成後，再以回傳的專案 ID 建立任務並開始開發。若需要核准，保留同一工作等待結果。

操作既有專案：

> 請列出 WebCodex 專案，選擇我指定的專案建立任務。先讀取專案規則與 Git 狀態，執行相關測試；需要修改時保留其他變更，最後顯示 diff 與真實測試結果。把任務 ID、摘要與下一步保存，讓下一次對話可以延續。

下次對話先使用 `task_list` 和 `task_get` 讀取同一個任務。Server 能保存工作摘要與證據，不會存取或還原 ChatGPT 的私有推理與完整對話上下文。

恢復時以 `task_get.progress` 的待處理工作、現行核准政策及最新執行結果判斷下一步。`summary_stale: true` 表示摘要後又有工作更新；即使摘要仍寫「等待核准」，也不能據此要求使用者回到終端機。`status: succeeded` 的工作若同時回傳 `outcome: failed`，代表命令失敗，應查看輸出處理錯誤。仍在執行時，依 `next_action`／`next_actions` 接續查詢，同一回合不要求使用者反覆回覆「繼續」。欄位與範例見 [任務進度與核准指南](APPROVALS.md#恢復任務時先讀取目前進度)。

## 等待中的進度回報

用戶端在工具呼叫帶入 `progressToken` 時，Server 會在該次呼叫尚未回覆期間，每約 5 秒送出一次進度通知，例如「正在確認工作狀態，已等候 5 秒」。數值表示本次等待的秒數，不代表整項開發工作的完成百分比。呼叫結束後停止通知；不會對已結束的請求持續推播。[MCP 進度通知規範](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress)

`/mcp` 使用 Streamable HTTP 的 SSE 回覆傳遞通知及最終結果；stdio bridge 會依原請求的進度識別轉送通知，因此經 bridge 的連線也能收到。一般 REST 呼叫仍回傳 JSON。通知不包含命令參數、憑證或原始輸出，原工作與程序仍依既有 ID 查詢。[MCP 傳輸規範](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

這項機制需要用戶端要求並呈現進度通知，不能保證 ChatGPT 的每個介面都會顯示。Harness 的初始化指引也要求用戶端在長操作開始前、狀態改變時，或等待約 30 秒後提供簡短可見回覆。進度通知本身無法喚醒已停止的回合；下方的任務卡片另透過用戶端支援的訊息介面請求續接。若畫面安靜，先查 `task_get` 的實際工作與程序狀態，不以「沒有聊天文字」推斷命令未執行。

## 任務卡片與跨回合續接

`work_on_project(project, title, request_id)` 可一次建立任務並讀取初始工作目錄。接續既有工作時傳入 `task_id`，不另開任務；已完成任務也不會被隱式重新開啟。

任務卡片的「檢查成果」可切換「修改內容」與「執行結果」。點選檔案會讀取差異，包含新增但尚未追蹤的檔案；沒有 Git 的專案則顯示目前檔案內容。命令結果來自該任務實際程序紀錄，顯示成功、失敗、結束碼與輸出。清單、差異與輸出都有長度限制，較長內容可在對話中指定檔案或繼續讀取程序紀錄。

也可直接用 `task_review(task_id)` 檢查，指定 `path` 讀取個別檔案。尚未完成時，以相同 `job_id` 與 `path` 等待原 review 工作。完成後仍可讀取檔案與檢視結果；要繼續修改，必須先明確用 `task_update` 重新開啟任務。畫面顯示整個專案目前尚未提交的變更，可能包含其他工作；舊命令成功也不代表後續修改已通過驗證。

`task_present(task_id)` 顯示一張持續更新的任務卡片。卡片是 `ui://webcodex/task-progress/v1` 的 MCP App，使用現有用戶端連線讀取進度；不含 Server token，也不直接存取專案。stdio bridge 同時轉送資源內容與工具呼叫中的視窗識別。[MCP Apps 規範](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)

建議流程如下：

1. 開始或恢復較長的任務時呼叫一次 `task_present`，後續照常執行已有授權的工具。
2. 確認只剩既有工作需要等待時，用 `task_wait` 保存 1–8 個 job ID 與固定 `request_id`，向使用者說明目前工作後結束回合。若卡片未連線、暫停或無法識別原視窗，工具會拒絕交接；此時繼續在原回合查詢 `job_get`／`read_process`。
3. 卡片在前景約每 3 秒、背景約每 15 秒確認進度。仍在執行的程序只透過 `read_process` 觀察，不會重跑命令。成功、失敗或結果不確定都會成為需要模型檢查的結果。
4. 等待結束且卡片在前景時，先保存通知狀態，再呼叫一次用戶端的 `ui/message`。模型收到後必須先以原參數呼叫 `task_resume`；僅第一次成功消費通知時接續工作，重複消費不得另啟流程。

通知「已接受」不等於模型已開始工作。回覆遺失、卡片更換或送出結果不明時，卡片顯示「尚未確認是否接續」並停止自動重送。使用者明確要求繼續後，模型先查 `task_get` 與原工作；若仍需新的等待，可在 `task_wait` 指定確切的 `replace_wait_id`。這會使舊通知失效，不會重跑原命令。已完成任務會取消待送通知。

同一任務只有一個有效卡片控制者，租期為 120 秒。同一視窗重新開啟卡片會取代舊卡片；其他視窗在租期內只顯示進度。背景卡片會更新與續租，但不自動送出訊息。關閉後的等待保存在 SQLite，重新開啟才恢復觀察。暫停按鈕只暫停自動續接，已啟動的工作仍會執行。

目前視窗識別使用用戶端提供的 `openai/session`，缺少此資訊時不啟用自動續接。用戶端還必須支援 MCP App、卡片工具呼叫及 `ui/message`；實際能否產生後續模型回合由用戶端決定。更新 Server 後需重新啟動長駐 bridge／tunnel，並在用戶端重新整理工具 metadata。本機整合測試及瀏覽器中的模擬對話測試，不代表真實 ChatGPT 對話已完成驗收。

## 專案在線，但每個命令都等待核准

`pending_approval` 表示 Server 正在等待操作員決定；與連線、Runner 是否在線及工具探索是不同狀態。此版本會在 job 回覆提供 `service` 識別與確切的核准／拒絕指令，不需要查參考倉庫的 CLI 或猜測 approval ID。

個人專案可由操作員設定 `trusted_projects`，讓已授權的日常開發工具直接執行；用 `projects_list` 查看實際 `approval_mode`。切換後符合新政策的待核准工作會沿用原 job，不需重送。完整流程與信任範圍見 [核准指南](APPROVALS.md)。

## 工具看得到，但無法執行

工具註冊、目前對話的執行權限、Runner 狀態是三個不同的檢查。看得到 schema 只代表工具 metadata 可取得，不能據此判定當前對話能呼叫工具。`projects_list` 在 Server 讀取最近的 Runner 心跳，不會建立 job，也不會直接執行 repo 操作；`online` 表示 15 秒內有心跳，不保證後續每個工具都成功。

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

本專案不產生上述 developer MCP 錯誤字串。若用戶端在請求送出前拒絕執行，Server 無法攔截、改寫回覆或自行解除限制；工具說明只能協助用戶端正確解讀。不要將所有 `FORBIDDEN`／HTTP 403 都歸類為此案例。

### Developer MCP 對話限制的排查

1. 在相同 Server 與 client 設定下執行 `uv run webcodex call projects_list`；使用自訂設定時加上 `--config /absolute/path/client.json`。這能驗證 CLI → Server 的工具呼叫，不能代替 ChatGPT → MCP／tunnel 的驗證。`webcodex doctor` 僅檢查本機環境。
2. 依官方介面確認 Settings → Security and login → Developer mode；其可用性受帳號與工作區政策影響。更新連線 metadata 後使用 Refresh，再開啟新對話並從工具選單加入該連線，重新測試 `projects_list`。[官方連線與更新步驟](https://developers.openai.com/plugins/deploy/connect-chatgpt)
3. 若仍被拒絕，請工作區管理者確認 plugin／MCP server 對該角色可用，以及連線的 action 權限。工具可被看到與具備執行權限是不同層次。[官方權限說明](https://learn.chatgpt.com/docs/enterprise/apps-and-connectors)
4. 保留用戶端介面／模式、時間與完整錯誤文字，供工作區管理者或用戶端支援管道排查。若有反向代理或 tunnel 日誌，可比對請求是否到達；`projects_list` 不建立 job，因此「沒有新 job」不能當成請求未到 Server 的證據。

公開文件未列出這個錯誤字串對應的所有內部條件；不能只憑它指定某個模型、方案或裝置一定不支援。更換對話是排查步驟，不保證能解除帳號或工作區限制。

### 驗收案例

保留「schema 可取得，但呼叫被 host 拒絕」作為用戶端驗收情境。預期回報為「工具已註冊；目前對話不允許執行；Runner 狀態未確認」，停止無效重試，且不宣稱已建立任務、讀取檔案或執行測試。

本機自動測試透過真實 stdio bridge 驗證初始化指引、工具說明，以及相同工具清單下的 Runner 上線、心跳過期與無可見專案結果。ChatGPT 的 host 拒絕需要在出錯的對話環境重測，不能用本機模擬宣稱已修復。
