# 子代理分工

ChatGPT 的原生子代理由 ChatGPT 協調，WebCodex 提供專案檔案、命令、Git、技能與桌面操作工具。Runner 的角色是執行工具及回傳結果。

## 使用方式

在支援原生子代理的 ChatGPT 對話中，直接要求把獨立工作分給子代理，由主對話收集結果與整合。是否可用及思考程度設定依 ChatGPT 的帳號與模型而定；官方說明見 [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)。

操作專案時仍使用相同的 WebCodex 專案與任務識別。並行寫入需分配不同檔案，命令的執行狀態由 `job_get`、`read_process` 觀察，避免重複執行。

## 從舊版升級

原有的 Codex CLI worker 後端與五個 `subagent_*` 工具已移除。更新 Server 與 Runner 後，舊工具名稱會立即回報不存在，舊設定中的 `subagents_enabled` 與 `max_subagents` 會被忽略，無法恢復 CLI 派工。

升級前先停止舊 Runner，結束其程序，再啟動新版。既有工作紀錄與輸出保留，可由 `job_get`、任務檢查與程序工具讀取。WebCodex 不需要 Codex CLI 安裝或登入。
