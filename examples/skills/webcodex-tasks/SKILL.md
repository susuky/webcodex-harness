---
name: webcodex-tasks
description: 使用 WebCodex 開始工作、續接原任務、跨對話接手或判讀工作卡狀態時使用。說明 task_id 的持久性、接手方式與此對話工具活動的範圍。
---

# WebCodex 任務與跨對話續接

`task_id` 指向 Server 持久保存的任務，包含目標、工作紀錄、命令與交接資訊。它獨立於 ChatGPT 對話、MCP 連線與 repo；同一 repo 可以有多個任務。不同任務的紀錄分開，同一 repo 的檔案仍共用。

## 開始或續接

- 獨立的新工作：用 `projects_list` 選擇確切專案，再以 `work_on_project(project, title, request_id)` 建立新任務並保存回傳的 `task_id`。不要只因 repo 相同就沿用其他對話的任務。
- 使用者要求延續原工作：已知 ID 就用 `task_get(task_id)`；未知時以 `task_list` 找到對應目標，再讀 `task_get`。同一客戶身分（owner）及現行專案權限內才能續接；跨帳號需另行授權。
- 使用者要求換到目前對話接手：呼叫 `task_takeover(task_id)`，讀取回傳的 `handoff` 與即時 `progress`，依 `next_action` 顯示工作卡。同一任務的卡片控制權只交給一個視窗；接手會撤回舊卡片的發訊權，原有命令繼續執行，自動訊息保持關閉。

續接時沿用原 `task_id`、`job_id` 與 `process_id`。queued/running job 用 `job_get`；已有程序用 `read_process`／`write_stdin`。從實際輸出與交接的下一步繼續，`progress` 優先於可能過時的 summary／plan。不要另開替代任務或重跑仍在執行的命令；遺失回應時查原 job，重試同一請求沿用 `request_id`。

例如對話 A 使用 `T1`，對話 B 建立 `T2`，兩者的任務紀錄分開；B 只有明確沿用 `T1` 才會看到 A 的工作並續接。

## 判讀工作卡

「此對話最後工具呼叫」是目前對話對此任務發出 WebCodex 工具呼叫的開始時間（`started_at`）。另一對話對同一任務的呼叫、任務更新、背景工作完成、卡片輪詢與 Runner 心跳都不更新它。工具可以立即完成，因此有最近呼叫時間，同時沒有執行中的背景工作。

Runner 連線狀態反映執行服務是否在線；工具活動時間反映最近一次已觀察到的呼叫。模型目前是否思考、回覆或卡住沒有直接訊號。久未活動時回報已觀察到的狀態，由使用者手動決定是否重啟；不要因此自動重啟對話、啟動替代 agent 或自動發送喚醒訊息。
