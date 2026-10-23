# 瀏覽器操作

選用的 `examples/plugins/browser-tools` 使用固定版本的 [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp)，提供網頁元素、表單、分頁、截圖與資料擷取。功能方向參考 [Browser Use 評估](ROADMAP.md#browser-use-比對)，實作沿用既有 plugin 連線與用戶端推理。

## 安裝

Runner 機器需有 Node.js 18+ 與 Google Chrome；Python 核心依賴不變。操作員先在專案外選擇一個目錄安裝 provider，將以下路徑換成實際位置：

```text
npm install --prefix /absolute/path/to/browser-provider --ignore-scripts --no-audit --no-fund --save-exact @playwright/mcp@0.0.80
```

Windows 可使用 `C:/tools/browser-provider`。在既有 `runner.json` 的 `environment` 加入以下欄位，保留原有設定：

```json
{
  "BROWSER_NODE": "/absolute/path/to/node",
  "BROWSER_MCP_CLI": "/absolute/path/to/browser-provider/node_modules/@playwright/mcp/cli.js",
  "BROWSER_ALLOWED_ORIGINS": "http://127.0.0.1:3000"
}
```

Windows 的 Node 路徑例如 `C:/Program Files/nodejs/node.exe`。網站範圍由操作員填入，包含 scheme、host 及非預設 port；多個 origin 用分號分隔。變更 Runner 環境後重啟 Runner。不要將 `node_modules`、瀏覽器 profile 或 Runner 設定放入 Git。

依 [操作教學](OPERATIONS.md#建立任務與呼叫工具) 建立任務。若註冊的工作 repo 就是此原始碼目錄，把以下參數存為 UTF-8 `args.json`：

```json
{"task_id":"TASK_ID","kind":"plugin","source":"examples/plugins/browser-tools","request_id":"install-browser-1"}
```

```text
uv run webcodex call extension_install --arguments '@args.json'
```

若工作 repo 是其他專案，先將整個 `browser-tools` 目錄（含隱藏的 `.codex-plugin`、`.mcp.json`）放入該 repo，再改用其相對路徑。安裝只複製設定，不會執行 npm 或啟動瀏覽器。修改來源範例不會自動更新已安裝的副本。

用以下參數呼叫 `plugin_tools`，確認 provider 能啟動並取得實際 schema：

```json
{"task_id":"TASK_ID","plugin":"browser-tools","server":"browser"}
```

安裝、工具探索與 `plugin_call` 依既有工具權限直接執行，使用 `job_get` 讀取同一工作的結果。

## 操作流程

每次透過 `plugin_call` 呼叫工具：

```json
{
  "task_id": "TASK_ID",
  "plugin": "browser-tools",
  "server": "browser",
  "tool_name": "browser_navigate",
  "arguments": {"url": "http://127.0.0.1:3000"},
  "request_id": "open-page-1"
}
```

安裝範例僅開放以下 13 個工具；精確參數以 `plugin_tools` 回傳為準：

| 用途 | 工具 |
| --- | --- |
| 導覽 | `browser_navigate`、`browser_navigate_back` |
| 觀察與等待 | `browser_snapshot`、`browser_wait_for`、`browser_take_screenshot` |
| 元素操作 | `browser_click`、`browser_type`、`browser_fill_form`、`browser_select_option`、`browser_press_key` |
| 分頁與關閉 | `browser_tabs`、`browser_close` |
| 頁面資料 | `browser_evaluate` |

建議順序：

1. 導覽後呼叫 `browser_snapshot`，`arguments` 使用 `{}`，取得 URL、標題與可互動元素。0.0.80 的一般操作回覆只附快照檔案連結；明確呼叫此工具才取得內嵌的完整快照，無須讀取 Runner 私有檔案。
2. 從最新快照取得 `[ref=eN]`，以 `browser_type` 的 `{"target":"eN","text":"文字"}` 輸入，再讀一次快照；按鈕則使用 `browser_click` 的 `{"target":"最新參照"}`。導航或頁面變動後重新觀察，不能沿用過期參照。
3. 使用 `browser_wait_for` 的 `{"text":"預期完成文字"}` 等待動態結果，再讀快照或截圖確認流程已完成。
4. `browser_tabs` 支援 `list`、`new`、`select`、`close`；用 `{"action":"list"}` 讀取目前分頁，再以 `{"action":"select","index":0}` 選擇。此 provider 使用索引，開關分頁後需重新列出，不能視為穩定 ID。
5. 截圖可用 `browser_take_screenshot` 的 `{"type":"png","scale":"css"}`，圖片經 `job_get` 保留 MCP 原生 image content。最後呼叫 `browser_close`，再以相同 task／plugin／server 參數呼叫 `plugin_disconnect`。

每個新操作使用新的 `request_id`；查詢或重送同一請求時保留原 ID，沿用 Harness 的 job 去重。發生逾時、斷線或結果不確定時，先查 `job_get` 並重新觀察頁面，再決定後續操作；換一個 ID 重送提交、付款或其他修改動作，可能造成重複執行。

## 擷取頁面資料

`browser_evaluate` 可直接回傳頁面文字、連結或表格，不必另外啟動 LLM。例如將以下物件作為該工具的 `arguments`：

```json
{
  "function": "() => Array.from(document.querySelectorAll('tbody tr'), row => ({item: row.cells[0].textContent.trim(), quantity: Number(row.cells[1].textContent)}))"
}
```

選擇器與欄位需符合目標頁面。此版本將 JSON 結果放在 MCP 文字內容的 `### Result` 區段。需要固定欄位時由用戶端驗證，再使用既有檔案工具保存。大型頁面應縮小選取範圍，Harness 對每次 provider 回覆設有 2 MiB 上限。

## 任務隔離與登入保存

預設 `browser.config.json` 使用 `isolated: true`、headless Chrome，cookie 與 localStorage 只在該瀏覽器連線存活期間保留。不同 task 的 provider 使用各自的瀏覽器狀態；不連線到使用者日常 Chrome。`${TASK_STATE_DIR}/artifacts` 保存該 task 的快照與截圖檔案。

需要保存登入時，由操作員修改已安裝的 plugin 設定：

1. 先對該 plugin 的使用中任務呼叫 `browser_close`、`plugin_disconnect`。
2. 在 `browser.config.json` 設定 `browser.isolated` 為 `false`。
3. 在 `.mcp.json` 的 `args` 加入 `"--user-data-dir", "${TASK_STATE_DIR}/profile"`。這兩項需一起修改；isolated 模式不能搭配 user-data-dir。
4. 如需手動登入，將 `browser.launchOptions.headless` 改為 `false`，在 Runner 的互動桌面完成登入；無圖形桌面的機器無法使用此方式。
5. 使用 `browser_close` 正常關閉，讓 profile 寫回後再斷線。同一 task 重連沿用其 profile，其他 task 仍使用不同目錄。

已安裝設定位於 `<state_dir>/extensions/<project_id>/plugin/browser-tools/`；完整私有狀態路徑見 [擴充指南](EXTENSIONS.md#plugin-格式)。此選項適用該安裝副本的所有任務。驗證涵蓋 cookie／localStorage 保存；網站要求重新登入時，需再次完成登入。

## 範圍與驗證

`allowedTools` 會在工具探索與呼叫時檢查，未開放檔案上傳或任意 Node.js 執行工具；`browser_evaluate` 可在頁面執行有副作用的 JavaScript，因此使用 `execute` 工具權限。外掛程序本身仍使用 Runner 帳號的系統權限。

Playwright 的 `allowed-origins` 會阻擋不符合設定的直接請求，但不限制重新導向；網路隔離需另外配置。[官方設定說明](https://github.com/microsoft/playwright-mcp#configuration) 瀏覽器與 plugin 使用 Runner 帳號權限，沒有 OS sandbox。範例未開啟本機 file URL 存取。

profile、快照、截圖與 Harness 的輸入／輸出紀錄目前沒有自動清理期限。`saveSession: false` 不會停用 Harness 紀錄；不要透過文字工具輸入不應被記錄的密碼。檔案上傳、下載驗證後匯入專案、憑證引用及自動清理仍是後續工作。

`tests/test_browser_live.py` 透過真正的 Client → Server → Runner → provider 驗證填表、JavaScript 延遲更新、資料擷取、圖片回傳、分頁、過期元素、請求去重、網站限制、跨任務隔離及選用的狀態保存。設定 `WEBCODEX_TEST_BROWSER_MCP` 為已安裝的 `cli.js` 絕對路徑，再執行 `uv run pytest -q tests/test_extensions.py tests/test_browser_live.py`；未設定時瀏覽器測試會跳過。

目前已在 Ubuntu 的 headless Chrome 實測；CI 已加入 Ubuntu／Windows 執行設定，Windows 的瀏覽器整合仍待該環境驗證。
