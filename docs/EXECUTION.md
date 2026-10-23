# 授權與執行

WebCodex 在驗證用戶端身分、專案範圍與工具權限後直接執行，不設逐筆核准佇列，也不需要為專案設定信任模式。

## 設定

每個用戶端以 `projects`、`project_roots` 與 `permissions` 指定可操作的範圍。Runner 在派工時再次檢查自己的工具權限及專案綁定。沒有權限的請求會被拒絕；已授權的工作直接進入 `queued`，由 Runner 執行。

`init` 與 `allow-root` 不需要 `--trusted`。桌面工具仍由 `--computer-use` 或 `computer_observe`／`computer_control` 權限啟用。

## 舊版升級

更新並重新啟動 Server 後：

- 舊設定中的 `approval_required`、`trusted_projects` 與 `trusted_project_roots` 會被忽略，新產生的設定不再包含這些欄位。
- 舊命令列的 `--trusted` 保留相容解析，沒有額外作用。
- 尚未執行的舊 `pending_approval` 工作，通過現行授權檢查後沿用原 job ID 排隊，不重送命令。
- 已完成任務或權限已撤銷的舊工作改為取消。
- `approve`、`deny`、`approvals` 命令與核准 API 已移除。

舊日誌中的 `approved` 欄位保留歷史紀錄，不影響派工。為相容既有用戶端，專案與進度回覆中的 `approval_mode` 固定為 `automatic`，`approval_required` 固定為空清單。

## 恢復任務時先讀取目前進度

使用 `task_get` 的 `progress` 判斷待處理工作與最新程序結果。`summary`、`status` 與 `plan` 是先前保存的交接紀錄，可能早於目前執行狀態。

- 依 `next_action`／`next_actions` 觀察同一 job 或 process，不重新啟動命令。
- 程序尚未結束時，使用 `read_process` 與回傳的 byte cursor 讀取新增輸出。
- `status: succeeded` 表示工具成功回傳；命令結果依 `outcome`、`exit_code` 與輸出判斷。
- 只剩等待且卡片已連線時，成功呼叫 `task_wait` 後才交接；其他工作仍可獨立進行時，繼續處理並使用觀察動作刷新原程序。
- Runner 離線時保留原 job，恢復後接續。取消未開始的工作使用 `job_cancel`；停止已開始的程序使用 `cancel_process`。
