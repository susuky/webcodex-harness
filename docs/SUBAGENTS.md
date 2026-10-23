# 平行子代理

ChatGPT 可以透過 WebCodex 將工作分派給多個 Codex CLI worker，再取得各自的狀態與最後回答。每個 worker 有獨立的推理循環，主對話負責分工、檢查結果與整合修改。

## 啟用

Runner 所在帳號需要安裝 Codex CLI 並完成 `codex login`。在 Runner 設定加入：

```json
{
  "subagents_enabled": true,
  "max_subagents": 4
}
```

重新啟動 Runner 與 Server，讓執行端及 MCP 工具清單載入新版本。`workspace_info.subagents` 回報是否啟用與並行上限。啟動 worker 使用既有 `execute` 權限及核准政策；查詢使用 `read`，取消使用 `write`。

## 自然語言分工

可以在連接 WebCodex 的 ChatGPT 對話說：

> 開兩個子代理，一個檢查 API 錯誤處理，一個檢查測試缺口。先各自唯讀分析，收齊結果後由你整合。

主對話依序呼叫兩次 `subagent_spawn`，取得兩個 `agent_id`，然後呼叫 `subagent_wait` 或 `subagent_get`。Spawn 只等到程序啟動，因此第二個 worker 能在第一個仍執行時開始。

| 工具 | 用途 |
| --- | --- |
| `subagent_spawn` | 指定名稱、提示、工作目錄與逾時，啟動一個 worker |
| `subagent_get` | 讀取狀態、最後回答與錯誤，可等待最多 30 秒 |
| `subagent_list` | 找回目前任務的 worker 與結果 |
| `subagent_wait` | 等待任一指定 worker 結束，回傳所有指定 worker 的狀態 |
| `subagent_cancel` | 停止 worker 及其程序群組，保留輸出 |

`prompt` 必須包含子任務需要的背景、限制與交付格式。Worker 不會自動取得 ChatGPT 對話歷史。預設 `write=false` 使用唯讀沙箱；需要修改時指定 `write=true`，使用工作區寫入沙箱。多個可寫 worker 應各自負責不同檔案；它們共用專案目錄，修改會直接反映在該 repo 的 `git status`。

## 狀態與結果

`completed` 需要 Codex 成功完成一輪並留下最後回答，內容在 `final`。`failed`、`cancelled`、`timed_out` 與 `interrupted` 分別表示失敗、取消、執行逾時與程序中斷。`subagent_wait.timed_out` 只表示這次等待到期，仍為 `running` 的 worker 會繼續執行。

網路斷線後使用原 `request_id` 取得同一工作，再以 `agent_id` 查詢；不要重新啟動以取得輸出。程序與結果保存在 Runner 私有狀態目錄。正常停止 Runner 會取消執行中的 worker；非預期中斷後重啟，未完成的 worker 標示為 `interrupted`。已完成的結果仍可讀取，不會自動重跑。

列表及批次等待會限制回答長度；`final_truncated=true` 時，以 `subagent_get` 讀取完整回答。

每個 worker 都占用既有的 `max_processes` 程序額度，並另受整台 Runner 的 `max_subagents` 上限約束。逾時採請求值與 `max_process_seconds` 的較小值。主對話可以將尚未完成的 spawn／get 工作交給既有任務卡片的 `task_wait`，之後續接會重新觀察同一 worker。

## 執行後端

WebCodex 重用已安裝的 Codex CLI 與既有登入，透過非互動 `codex exec --json` 執行，無須新增模型 SDK 依賴。子代理能力與 ChatGPT 的思考程度設定分開；Pro 不會改變 Runner 的工具或並行上限。

委派提示，以及 worker 讀取並交給模型的程式內容、命令輸出，會透過 Codex CLI 傳送到 OpenAI。Server 的私有工作紀錄保存提示與結果；Runner 保存程序輸出與結果。登入憑證由 CLI 就地讀取，不會複製進專案或加入工具參數。

官方參考：[非互動執行](https://learn.chatgpt.com/docs/codex/noninteractive)、[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)。

## Ubuntu 24.04 的 AppArmor

若 Codex 回報 `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`，先檢查系統是否限制未授權程式建立 user namespace。Codex CLI 0.162.1 優先使用 PATH 中可用的系統 bubblewrap；本專案提供僅綁定 `/usr/bin/bwrap` 的 [AppArmor 範例](../examples/apparmor/webcodex-bwrap)。

管理員先檢查範例，再在 repo 根目錄執行以下單行命令：

```bash
sudo install -o root -g root -m 0644 examples/apparmor/webcodex-bwrap /etc/apparmor.d/webcodex-bwrap && sudo apparmor_parser -r /etc/apparmor.d/webcodex-bwrap
```

此設定只授予該系統執行檔建立 user namespace 的權限，保留系統全域限制及 Codex 的檔案／網路沙箱。設定檔的可執行檔路徑必須對應實際安裝的 bubblewrap。參考 [Ubuntu AppArmor 說明](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/)。

## 設定隔離與相容性

Worker 共用原 Codex 登入，但忽略使用者設定與 execpolicy rules，並將工作目錄及祖先的 project config 設為不受信任；停用 hooks、外掛、Apps 與遞迴代理。主對話透過 `prompt` 傳遞子任務背景，專案的 `AGENTS.md` 不會自動繼承；主對話需先用 `read_instructions` 讀取相關指引，再將適用規則與使用者限制寫入 `prompt`。原 Codex 登入目錄中的全域指引仍按 CLI 規則載入。

目前以 Linux Codex CLI **0.162.1** 與 Windows **0.163.0-alpha.11.2** 實測；其他版本需確認支援上述執行旗標。Runner 的環境不應含額外 `OPENAI_*` 或 `CODEX_*` 供應商／內部覆寫；`CODEX_HOME` 可指定原有登入目錄。若只有作業系統憑證庫登入而沒有 `auth.json`，此版本會要求改用 CLI 可讀取的登入檔。

Linux 需要可用的 Codex sandbox，macOS 使用 Codex 的原生沙箱。Windows 使用原生 Codex CLI，需先完成 [Windows 沙箱設定](https://learn.chatgpt.com/docs/windows/windows-sandbox)。Runner 會明確指定 `windows.sandbox="elevated"`，避免忽略使用者設定時連沙箱設定也被略過。Windows 已驗證唯讀盤點、兩路隔離教材寫入與字幕複核並行執行。

單筆 CLI 事件上限為 512 KiB，整份結果解析上限維持 8 MiB；超出上限會標記輸出截斷並回報失敗。

## 重現測試

一般測試使用本機假 CLI，驗證真正的程序並行、結果解析、取消、逾時、跨任務隔離與重啟恢復：

```bash
uv run pytest -q tests/test_subagents.py tests/test_subagents_mcp.py tests/test_subagent_activity.py
```

已登入 Codex CLI 且沙箱可用時，可以明確啟用真實模型測試。它使用暫存專案與獨立的 Server／Runner，啟動兩個 worker 分別讀檔與寫檔，會使用目前登入帳號的 Codex 用量：

```bash
WEBCODEX_TEST_SUBAGENTS=1 uv run pytest -q tests/test_subagents_live.py
```
