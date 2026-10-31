# 瀏覽器操作

選用的 `examples/plugins/browser-tools` 使用固定版本的 [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp)，提供網頁元素、表單、分頁、截圖與資料擷取。功能方向參考 [Browser Use 評估](ROADMAP.md#browser-use-比對)，實作沿用既有 plugin 連線與用戶端推理。

## 選擇操作方式

1. 有合適的 connector／API 時，優先使用。
2. 需要操作網頁介面時，優先使用目前用戶端提供的原生瀏覽器分頁工具；需要既有登入時選可存取該分頁的 Chrome 工具，其他情境可用內建 `@Browser`。以背景分頁完成導航、點擊、填表及驗證，不要求使用者切換到操作中的分頁。
3. ChatGPT 網頁版或其他用戶端未提供原生瀏覽器工具時，先用 `plugins_list` 確認已安裝 `browser-tools`，再依序使用 `plugin_tools`、`plugin_call`，兩者均指定 `plugin=browser-tools`、`server=browser`。未安裝時依下節啟用既有 provider。保留預設 headless 模式，讓網頁操作在背景執行。
4. 原生桌面程式、OS 對話框或瀏覽器工具無法完成的必要步驟，才使用 `computer_*`；完成該步驟後回到瀏覽器工具。網站或工具拒絕權限時，依拒絕原因處理，不轉用桌面工具繞過。

WebCodex 的 headless 瀏覽器使用獨立狀態，不會自動沿用使用者 Chrome 的登入。需要登入的網站，使用原生瀏覽器可存取的已登入分頁，或依「任務隔離與登入保存」建立該 task 的專用登入狀態。

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

Windows 的 Runner 子程序環境不包含 `ProgramFiles`，Playwright 可能只在使用者目錄尋找 Chrome。Chrome 安裝在系統目錄時，在要安裝的 `browser.config.json` 的 `browser.launchOptions` 加入實際存在的 `executablePath`，例如 `"C:/Program Files/Google/Chrome/Application/chrome.exe"`，並保留 `headless: true`。

依 [操作教學](OPERATIONS.md#建立任務與呼叫工具) 建立任務。若註冊的工作 repo 就是此原始碼目錄，把以下參數存為 UTF-8 `args.json`：

```json
{"task_id":"TASK_ID","kind":"plugin","source":"examples/plugins/browser-tools","request_id":"install-browser-1"}
```

```text
uv run webcodex call extension_install --arguments '@args.json'
```

若工作 repo 是其他專案，先將整個 `browser-tools` 目錄（含隱藏的 `.codex-plugin`、`.mcp.json`）放入該 repo，再改用其相對路徑。安裝只複製設定，不會執行 npm 或啟動瀏覽器。修改來源範例不會自動更新已安裝的副本。

既有 Runner 正在執行工作時，也可在各專案已安裝副本的 `.mcp.json` 直接填入 Node、provider 的絕對路徑及允許的網站範圍，取代三個 `BROWSER_*` 變數；保留 `${PLUGIN_ROOT}`、`${TASK_STATE_DIR}`。如此無須重啟 Runner，新 provider 連線即採用設定。`--allowed-origins` 設為 `*` 允許一般 HTTP(S) 網站；有指定網站範圍時保留原限制。已建立的 provider 需先 `browser_close`、`plugin_disconnect` 再連線。

先呼叫 `plugins_list` 確認已安裝 `browser-tools`，再用以下參數呼叫 `plugin_tools`，確認 provider 能啟動並取得實際 schema：

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

安裝範例開放以下 16 個工具；精確參數以 `plugin_tools` 回傳為準：

| 用途 | 工具 |
| --- | --- |
| 導覽 | `browser_navigate`、`browser_navigate_back` |
| 觀察與等待 | `browser_snapshot`、`browser_wait_for`、`browser_take_screenshot` |
| 元素操作 | `browser_click`、`browser_type`、`browser_fill_form`、`browser_select_option`、`browser_press_key` |
| 分頁與關閉 | `browser_tabs`、`browser_close` |
| 頁面資料 | `browser_evaluate` |
| 偵錯 | `browser_console_messages`、`browser_network_requests` |
| 上傳 | `browser_file_upload` |

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

預設 `browser.config.json` 使用 `isolated: true`、headless Chrome，在背景操作網頁而不移動使用者的桌面滑鼠，也不需要將頁面切到前景。cookie 與 localStorage 只在該瀏覽器連線存活期間保留。不同 task 的 provider 使用各自的瀏覽器狀態，不自動沿用使用者日常 Chrome 的登入。`${TASK_STATE_DIR}/artifacts` 保存該 task 的快照與截圖檔案。

需要保存登入時，由操作員修改已安裝的 plugin 設定：

1. 先對該 plugin 的使用中任務呼叫 `browser_close`、`plugin_disconnect`。
2. 在 `browser.config.json` 設定 `browser.isolated` 為 `false`。
3. 在 `.mcp.json` 的 `args` 加入 `"--user-data-dir", "${TASK_STATE_DIR}/profile"`。這兩項需一起修改；isolated 模式不能搭配 user-data-dir。
4. 如需手動登入，暫時將 `browser.launchOptions.headless` 改為 `false`，在 Runner 的互動桌面完成登入；無圖形桌面的機器無法使用此方式。
5. 使用 `browser_close` 正常關閉，讓 profile 寫回後再斷線；手動登入完成後將 `headless` 恢復為 `true`，再重新連線。同一 task 重連沿用其 profile，其他 task 仍使用不同目錄。

已安裝設定位於 `<state_dir>/extensions/<project_id>/plugin/browser-tools/`；完整私有狀態路徑見 [擴充指南](EXTENSIONS.md#plugin-格式)。此選項適用該安裝副本的所有任務。驗證涵蓋 cookie／localStorage 保存；網站要求重新登入時，需再次完成登入。

## 範圍與驗證

`allowedTools` 會在工具探索與呼叫時檢查。上傳使用使用者指定檔案的絕對路徑，經檔案選擇器送往目前頁面。`browser_evaluate` 可在頁面執行有副作用的 JavaScript，因此使用 `execute` 工具權限。外掛程序本身使用 Runner 帳號的系統權限。

Playwright 的 `allowed-origins` 會阻擋不符合設定的直接請求，但不限制重新導向；網路隔離需另外配置。[官方設定說明](https://github.com/microsoft/playwright-mcp#configuration) 瀏覽器與 plugin 使用 Runner 帳號權限，沒有 OS sandbox。範例未開啟本機 file URL 存取。

已完成任務的快照、截圖、process log 與 Server 輸出可透過 `webcodex retention` 設定天數及容量，預設先預覽，加入 `--apply` 才清理。browser profile 與執行中的工作保留。清理範圍與資料庫容量見[保留期限](INSTALLATION.md#容量與保留期限)。

`tests/test_browser_live.py` 透過真正的 Client → Server → Runner → provider 驗證填表、JavaScript 延遲更新、資料擷取、圖片回傳、分頁、過期元素、請求去重、網站限制、跨任務隔離及選用的狀態保存。設定 `WEBCODEX_TEST_BROWSER_MCP` 為已安裝的 `cli.js` 絕對路徑，再執行 `uv run pytest -q tests/test_extensions.py tests/test_browser_live.py`；未設定時瀏覽器測試會跳過。

目前已在 Ubuntu 與 Windows 原生的 headless Chrome 實測。Windows 於 2026-10-28 通過完整瀏覽器整合，包含繁中填表、動態更新、頁面擷取、MCP 圖片、分頁、請求去重、網站限制、任務隔離及登入狀態保存；CI 亦有兩平台的執行設定。


## Profile、重連與檔案操作盤點

目前 provider 固定為 0.0.80，`allowedTools` 開放 16 個工具。以下以實際安裝範例的設定為準：

| 需求 | 現況 | 操作與下一步 |
| --- | --- | --- |
| 每次乾淨環境 | `isolated: true` | 每個 task 使用獨立瀏覽器狀態 |
| 同 task 保留登入 | 已有持久 profile 設定 | `isolated: false` 加 `${TASK_STATE_DIR}/profile`；正常關閉後再斷線 |
| 連線中斷 | provider 連線可重建 | 查原 job，再 `plugin_tools` 連回；以 `browser_snapshot` 取得新參照 |
| Console 錯誤 | `browser_console_messages` | 以 `level` 選擇嚴重度，回傳較嚴重等級的訊息 |
| Network 請求 | `browser_network_requests` | `static: false` 聚焦一般請求，可加 `filter` 篩 URL |
| 上傳 | `browser_file_upload` | 開啟檔案選擇器後傳入指定檔案的絕對路徑；省略 `paths` 取消 |
| 下載與匯入 repo | 使用 task artifact 目錄 | 下載完成後核對檔名、大小與 hash，再匯入指定位置 |

Console、network 與上傳沿用 [Microsoft Playwright MCP 的工具](https://github.com/microsoft/playwright-mcp#tools)，0.0.80 的原生 schema 已核實。既有安裝副本需同步上述三個工具的 allowlist，再呼叫 `plugin_tools` 更新探索結果。要接上使用者已開啟的日常瀏覽器、沿用該瀏覽器的登入時，再評估 CDP／擴充連接。

常用參數：

```json
{"tool_name":"browser_console_messages","arguments":{"level":"error"}}
```

```json
{"tool_name":"browser_network_requests","arguments":{"static":false,"filter":"/api/"}}
```

```json
{"tool_name":"browser_file_upload","arguments":{"paths":["/absolute/path/to/chosen-file.csv"]}}
```

上述物件加上原本的 `task_id`、`plugin`、`server` 與 `request_id` 後交給 `plugin_call`。Windows 的檔案路徑可用 `C:/src/project/chosen-file.csv`。

### 重連順序

1. 工具回覆遺失時，對原 job ID 呼叫 `job_get`。有 process ID 時沿用 `read_process`。
2. provider 已結束時，先 `plugin_disconnect` 清掉該連線，再 `plugin_tools` 建立新連線。
3. 持久模式使用同 task 的 profile；isolated 模式重新登入需要的網站。
4. 重新 `browser_snapshot`，依新參照操作。已送出的提交以頁面結果與原 job 確認，保留原 `request_id`。

### 檔案操作驗收

目前原生測試使用隔離網站與明確指定的小檔案，確認上傳內容、正常下載的大小與 SHA-256，以及 provider 重連後的登入狀態。後續檔案案例需補上取消選擇、同一上傳請求重送、下載失敗、重複檔名與下載中斷。下載完成後再將指定產物移入工作 repo；登入 profile 保持原位置。

`tests/fixtures/browser_native_check.py` 會建立 localhost 測試網站、專用 profile、上傳檔與 artifact 目錄。它實際驗證 console、network 的 200／418、上傳內容、下載大小與 SHA-256，以及關閉 provider 後 cookie／localStorage 的保存：

```text
uv run python tests/fixtures/browser_native_check.py --node /absolute/path/to/node --cli /absolute/path/to/browser-provider/node_modules/@playwright/mcp/cli.js --output .state/browser-native-check.json
```

Chrome 位置不在 provider 預設搜尋範圍時，加上 `--chrome /absolute/path/to/chrome`。測試會正常關閉自己的 provider，清除暫存 profile 與檔案；其他 task 的瀏覽器連線保留。
