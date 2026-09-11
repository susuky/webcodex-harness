# 功能參考與後續建議

比對日期：2026-09-11；參考 [yyjeqhc/webcodex @ 04e8135](https://github.com/yyjeqhc/webcodex/tree/04e8135f852a75b572c9a575db0182b07b534eea)。本專案為獨立 Python 實作，以下區分目前能力與建議，並非上游相容性承諾。

## 已採用的 WebCodex 概念

參考上游 [Computer Use](https://github.com/yyjeqhc/webcodex/blob/04e8135f852a75b572c9a575db0182b07b534eea/docs/COMPUTER_USE.md) 的觀察／控制分權、單次截圖世代與不確定結果處理，加入：

- Ubuntu X11／Windows 桌面後端與八個 MCP 工具；Ubuntu 虛擬桌面已驗證，Windows 互動桌面待驗收。
- 獨立的 `computer_observe`、`computer_control` 權限；既有設定不會自動得到桌面存取。
- 每次輸入使用最新 `snapshot_id`，原尺寸座標、尺寸變動檢查、跨專案序列化及持久 job 去重。
- 英文 README 作為預設首頁，與繁體中文版同步；操作細節留在專題文件。

上游已有視窗與控制項身分驗證、Windows UI Automation 和 macOS Accessibility 等語意操作。本次提供座標與按鍵控制，尚未移植這些能力；Ubuntu X11 是本專案另外實作的後端。

## 桌面與開發工具建議（尚未實作）

| 優先 | 功能 | 使用價值與最小範圍 |
| --- | --- | --- |
| 1 | 指定視窗與語意控制項 | 參考上游 UIA 路徑，先探索視窗、聚焦與按鈕；Ubuntu 的 AT-SPI 路徑需另外驗證。減少座標錯位及焦點切換風險 |
| 2 | 截圖附件與保留期限 | 參考上游截圖附件及審計分離概念，讓 SQLite 只存 metadata，圖片另存並設定清理期限；保留期限是本專案的延伸建議 |
| 3 | 精簡工作監看頁 | 參考 [Runtime Console](https://github.com/yyjeqhc/webcodex/blob/04e8135f852a75b572c9a575db0182b07b534eea/docs/runtime-console.md)，先以既有 API 顯示 Runner、job、待核准及程序輸出，沿用目前的角色權限 |
| 4 | LSP 程式碼導覽 | 參考 [架構中的 LSP](https://github.com/yyjeqhc/webcodex/blob/04e8135f852a75b572c9a575db0182b07b534eea/docs/ARCHITECTURE.md)，先提供 definition／references／diagnostics；需要可量測優於目前 ripgrep 的使用情境再加入 |

下一步優先完成 Windows 互動桌面與 DPI 驗收，再加入指定視窗控制。Wayland 需要另行評估 compositor／portal 授權流程，不能把 X11 測試當成 Wayland 支援證據。

## Browser Use 比對

比對日期：2026-09-12；參考 [browser-use/browser-use @ 50f2055](https://github.com/browser-use/browser-use/tree/50f205533fe10ba35b553d2a3689c77b87bd5d0a)。以下為原始碼與範例評估，尚未安裝或驗證整合。

目前 `fetch_url` 只讀取 HTTP 回應，不執行 JavaScript；`computer_*` 使用桌面座標與按鍵，沒有網頁元素或分頁模型。Browser Use 可以補足瀏覽器操作，但不能代替原生桌面控制。

### 值得加入的功能（尚未實作）

| 順序 | 功能與來源 | 本專案的最小範圍 |
| --- | --- | --- |
| 1 | [網頁元素觀察與操作](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/mcp/server.py#L221) | 透過選用 MCP plugin 提供導航、URL／標題、可互動元素、截圖、點擊與填表；適合驗證本機 Web app。先由現有用戶端推理，操作後重新讀取頁面狀態 |
| 2 | [分頁管理](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/mcp/server.py#L1079)與[登入狀態保存](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/examples/browser/save_cookies.py) | 使用穩定分頁 ID；每個 task 使用獨立瀏覽器設定目錄，登入由操作員完成。保存登入狀態需明確選用，不自動接管日常 Chrome |
| 3 | [結構化輸出](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/examples/features/custom_output.py) | 先提供頁面文字、連結及表格，讓用戶端整理；需要固定欄位時沿用現有 Pydantic 驗證。來源範例使用 Agent／LLM，不能當成免模型的 HTML 解析器 |
| 4 | [檔案上傳](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/tools/service.py#L860)與[下載](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/examples/features/download_file.py) | 上傳只能選擇明確核准的專案檔案；下載進獨立 task 目錄，驗證完成、大小與 hash 後再匯入專案。沿用現有路徑與覆寫保護 |
| 5 | [依網站提供敏感資料](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/examples/features/sensitive_data.py) | 工具只傳憑證引用，由 Runner 依網站與權限取值。現有 job 紀錄會保存輸入參數，必須連同截圖與紀錄保留期限處理，不能只遮蔽工具回覆 |

分頁與 task 的隔離是第 1 項接入時就要驗證的邊界；第 2 項再增加登入狀態的保存與恢復。網站限制可參考 [BrowserProfile](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/browser/profile.py#L628)，但導航網域清單不等於整個瀏覽器的網路隔離。

### 最小接入方式

本專案已支援 stdio MCP、task 專屬 provider session、原生圖片回傳與 `plugin_disconnect`。優先重用這條路徑，在獨立環境安裝固定版本的瀏覽器 provider，讓核心套件維持目前依賴。

參考版本的 [CLI 入口](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/cli.py#L357) 區分兩種模式：

- `--mcp`：有 `browser_navigate`、`browser_get_state`、`browser_click`、`browser_type` 與分頁工具，較適合先評估。`browser_extract_content` 與 `retry_with_browser_use_agent` 會另用 LLM，首版不納入。
- `--cli-mcp`：提供 `browser_exec` 執行任意 Python，以及截圖工具；不適合視為只有瀏覽器權限的介面。

目前 `plugin_call` 對 provider 工具一律使用 `execute` 權限，沒有工具白名單；task 專屬 MCP 連線也不會自動隔離 provider 的瀏覽器設定目錄。來源 MCP 預設共用設定目錄與下載位置，因此接入前需限制可呼叫工具、配置 task 專屬目錄，並由操作員固定網站範圍。這些尚未完成，不能宣稱直接安裝後就具備上述隔離。

首版以本機測試頁驗收：讀取元素 → 填表 → 檢查 JavaScript 更新結果 → 切換分頁；另驗證跨 task 隔離、導航後舊元素失效及逾時後不重複送出。上游 Actor 的 CSS 查詢[不會等待元素可見](https://github.com/browser-use/browser-use/blob/50f205533fe10ba35b553d2a3689c77b87bd5d0a/browser_use/actor/README.md#L60)，需另外驗證載入中的頁面，不能把查到元素當成操作已就緒。

完整 Agent 執行迴圈、雲端瀏覽器與錄影暫不列入首版。用戶端已提供推理；錄影則等附件儲存與保留期限完成後再評估。
