# Windows 與 Ubuntu 語意桌面控制

依視窗、控制項名稱、角色或 automation ID 定位，透過 Windows UI Automation 或 Ubuntu AT-SPI 按下按鈕、填入文字。Runner 每次操作都重新讀取目前的無障礙控制項樹；移動視窗或調整大小後，沿用相同選擇條件即可。

## 設定

在 client 與 Runner 啟用既有的 `computer_observe`、`computer_control` 權限，並從已登入的桌面工作階段啟動 Runner。

Windows 使用內建的 Windows PowerShell 5.1 與 .NET UI Automation 組件，包含 `UIAutomationClientsideProviders`。Ubuntu 使用系統 Python 與下列套件：

```bash
sudo apt install python3-gi gir1.2-atspi-2.0 at-spi2-core
```

Ubuntu Runner 需繼承桌面工作階段的 `DBUS_SESSION_BUS_ADDRESS`。缺少套件或無障礙服務無法連線時，工具會回傳對應原因。目標應用程式需透過原生無障礙服務提供控制項。

## 尋找與操作控制項

呼叫 `computer_ui_observe`，設 `action="windows"`。選取回傳的 `window_id`，再讀取該視窗：

```json
{
  "task_id": "your-task",
  "action": "tree",
  "window": {"window_id": "returned-window-id"},
  "max_depth": 6,
  "max_nodes": 200
}
```

設 `action="find"` 並傳入選擇條件，僅列出符合的控制項：

```json
{
  "task_id": "your-task",
  "action": "find",
  "window": {"window_id": "returned-window-id"},
  "selector": {"name": "Apply", "role": "button"}
}
```

將選擇條件傳給 `computer_ui_control`：

```json
{
  "task_id": "your-task",
  "action": "invoke",
  "window": {"window_id": "returned-window-id"},
  "selector": {"automation_id": "apply"},
  "request_id": "apply-once"
}
```

`focus` 將焦點移到控制項。`set_text` 以 `text` 取代可編輯欄位內容，也接受空字串。名稱與 ID 必須完全相符，含英文字母大小寫。視窗可用 `window_id`、`pid`、`name` 篩選；控制項可用 `name`、`role`、`automation_id`。同時指定的條件都須符合。

| 結果 | 處理方式 |
|---|---|
| `ambiguous_window` | 選用候選視窗的確切 `window_id`。視窗列表讀取不完整時，也需指定此身分。 |
| `ambiguous_element` | 加上角色或 automation ID，定位單一控制項。 |
| `search_incomplete` | 提高 `max_depth` 或 `max_nodes`，上限分別為 20 與 1,000。 |
| `window_not_found` 或 `stale_element` | 重新觀察目前的視窗與控制項樹。 |
| `unsupported_action` | 查看 `can_invoke`、`can_focus`、`can_set_text`；需要時改用既有截圖與座標工具。 |
| 操作結果未知 | 觀察應用程式目前狀態，確認是否還需要操作。 |

回傳的 `element_id` 用來辨識觀察結果；操作仍使用選擇條件。密碼欄位標為 `protected`，名稱隱去、停用文字取代，工具也不讀取其值。觀察結果提供長度受限的名稱與可用操作。

## 原生驗收

測試會開啟專用視窗、檢查實際應用程式結果，結束後關閉該視窗。

Ubuntu 使用獨立顯示與 D-Bus 工作階段：

```bash
sudo apt install xvfb dbus-x11 gir1.2-gtk-3.0
dbus-run-session -- xvfb-run -a /usr/bin/python3 tests/fixtures/semantic_native_check.py
```

Windows 從桌面工作階段的 repo 根目錄執行：

```powershell
python tests/fixtures/semantic_windows_check.py
```

兩平台皆檢查無障礙控制項樹、密碼遮蔽、Unicode 輸入、按鈕操作、視窗移動與縮放，以及重名控制項。Windows 在有第二台螢幕時，也會將測試視窗移過去，回報移動前後的實際螢幕與 DPI。

### 已完成的原生驗收

| 平台 | 實測環境 | 結果 |
|---|---|---|
| Ubuntu AT-SPI | 獨立 Xvfb 與 D-Bus 工作階段中的 GTK 視窗 | 六項通過。 |
| Windows UI Automation | WinForms 視窗、PowerShell 5.1、64 位元 CLR；由 144 DPI 的 `DISPLAY1` 移到 96 DPI 的 `DISPLAY2` | 六項通過，含跨螢幕與 DPI 改變後的控制項定位、輸入。 |

Windows helper 透過標為 `NoInlining` 的 C# 方法註冊 Win32／WinForms 標準代理表。原生驗收曾發現直接從 PowerShell 註冊會觸發 `NullReferenceException`；這段 C# 初始化使控制項角色與 Invoke／Value pattern 恢復正常。修改 helper 時需保留此初始化。`semantic_windows_diagnose.ps1` 可單獨檢查組件載入與代理註冊。

視窗列舉的失敗分支另由 fake provider 回歸測試：其中一個同名視窗讀取失敗時，以名稱或 PID 操作會回傳 `ambiguous_window`；使用已回傳的確切 `window_id` 才繼續。Windows 測試直接載入正式 PowerShell helper 函式，使用模擬 walker，不開啟桌面。

## API 參考

- [Microsoft: obtaining UI Automation elements](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-obtainingelements)
- [Microsoft: invoke a control through UI Automation](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/invoke-a-control-using-ui-automation)
- [Microsoft: UI Automation and screen scaling](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-screenscaling)
- [GNOME: AT-SPI actions](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/iface.Action.html)
- [GNOME: editable text](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/method.EditableText.set_text_contents.html)

實作使用作業系統的公開無障礙 API。專案授權維持 All rights reserved。
