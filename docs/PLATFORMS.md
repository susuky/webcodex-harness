# 平台支援與驗證

更新日期：2026-10-23。以下依日期保存實際驗證結果。

## 2026-10-23 移除核准閘門後的驗證

框架已移除逐筆核准與專案信任設定。Windows 完整測試為 306 passed、36 skipped；Ubuntu 為 328 passed、14 skipped，卡片 JavaScript 測試另有 31 passed。升級測試確認舊工作保留原 job ID，已撤銷的權限與已完成任務不會重新執行，重複請求不會重複派工。

Windows 已套用新版 Server，五個專案均在線，`approval_mode="automatic"`、`approval_required=[]`。ChatGPT 雲端外掛命令實際完成、退出碼 0；原教材對話亦成功執行 `computer_click`、`computer_key`、`computer_type`，繼續操作原生 PowerPoint，沒有本機核准步驟。ll1 亦已更新 Server，既有三個專案的 REST 查詢均回報自動執行；原有憑證、專案範圍與工具權限保留。

本輪兩次額外游標探測因教材流程同時擷取新圖，回報 `Stale snapshot`；紀錄保留，未重送輸入。桌面快照在整個 Runner 共用，任何任務的新截圖都會取代舊快照；輸入與觀察應由同一流程依序操作。以下三螢幕與跨螢幕紀錄是先前實機驗收，本輪未重跑整組拖曳。

## 2026-10-23 Windows 跨螢幕雙向拖曳

已新增 Windows 整個虛擬桌面截圖模式（`monitor_id="desktop"`），部署到本機 Server、Runner 與 Tunnel，並從 ChatGPT 外掛完成兩輪來回拖曳。每次使用整個桌面的實體像素定位，落下後再點擊視窗按鈕。

| 項目 | 結果 |
| --- | --- |
| 左側 DISPLAY2 → 主螢幕 DISPLAY1 | 2 次通過，96 → 144 DPI（100% → 150%） |
| 主螢幕 DISPLAY1 → 左側 DISPLAY2 | 2 次通過，144 → 96 DPI（150% → 100%） |
| 游標終點與標題列抓取點 | 4 次誤差均為 0 像素 |
| 按鍵與後續操作 | 4 次拖曳皆放開左鍵，後續 4 次按鈕點擊均收到，視窗未被繼續拖動 |
| 整個桌面截圖 | 來源 6840×2160、原點 `(-1920,0)`；驗收 9 張原生 JPEG 的尺寸與標記像素通過 |
| Windows 回歸 | `test_computer.py`、`test_computer_monitors.py`、`test_windows_keys.py` 共 79 passed |
| Windows 實機回歸 | `test_computer_live.py -k multimonitor`：1 passed、1 deselected，包含跨螢幕往返與 DPI／游標／左鍵核對 |
| ChatGPT 連線 | 完整驗收段 25 次呼叫成功；含試跑與服務冷啟動後檢查共 46 次呼叫，0 次 409 |
| 工作與清理 | 31 筆工作均成功，request ID 無重複；驗收視窗與回歸測試視窗已關閉，三個服務保持執行 |

實機紀錄包含左側 `(-1360,618)→(1600,800)`、主螢幕 `(1600,800)→(-960,800)`，以及第二輪 `(-960,800)↔(1600,800)`。視窗 DPI 在 96／144 間切換，標題列抓取點按 DPI 縮放後仍與游標終點重合。

整個桌面模式保留新截圖、五分鐘期限及排列／DPI 改變失效檢查；已授權的輸入直接執行。輸入端點若位於螢幕間空白區域，會在送出輸入前拒絕；模擬拖曳失敗時亦驗證左鍵釋放。

完整證據位於 `C:\Users\hpisj\.codex\visualizations\2026\10\22\01a161d4-3dab-7d10-9fc7-e1183cee433c\windows-cross-monitor\run-2\results.json`，同目錄保存 9 張 JPEG、各階段事件、實機回歸 XML 與服務冷啟動紀錄。試跑時紀錄器遇到 Windows 讀取鎖；修正定時發布後，另啟新視窗完成上述四次驗收，試跑資料保留於工作紀錄。本輪未測右側螢幕的跨螢幕路徑。

## 2026-10-23 ChatGPT 雲端至 Windows 三螢幕驗收

使用 Windows 本機 Server、Runner、stdio bridge 與官方 `tunnel-client` 0.0.17，經已連接的 ChatGPT 自訂 MCP 外掛執行。Server 與 Runner 使用 Windows 本機副本，執行時不依賴 ll1。

| 項目 | 實際結果 |
| --- | --- |
| Tunnel 與 MCP 探索 | `healthy=true`、`ready=true`，探索到 66 個工具 |
| ChatGPT 雲端呼叫 | `projects_list`、`work_on_project`、`computer_status`、截圖、點擊、拖曳及讀檔通過 |
| 主螢幕 DISPLAY1 | 3840×2160、原點 `(0,0)`、150%；截圖、點擊及拖曳通過 |
| 左側 DISPLAY2 | 1920×1200、原點 `(-1920,352)`、100%；截圖、點擊及拖曳通過 |
| 右側直向 DISPLAY3 | 1080×1920、原點 `(3840,0)`、100%；截圖、點擊及拖曳通過 |
| 應用程式收件 | 解鎖後重驗，每個視窗收到一次點擊、16 次拖曳移動及一次釋放；方塊位移 200 像素 |
| 拖曳全域座標 | 主螢幕 `(1749,1217)→(1949,1217)`；左側 `(-1131,1075)→(-931,1075)`；右側 `(4209,1083)→(4409,1083)` |
| 截圖證據 | 各螢幕操作前、點擊後、拖曳後各一張，共 9 張原生 MCP JPEG；尺寸與標記像素核對通過 |
| 核准與清理 | 6 筆桌面輸入皆先等待本機核准，再觀察同一 job 完成；左鍵已放開，測試視窗已關閉 |

早期驗收曾出現 MCP 初始化 409；已定位 stdio 握手空窗，並於 Windows 啟動流程啟用官方 `MCP_STDIO_SEND_INITIALIZED_NOTIFICATION=true`。修正後本機 12 輪初始化、4 路並行、60 次工具呼叫及 ChatGPT 端 34 次實際呼叫均成功，0 失敗、0 工具重試。

2026-10-23 15:32–15:36（台北時間）解鎖 Windows 後，另建任務 `ed42be79085f4a9ab2c1d922043484b3`，經同一 ChatGPT 外掛重驗上述三個螢幕。25 次雲端呼叫、9 張原生 JPEG、3 次點擊與 3 次拖曳全部成功，0 失敗、0 重試、0 次 409。17 筆工作紀錄成功且沒有重複；左側負座標、150%／100% 混合 DPI 與右側直向螢幕均核對實際視窗事件。測試視窗已關閉，Server、Runner 與 Tunnel 保持執行。證據位於本機 `C:\Users\hpisj\.codex\visualizations\2026\10\22\01a161d4-3dab-7d10-9fc7-e1183cee433c\windows-unlocked-live\results.json`，同目錄保存 9 張截圖、3 份視窗事件紀錄及 25 次雲端呼叫紀錄。本輪未測跨螢幕拖曳。

2026-10-23 已為這台 Windows 設定 DPAPI 加密金鑰與登入啟動捷徑；停止 Server、Runner、Tunnel 後，使用捷徑重啟與 ChatGPT 連線均通過，無需重新輸入金鑰。完整 Windows 重開機尚未驗證。原桌面測試目標為獨立 Tk 視窗，指定截圖軟體尚未提供。

## 2026-10-22 Windows 本機與多螢幕工作區驗證

Windows 11（10.0.26300）、Python 3.12.14，使用 Windows 本機程式副本、獨立 Python 環境與暫存設定。依 `uv.lock` 安裝依賴；執行時的模組來源已確認為本機路徑。

| 項目 | 實際結果 |
| --- | --- |
| Windows `test_computer.py`、`test_computer_monitors.py`、`test_windows_keys.py` | 68 passed，包含導航鍵、滾輪與失敗清理 |
| Linux `test_computer.py`、`test_computer_monitors.py`、`test_windows_keys.py` | 67 passed、1 skipped；略過 Windows INPUT ABI |
| Linux Xvfb `test_computer_live.py` | 1 passed、1 skipped；多螢幕案例限 Windows 明確啟用 |
| Windows 獨立 CLI Server／Runner | 暫存設定的 `init` → `serve`／`runner` → REST／CLI／MCP 專案查詢通過 |
| Windows MCP 工作 | 建立任務、讀檔、命令執行（exit 0）及 `computer_status` 通過；測試程序已停止 |
| Windows 原生螢幕與 DPI | 三個螢幕：主螢幕 3840×2160／144 DPI；左側副螢幕 1920×1200／96 DPI；右側直向 1080×1920／96 DPI |
| Windows 真實三螢幕 MCP 桌面測試 | 3 passed；每個螢幕皆完成 JPEG 像素核對、點擊、Unicode／快捷鍵、移動、滾輪與拖曳 |
| Windows 混合 DPI／負座標 | 150% 主螢幕、100% 左側及直向副螢幕皆通過；測試視窗的原生 DPI 與所選螢幕一致 |
| 拖曳收件驗證 | 左側 `(-1049,1042)→(-849,1042)`、主螢幕 `(1831,1184)→(2031,1184)`、右側 `(4291,1050)→(4491,1050)`；皆核對按下／釋放、游標終點及左鍵已放開 |
| 指定截圖軟體 | 本次使用暫存 Tk 測試視窗；尚未指定目標截圖軟體 |
| Ruff、格式與 `git diff --check` | 通過 |

多螢幕模擬測試涵蓋左方、上方、直向、縮圖座標、重排、拔插、主螢幕與 DPI 變更、過期截圖、輸入清理及 MCP schema／座標轉換。Windows 實機已驗證每個螢幕的負座標及混合 DPI 操作；互動測試與可貼 PowerShell 安裝指令見 [Windows 安裝指南](WINDOWS_RUNNER.md)。完整全庫回歸未完成。

實機測試另發現並修正兩個 Windows 輸入問題：導航鍵缺少擴充旗標時，NumLock 會讓 `Shift+Home` 失去 Shift；滾輪每格需送出 120 delta。修正後三個螢幕皆通過。測試結束後視窗、Server 及 Runner 已關閉。

## 2026-10-22 驗證

| 項目 | 實際結果 |
| --- | --- |
| 本次提交快照的完整 pytest 回歸 | 158 passed、13 skipped，177.53 秒 |
| Ruff／格式檢查／`git diff --check` | 通過 |
| 真實 Codex CLI 0.162.1／MCP 子代理 | 兩個 worker 並行：唯讀取得指定內容、可寫建立內容相同的檔案；14.54 秒 |
| Linux X11／Xvfb 桌面 | 真實 MCP 截圖、Unicode、快捷鍵、點擊、移動、捲動及拖曳通過；13.22 秒 |
| macOS 桌面 | 已加入 Quartz 權限、Retina 及 Unicode 後端；模擬測試通過，實機待驗證 |
| Windows 桌面與子代理 | 桌面後端保留；本次沒有 Windows 實機結果 |

提交前以 Git 暫存內容建立獨立目錄，對該快照執行完整 pytest、Ruff 與格式檢查。

子代理測試使用獨立專案與暫存 Server／Runner，透過真正 TCP MCP 執行；不修改正式專案。`WEBCODEX_TEST_SUBAGENTS=1` 明確啟用真實 CLI 與帳號用量測試，重現方式見 [子代理指南](SUBAGENTS.md)。桌面測試使用獨立 Xvfb，沒有操作使用者正在使用的桌面。

## 2026-09-11 驗證

| 項目 | 實際結果 |
| --- | --- |
| Ubuntu 24.04／Python 3.12.12，含 Xvfb 桌面的完整套件 | 20 passed、7 skipped，67.53 秒 |
| Ruff／格式檢查、wheel／sdist 建置 | 通過 |
| Windows 互動桌面 | 本次未驗收；已實作後端及可在該平台執行的測試 |
| GitHub Actions | 已更新，尚未執行；Ubuntu／Windows、Python 3.11／3.12 矩陣 |

完整套件的 7 個 skipped 包含 4 個既有 Windows 原生測試、1 個 Windows INPUT ABI 測試、未安裝 Cargo，以及未啟用固定 commit 的 HTTPS 安裝測試。不同執行環境請記錄實際 pytest 輸出。

Xvfb 測試透過真正的 TCP MCP／Runner 操作獨立 Tk 視窗，驗證原生 JPEG、繁體中文輸入、快捷鍵、點擊、移動、捲動與拖曳。其他測試涵蓋桌面權限、自動執行、task 綁定、原始座標、截圖失效、重試去重、未知結果、輸入清理與取消時的序列化。

Windows 的新增 Unicode 封包測試驗證 UTF-16、INPUT 結構大小與部分傳送錯誤，在本次 Ubuntu 執行中略過，仍待 Windows 實機驗收。Wayland、WSLg、macOS、多螢幕及混合 DPI 也不在本次驗證範圍。

## 重現

```sh
uv sync --locked --extra computer
uv run --extra computer pytest -q -ra
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv build
```

一般 `pytest` 不會操作真實桌面。Ubuntu Xvfb 與 Windows 的明確啟用方式見 [Computer use 測試](COMPUTER_USE.md#重現桌面測試)。如需驗證 Rust，先讓 Runner 找得到 `cargo`，或設定 `WEBCODEX_TEST_RUST_BIN`；HTTPS 安裝測試另以 `WEBCODEX_TEST_SKILL_COMMIT` 指定 `anthropics/skills` 的完整固定 commit。

[CI 設定](../.github/workflows/test.yml) 安裝選用桌面依賴，並在 Ubuntu 的獨立 Xvfb 執行實際桌面測試。Windows CI 執行原生 API 測試，互動桌面測試保留明確啟用，避免操作非測試桌面。

## 歷史證據

基準版本 `b3c9230` 的 coding 功能曾在 Ubuntu 24.04、Windows 11 Enterprise 10.0.26200 及 WSL2 Ubuntu 22.04 驗收，涵蓋檔案、命令、ConPTY／PTY、Git、擴充與任務延續。完整版本、結果及測試副本位置保留於 [歷史紀錄](archive/PLATFORMS-2026-09-11-b3c9230.md)；新增桌面操作的驗證結果見上方「本次驗證」。

ChatGPT 帳號、tunnel、公開 TLS 與外部 provider 的連線，需依目標環境驗證。
