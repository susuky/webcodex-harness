# Computer use：Ubuntu、macOS 與 Windows

提供八個 MCP 工具，沿用 Server／Runner 的驗證、核准、持久 job 與重試去重。三個平台皆使用 PyAutoGUI 與 Pillow；Ubuntu 使用 X11 與 xdotool，macOS 使用 Quartz Unicode 事件，Windows 使用 Unicode `SendInput`。

實際操作的是任務所屬 **Runner 電腦的桌面**。Server 只轉送請求與結果；ChatGPT 網頁開在哪台電腦，不會改變操作目標。例如 Server 在 Linux、Runner 在 Windows 時，工具操作 Windows 桌面。

## 啟用

Ubuntu 先安裝桌面依賴，登入 **Ubuntu on Xorg** 工作階段，並在該桌面的終端機啟動 Runner：

```bash
sudo apt-get install -y python3-tk xdotool
uv sync --locked --extra computer
uv run --extra computer webcodex init --project "$HOME/src/my-repo" --computer-use
```

Windows 在已登入且解鎖的桌面開啟 PowerShell：

```powershell
uv sync --locked --extra computer
uv run --extra computer webcodex init --project 'C:\src\my-repo' --computer-use
```

macOS 在已登入且解鎖的桌面開啟 Terminal，使用 macOS 10.15 以上版本：

```bash
uv sync --locked --extra computer
uv run --extra computer webcodex init --project "$HOME/src/my-repo" --computer-use
```

在「系統設定 → 隱私權與安全性」中，將「螢幕錄製」授予啟動 Runner 的應用程式；需要滑鼠或鍵盤輸入時，再授予「輔助使用」。例如從 Terminal 啟動，就授予 Terminal。調整後重新啟動該應用程式與 Runner。選用依賴已包含 PyAutoGUI 所需的 PyObjC／Quartz；不需要另外安裝桌面控制套件。

`init` 僅用於新安裝；Server 仍需在另一個終端機執行 `uv run webcodex serve`。桌面 Runner 與環境檢查使用：

```text
uv run --extra computer webcodex doctor
uv run --extra computer webcodex runner
```

既有安裝不重跑 `init`。停機後，只修改以下權限陣列，保留原值及憑證，然後重啟 Server 與 Runner：

| 設定 | 新增項目 |
| --- | --- |
| `server.json` → `clients` → 所選用戶端 → `permissions` | `computer_observe`、`computer_control` |
| 同一用戶端 → `approval_required` | `computer_control`，若希望輸入前核准 |
| `runner.json` → `permissions` | `computer_observe`、`computer_control` |

只需截圖時，兩端僅加入 `computer_observe`。既有設定的核准政策不會自動改寫；`--trusted` 本身不啟用桌面工具，新安裝同時使用 `--computer-use --trusted` 才會啟用並省略核准。

## 工具與操作順序

先用 `task_create` 或 `task_get` 取得這台 Runner 上專案的 `task_id`，再使用以下工具。完整 schema 可用 `webcodex tools` 查詢。

| 工具 | 參數（另需 `task_id`） | 效果 |
| --- | --- | --- |
| `computer_status` | 無 | 檢查後端、畫面尺寸及文字輸入依賴；不截圖 |
| `computer_screenshot` | `max_dimension`，預設 1600 | 回傳最多 1 MiB 的原生 MCP JPEG 與 `snapshot_id` |
| `computer_move` | `snapshot_id`、`x`、`y` | 移動游標 |
| `computer_click` | `snapshot_id`、`x`、`y`、`button`、`clicks` | 左／右／中鍵；一次或雙擊 |
| `computer_scroll` | `snapshot_id`、`x`、`y`、`clicks` | 指定位置垂直捲動，正數向上、負數向下 |
| `computer_drag` | `snapshot_id`、`start_x`、`start_y`、`x`、`y`、`duration` | 按住左鍵拖曳後釋放，0.2–3 秒 |
| `computer_key` | `snapshot_id`、`keys` | 單鍵或最多四鍵組合，如 `["ctrl","a"]`、`["enter"]` |
| `computer_type` | `snapshot_id`、`text` | 輸入最多 4096 字元的可列印 Unicode，不讀寫剪貼簿 |

狀態與截圖需要 `computer_observe`，六種輸入需要 `computer_control`。輸入工具均使用新的截圖 ID；`computer_key` 使用 PyAutoGUI 小寫鍵名。macOS 快捷鍵使用 `command`，例如 `["command","a"]`。換行與 Tab 請明確呼叫按鍵工具，不混入文字。`computer_type` 在 Ubuntu 約每字 12 ms，最長 60 秒逾時；應用程式須持續處理事件。

標準流程為：**截圖 → 一次操作 → 新截圖確認**。`snapshot_id` 綁定 task、畫面尺寸及 Runner 記憶體中的最新截圖，五分鐘後失效。任何 task 取得新截圖、已送出輸入或 Runner 重啟，都會讓舊 ID 失效。核准等待超過期限時，需重新觀察並建立新操作。

座標採 `source_width`／`source_height`；若圖片縮小，換算為 `x = 圖片上的 x × source_width / width`，`y` 同理，再取整數。Ubuntu 觀察整個 X11 screen；macOS 與 Windows 限主螢幕。macOS 使用滑鼠座標的邏輯尺寸，Retina 截圖會先轉為同一尺寸，再依 `max_dimension` 縮小。不支援指定視窗、螢幕編號或自動判讀控制項。

CLI 範例：將 `{"task_id":"TASK_ID"}` 存入 UTF-8 `args.json` 後執行：

```text
uv run webcodex call computer_screenshot --arguments '@args.json'
```

若回傳 `queued`／`running`，用 `job_get` 取得結果，再將參數改為：

```json
{"task_id":"TASK_ID","snapshot_id":"SCREENSHOT_ID","x":300,"y":200,"request_id":"click-1"}
```

```text
uv run webcodex call computer_click --arguments '@args.json'
```

依 [操作教學](OPERATIONS.md) 核准與觀察同一 job。MCP 用戶端會收到原生圖片；REST／CLI 的 JSON 則保留 base64 圖片資料。

## 執行邊界

- 工具共用整個桌面，可能看見或操作專案外資料。每個桌面只使用一個 Runner，避免不同實例或人為操作互相干擾。
- 截圖 ID 可防止誤用舊尺寸或重複輸入；人為切換視窗或程式自行重排仍會改變畫面。輸入後重新截圖，確認目標程式是否已儲存或更新。
- 游標位於畫面角落時，PyAutoGUI 的 fail-safe 拒絕後續輸入。執行中的 Unicode 輸入不會因移至角落立刻停止；停止 Runner 時已開始的原生呼叫仍可能完成，應重新確認畫面。
- 工具在已開始輸入後失敗或遭中斷，job 標為 `unknown`。先重新截圖查證，不自動重送。相同 `request_id` 與參數只會讀回原 job。
- 截圖與輸入文字會保存在 Server／Runner 私有 SQLite 工作紀錄；事件摘要不含圖片，但工作結果包含。尚無自動清理，不應用此介面輸入秘密。

| 環境 | 範圍 |
| --- | --- |
| Ubuntu X11／Xvfb | 已有真實 MCP 桌面測試，包含繁體中文與六種輸入 |
| macOS | 已實作權限檢查、主螢幕截圖、Retina 座標與 Quartz Unicode 輸入；已加入模擬後端測試，尚未執行 Mac 桌面實測。結構化 Git／patch helper 在完成目錄綁定支援前拒絕啟動 |
| Windows 原生 | 已實作互動桌面檢查與輸入後端；本次未取得 Windows 桌面實機證據 |
| Windows SSH／服務／斷線 RDP／鎖定或 UAC 桌面 | 不支援；請在登入者的正常桌面啟動 Runner |
| macOS 未登入主控台或鎖定桌面 | 後端拒絕啟用；請解鎖並從該桌面啟動 Runner |
| Wayland／WSLg | 不支援，後端拒絕啟用；控制 Windows 桌面請使用原生 Windows Runner |
| 多螢幕、混合 DPI、遠端桌面切換 | 尚未驗收 |

`doctor.computer.available` 表示後端與尺寸查詢可用。接著呼叫截圖與輸入工具，確認權限和目標程式的實際行為。

## 重現桌面測試

測試會移動滑鼠及輸入，只在可丟棄的桌面執行。Ubuntu 可使用獨立 Xvfb：

```bash
sudo apt-get install -y xvfb xauth python3-tk xdotool
XLOCALEDIR=/usr/share/X11/locale WEBCODEX_TEST_DESKTOP=1 \
  xvfb-run -a -s '-screen 0 1280x800x24 -nolisten tcp' \
  uv run --extra computer pytest -q tests/test_computer_live.py
```

`XLOCALEDIR` 指定系統的 X11 locale 資料，讓 uv 提供的 Python／Tk 在測試視窗正確接收繁體中文與其他 Unicode 字元。

Windows 在獨立測試帳號、解鎖的互動桌面執行：

```powershell
$env:WEBCODEX_TEST_DESKTOP = '1'
uv run --extra computer pytest -q tests/test_computer_live.py
Remove-Item Env:WEBCODEX_TEST_DESKTOP
```

macOS 使用含 Tk 的 Python，在獨立測試帳號、解鎖的桌面授予上述權限後執行同一測試：

```bash
WEBCODEX_TEST_DESKTOP=1 uv run --extra computer pytest -q tests/test_computer_live.py
```

可另設 `WEBCODEX_TEST_SCREENSHOT` 為輸出 JPEG 的絕對路徑。一般 `pytest` 不會操作真實桌面；權限、snapshot、去重、輸入清理及 Windows 封包測試獨立執行。
