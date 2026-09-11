# 上游比對與後續建議

比對日期：2026-09-11；參考 [yyjeqhc/webcodex @ 04e8135](https://github.com/yyjeqhc/webcodex/tree/04e8135f852a75b572c9a575db0182b07b534eea)。本專案為獨立 Python 實作，以下區分目前能力與建議，並非上游相容性承諾。

## 本次採用

參考上游 [Computer Use](https://github.com/yyjeqhc/webcodex/blob/04e8135f852a75b572c9a575db0182b07b534eea/docs/COMPUTER_USE.md) 的觀察／控制分權、單次截圖世代與不確定結果處理，加入：

- Ubuntu X11／Windows 桌面後端與八個 MCP 工具；Ubuntu 虛擬桌面已驗證，Windows 互動桌面待驗收。
- 獨立的 `computer_observe`、`computer_control` 權限；既有設定不會自動得到桌面存取。
- 每次輸入使用最新 `snapshot_id`，原尺寸座標、畫面變動檢查、跨專案序列化及持久 job 去重。
- 英文 README 作為預設首頁，與繁體中文版同步；操作細節留在專題文件。

上游已有視窗與控制項身分驗證、Windows UI Automation 和 macOS Accessibility 等語意操作。本次提供座標與按鍵控制，尚未移植這些能力；Ubuntu X11 是本專案另外實作的後端。

## 建議順序（尚未實作）

| 優先 | 功能 | 使用價值與最小範圍 |
| --- | --- | --- |
| 1 | 指定視窗與語意控制項 | 參考上游 UIA 路徑，先探索視窗、聚焦與按鈕；Ubuntu 的 AT-SPI 路徑需另外驗證。減少座標錯位及焦點切換風險 |
| 2 | 截圖附件與保留期限 | 參考上游截圖附件及審計分離概念，讓 SQLite 只存 metadata，圖片另存並設定清理期限；保留期限是本專案的延伸建議 |
| 3 | 精簡工作監看頁 | 參考 [Runtime Console](https://github.com/yyjeqhc/webcodex/blob/04e8135f852a75b572c9a575db0182b07b534eea/docs/runtime-console.md)，先以既有 API 顯示 Runner、job、待核准及程序輸出，沿用目前的角色權限 |
| 4 | LSP 程式碼導覽 | 參考 [架構中的 LSP](https://github.com/yyjeqhc/webcodex/blob/04e8135f852a75b572c9a575db0182b07b534eea/docs/ARCHITECTURE.md)，先提供 definition／references／diagnostics；需要可量測優於目前 ripgrep 的使用情境再加入 |

下一步優先完成 Windows 互動桌面與 DPI 驗收，再加入指定視窗控制。Wayland 需要另行評估 compositor／portal 授權流程，不能把 X11 測試當成 Wayland 支援證據。
