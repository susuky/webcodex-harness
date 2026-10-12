# 核准與受信任專案

個人專案可以設定日常開發免逐筆核准，讓 MCP 用戶端直接執行已獲允許的工具。需要審核的專案則保留核准佇列。這是每個用戶端、每個專案的設定，不會改變其他使用者的核准政策。

## 日常開發不必切回終端機

操作員在 `server.json` 的指定 `clients.CLIENT_ID` 中加入確切的專案 ID：

```json
"trusted_projects": ["local/my-app"]
```

保留原有 `projects`、`project_roots`、`permissions` 與 `approval_required`，備份設定後重新啟動 Server。只更改 Server 核准政策不需要重啟 Runner；既有命令會繼續由 Runner 執行。

設定生效後：

- 受信任專案中的命令、Git 操作及擴充工具，不再逐筆等待本機核准；讀寫工具亦沿用這個模式。
- 用戶端與 Runner 原有的工具權限、專案授權、路徑檢查及工作去重仍會檢查。信任清單本身不授予專案存取或新增工具。
- 桌面操作與建立／登記新專案仍使用原有核准政策。專案信任不延伸到父目錄或其他專案；不接受 `*` 等萬用字元。
- 尚在 `pending_approval`、任務未完成且現行政策已允許的工作，會用**原 job ID** 接續排隊，不重新送出命令。已撤銷工具或專案存取的工作不會因此放行。

移除信任設定並重啟 Server 後，尚未交付的工作會再次檢查核准要求。已交付或執行中的工作不會被回溯取消。

信任模式表示允許相關命令與擴充以 Runner 帳號權限執行；shell 本身沒有 OS sandbox，不能把專案信任誤認為作業系統限制。

## 整個工作目錄的日常開發

若經常新增專案，使用 `webcodex allow-root /absolute/path/to/projects --trusted` 一次設定，再重新啟動 Server 與 Runner，詳見 [建立專案](PROJECTS.md)。這會同時授予必要的開發權限，並在指定用戶端加入精確的 `trusted_project_roots`。父目錄必須也在該用戶端的 `project_roots` 授權內；單獨加入信任清單不會開放目錄。

受信任父目錄內的建立／登記與已登記專案的日常開發免逐筆核准。設定檔明確列出的既有專案也適用；未登記的資料夾不會自動變成專案。不同父目錄、未授權專案與桌面操作不受影響。移除父目錄授權後，尚未交付的工作會重新檢查並拒絕越權執行。

## 先查看目前模式

`projects_list` 會回傳每個專案的 `approval_mode` 與 `approval_required`：

| 回傳值 | 意義 |
| --- | --- |
| `automatic` | 這個用戶端在此專案的已授權工具不需另外等待本機核准 |
| `review` | 只有 `approval_required` 列出的工具權限類別需要核准 |

不要只憑 `approved: 0` 判定工作被阻擋；它記錄是否有逐筆人工核准。免核准工作的值仍可為 `0`，實際是否完成應依 job 狀態、結果與程序結束狀態判斷。

## 恢復任務時先讀取目前進度

`task_list` 的 `status`、`summary` 與 `plan` 是用戶端儲存的交接紀錄；核准完成或程序執行後，舊摘要不會自動改寫。依回覆的 `next_action` 呼叫 `task_get`，以 `progress` 檢查目前情況：

- `approval_mode`、`approval_required`：這個用戶端在該專案的現行核准政策。
- `summary_stale`：儲存摘要後是否又有工作更新；舊的「等待核准」不能當成目前仍受阻的證據。
- `pending_jobs`、`job_counts`：未結束的工作及各狀態總數；即使待核准工作已超出最近 30 筆紀錄，仍會列出。清單最多 100 筆，超過時標記 `pending_jobs_truncated`。
- `process_observations`、`latest_execution`：最近保存的程序狀態與最新一筆執行結果。它們是觀測紀錄；程序仍顯示執行中時，依 `next_actions` 呼叫 `read_process` 更新。
- `state`、`next_step`：區分等待核准、等待 Runner、繼續觀測、需要處理執行問題及可接續工作。已完成或專案位置已改變的任務也會明確標示。

新工作回覆中的 `outcome` 直接顯示已知的程序結果。例如：

```json
{
  "status": "succeeded",
  "outcome": "failed",
  "approved": 0,
  "result": {"process_id": "PROCESS_ID", "status": "failed", "exit_code": 2}
}
```

這表示工具成功回傳結果，但命令失敗。MCP 的 `isError` 仍描述工具呼叫是否成功；測試是否通過必須查看 `outcome`、`exit_code` 與輸出。

工作或程序還在執行、或仍有輸出未讀完時，回覆會附上可直接呼叫的 `next_action`，包含原工作／程序 ID、輸出游標及等待時間。用戶端應在同一回合接續查詢，不要求使用者反覆回覆「繼續」，也不重新執行命令。沒有新心跳、待核准或結果不明時，應說明實際原因並停止無效重試。

一般偏好與需求澄清直接在目前對話詢問。`request_user_input` 是留給本機操作員透過 CLI 回覆的另一條管道，不會顯示 ChatGPT 的提問介面，亦不授予執行權限。

## 保留逐筆核准時

需要核准的 job 會附上：

```json
{
  "status": "pending_approval",
  "job_id": "JOB_ID",
  "service": {
    "id": "SERVICE_ID",
    "product": "webcodex-harness",
    "version": "VERSION"
  },
  "approval": {
    "approve_command": "webcodex approve JOB_ID --service-id SERVICE_ID",
    "deny_command": "webcodex deny JOB_ID --service-id SERVICE_ID"
  }
}
```

操作員檢查該工作的工具及參數後，執行回傳的其中一個指令。在原始碼環境可加上 `uv run` 前綴；自訂設定則加 `--config /absolute/path/server.json`。

`--service-id` 會由接收決定的 Server 驗證；若設定指向另一個服務，決定會被拒絕，不會套用到該服務。服務識別保存在 Server SQLite，重啟後保持一致，不包含憑證、路徑或登入環境。`webcodex approvals` 與 `/healthz` 也會回傳相同識別。

本專案的核准目標就是 **job ID**，沒有另一個 approval ID，也不使用參考倉庫的 `task approve` 子命令。先前不含 `--service-id` 的指令仍可使用；建議採用新回覆附上的完整指令。

核准完成後，MCP 用戶端繼續用 `job_get` 查詢同一工作。若狀態仍是 `pending_approval`，應顯示回傳的核准資訊並等待操作員，不反覆重送命令、修復連線或猜其他版本的 CLI 語法。

信任設定只由操作員控制，不提供讓 MCP 用戶端自行核准或更改信任的工具。逐筆人工核准目前使用本機 CLI；尚未提供手機核准頁面。
