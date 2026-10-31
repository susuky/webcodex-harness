---
name: windows-desktop-testing
description: 使用者要操作或測試 Windows 上的網站、軟體、畫面與多螢幕時使用。網頁優先使用原生瀏覽器工具，無法使用時選 WebCodex Playwright；只有必要的桌面步驟才使用 computer use。
---

# Windows 網頁與桌面操作

## 先選操作方式

1. 已有合適的專用連線、API 或檔案工具能完成工作時，直接使用。
2. 需要操作網頁介面時，先確認目前對話實際可用的原生瀏覽器工具。需要使用者已登入的分頁時選 Chrome；適合獨立瀏覽器環境時選內建 Browser。確認瀏覽器位於目標電腦，尤其是 localhost 網址。預設在背景分頁操作，保留使用者的前景分頁與滑鼠位置。
3. 原生瀏覽器工具不可用時，包括未提供這些工具的 ChatGPT 網頁版對話，使用本外掛的 WebCodex Windows 工具及已安裝的 `browser-tools` Playwright 外掛。不要只因缺少 `@Chrome` 就直接操作桌面。
4. `computer_*` 會操作 Runner 的共用桌面。只有原生桌面軟體、作業系統視窗，或可用瀏覽器工具確實無法完成的必要步驟，才進入下方桌面流程。完成該步驟後回到瀏覽器工具。網站或工具的權限遭拒時，回報具體原因，不改用桌面控制繞過。

## WebCodex 瀏覽器流程

1. 呼叫 `projects_list`，選擇目標 Windows Runner 上的確切專案。以 `work_on_project` 開始，或用原 task ID 接續；保存 task ID，依回傳的 `next_action` 處理工作卡與等待。
2. 呼叫 `plugins_list(task_id)`，確認 `browser-tools` 已安裝，再呼叫 `plugin_tools(task_id, plugin="browser-tools", server="browser")` 取得實際工具及參數。缺少外掛或 provider 時，依具體錯誤處理安裝設定，不宣稱瀏覽器已可用。
3. 經 `plugin_call` 指定同一 task、`plugin="browser-tools"`、`server="browser"` 與實際 `tool_name`。先 `browser_navigate`，再 `browser_snapshot`；從最新快照取得元素參照，依實際 schema 呼叫輸入、點擊、表單與分頁工具。導覽或頁面變動後重新觀察。
4. 用 `browser_wait_for`、新快照或 `browser_take_screenshot` 確認應用程式實際結果。網頁截圖使用瀏覽器工具；不用桌面截圖尋找已知網頁。
5. Playwright 預設使用 headless Chrome，每個 task 的瀏覽器狀態獨立，且不沿用使用者日常 Chrome 的登入。需要登入時使用已授權的獨立登入流程；不要複製日常瀏覽器的 profile 或 cookie。完成工作後呼叫 `browser_close` 與 `plugin_disconnect`，只關閉本任務的瀏覽器連線。

## 必要時操作桌面

1. 使用同一個 Windows 任務呼叫 `computer_status`，確認 `available=true`、`backend=windows`，讀取 `monitors`。
2. 使用 `computer_screenshot` 的 `monitor_id` 選取目標螢幕；只有目標尚未定位或需要多螢幕驗證時，才查看其他螢幕。將相關原生影像顯示給使用者。以當次狀態為準，不假定螢幕數量、位置或縮放比例；仍無法辨認目標時再請使用者指定。
3. 每次輸入使用本任務最新截圖的 `snapshot_id`。座標以所選螢幕的 `source_width`、`source_height` 為準，從左上角 0,0 起算。縮圖位置先換算成來源像素，不加入桌面原點或負座標。
4. 使用 `computer_click`、`computer_drag`、`computer_scroll`、`computer_key`、`computer_type` 完成必要操作。一次輸入會消耗 snapshot；操作後重新截圖確認結果。螢幕排列或 DPI 改變時，重新讀取狀態與截圖。驗證點擊時檢查應用程式收到的結果；拖曳則檢查位置變化與釋放結果。

## 工作續接與驗證

同一瀏覽器或桌面工作依序操作，確認目前 job 的結果後再進行下一步。已授權的命令與操作直接執行；queued/running job 用 `job_get`，已有 `process_id` 的命令用 `read_process` 觀察同一程序。重試同一請求沿用 `request_id`；結果不確定時先查原 job 並重新觀察，不重送修改動作。

需要命令、檔案或測試時，先讀取 `workspace_info` 與相關指引，保留無關變更。機密不要放入輸入工具或其他會記錄的工具參數。完成後回報實際使用的瀏覽器或螢幕、操作結果及相關畫面證據，並以 `task_update` 儲存結果。
