# 功能參考與後續建議

比對日期：2026-09-11；參考 [yyjeqhc/webcodex @ 04e8135](https://github.com/yyjeqhc/webcodex/tree/04e8135f852a75b572c9a575db0182b07b534eea)。本專案為獨立 Python 實作，以下區分目前能力與建議，並非上游相容性承諾。

## 已採用的 WebCodex 概念

參考上游 [Computer Use](https://github.com/yyjeqhc/webcodex/blob/04e8135f852a75b572c9a575db0182b07b534eea/docs/COMPUTER_USE.md) 的觀察／控制分權、單次截圖世代與不確定結果處理，加入：

- Ubuntu X11／Windows 桌面後端與八個 MCP 工具；Ubuntu 虛擬桌面已驗證，Windows 互動桌面待驗收。
- 獨立的 `computer_observe`、`computer_control` 權限；既有設定不會自動得到桌面存取。
- 每次輸入使用最新 `snapshot_id`，原尺寸座標、尺寸變動檢查、跨專案序列化及持久 job 去重。
- 英文 README 作為預設首頁，與繁體中文版同步；操作細節留在專題文件。

上游已有視窗與控制項身分驗證、Windows UI Automation 和 macOS Accessibility 等語意操作。本次提供座標與按鍵控制，尚未移植這些能力；Ubuntu X11 是本專案另外實作的後端。

## 新專案流程

2026-09-12 再比對 [yyjeqhc/webcodex @ c9f1a54](https://github.com/yyjeqhc/webcodex/tree/c9f1a54fc9e9e3fd21d5005a140b9e97998134db) 的專案操作，補上原先只能對已登記專案建立 task 的缺口：

- `project_roots_list` 探索操作員允許的父目錄，`project_create`／`project_register` 不需要既有 task。
- Runner 建立或登記資料夾並持久保存，再公布可用 project ID；沿用現有權限、核准及工作去重。
- 已驗證從零個專案開始、回報中斷、重啟恢復、路徑邊界與舊資料庫 migration；Windows 新流程仍待實機測試。

實際設定、參考來源與限制見 [建立與登記專案](PROJECTS.md)。目前不提供上游的模板、既有目錄採用或取消登記功能。傳輸沿用原有 Server／Runner 與選用的 Secure MCP Tunnel，不需因建立專案額外加入 Cloudflare 或 SSH。

## 桌面與開發工具建議（尚未實作）

| 優先 | 功能 | 使用價值與最小範圍 |
| --- | --- | --- |
| 1 | 指定視窗與語意控制項 | 參考上游 UIA 路徑，先探索視窗、聚焦與按鈕；Ubuntu 的 AT-SPI 路徑需另外驗證。減少座標錯位及焦點切換風險 |
| 2 | 截圖附件與保留期限 | 參考上游截圖附件及審計分離概念，讓 SQLite 只存 metadata，圖片另存並設定清理期限；保留期限是本專案的延伸建議 |
| 3 | 精簡工作監看頁 | 參考 [Runtime Console](https://github.com/yyjeqhc/webcodex/blob/04e8135f852a75b572c9a575db0182b07b534eea/docs/runtime-console.md)，先以既有 API 顯示 Runner、job、待核准及程序輸出，沿用目前的角色權限 |
| 4 | LSP 程式碼導覽 | 參考 [架構中的 LSP](https://github.com/yyjeqhc/webcodex/blob/04e8135f852a75b572c9a575db0182b07b534eea/docs/ARCHITECTURE.md)，先提供 definition／references／diagnostics；需要可量測優於目前 ripgrep 的使用情境再加入 |

下一步優先完成 Windows 互動桌面與 DPI 驗收，再加入指定視窗控制。Wayland 需要另行評估 compositor／portal 授權流程，不能把 X11 測試當成 Wayland 支援證據。

## Browser Use 比對

比對日期：2026-09-12；參考 [browser-use/browser-use @ 50f2055](https://github.com/browser-use/browser-use/tree/50f205533fe10ba35b553d2a3689c77b87bd5d0a)。Browser Use 本身只做原始碼評估；下列前三項已透過選用的 Playwright MCP plugin 實作，操作方式與實測範圍見 [瀏覽器操作](BROWSER_USE.md)。

`fetch_url` 只讀取 HTTP 回應，不執行 JavaScript；`computer_*` 使用桌面座標與按鍵，沒有網頁元素或分頁模型。`browser-tools` 補上瀏覽器操作，不代替原生桌面控制。

### 功能與實作狀態

| 順序 | 功能與來源 | 本專案狀態與範圍 |
| --- | --- | --- |
| 1 | [網頁元素觀察與操作](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/mcp/server.py#L221) | 已透過 Playwright MCP 提供導覽、URL／標題、元素快照、截圖、點擊、填表與等待。操作後重新取得快照，由原有用戶端推理 |
| 2 | [分頁管理](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/mcp/server.py#L1079)與[登入狀態保存](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/examples/browser/save_cookies.py) | 已支援開啟／列出／切換／關閉分頁及 task 獨立狀態。Provider 使用分頁索引，開關後需重新列出。持久 profile 需操作員明確啟用；不接管日常 Chrome |
| 3 | [結構化輸出](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/examples/features/custom_output.py) | 已可由 `browser_evaluate` 擷取文字、連結與表格並回傳 JSON 文字。固定欄位由用戶端驗證；尚未加入獨立 schema 驗證工具。來源範例使用 Agent／LLM，本實作直接讀取頁面 DOM |
| 4 | [檔案上傳](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/tools/service.py#L860)與[下載](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/examples/features/download_file.py) | 尚未實作受控傳檔。建議上傳只能選擇明確核准的專案檔案；下載驗證完成、大小與 hash 後再匯入專案，沿用路徑與覆寫保護 |
| 5 | [依網站提供敏感資料](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/examples/features/sensitive_data.py) | 尚未實作。建議工具只傳憑證引用，由 Runner 依網站與權限取值。現有 job 紀錄保存輸入參數，需連同截圖與保留期限處理 |

任務間的 cookie／localStorage 隔離與明確選用的保存／恢復已在本機測試頁驗證。網站限制可參考 [BrowserProfile](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/browser/profile.py#L628)，但導航網域清單不等於整個瀏覽器的網路隔離；目前使用的 Playwright 設定同樣不是網路安全邊界。

### 接入方式與取捨

實作重用 stdio MCP、task 專屬 provider session、原生圖片回傳與 `plugin_disconnect`。Playwright MCP 固定為 0.0.80，另行安裝於 Runner；核心沒有增加 Python 或 npm 依賴。

參考版本的 [CLI 入口](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/cli.py#L357) 區分兩種模式：

- `--mcp`：有 `browser_navigate`、`browser_get_state`、`browser_click`、`browser_type` 與分頁工具，較適合先評估。`browser_extract_content` 與 `retry_with_browser_use_agent` 會另用 LLM，首版不納入。
- `--cli-mcp`：提供 `browser_exec` 執行任意 Python，以及截圖工具；不適合視為只有瀏覽器權限的介面。

Browser Use 參考版本預設共用設定目錄與下載位置，不能只靠 MCP session 分離就宣稱瀏覽器隔離。目前核心新增 `allowedTools` 與保留的 `${TASK_STATE_DIR}`，範例僅開放 13 個工具、預設 isolated browser，並要求操作員配置網站範圍。所有呼叫仍使用 `execute` 權限；其他外掛是否隔離狀態，取決於其實際設定。

Ubuntu 已完成真實 MCP 路徑驗收：讀取元素 → 填表 → 等待 JavaScript 更新 → 擷取資料／截圖 → 切換分頁；另驗證跨 task 隔離、導航後舊元素失效、相同 request ID 不重複提交及選用 profile 保存。Windows 瀏覽器整合仍待驗證。上游 Actor 的 CSS 查詢[不會等待元素可見](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/actor/README.md#L60)，本實作使用 Playwright 操作與明確的頁面完成條件。

完整 Agent 執行迴圈、雲端瀏覽器與錄影暫不列入首版。用戶端已提供推理；錄影則等附件儲存與保留期限完成後再評估。
