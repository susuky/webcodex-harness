# Windows 獨立執行 Server 與 Runner

## 已匯入的教材技能

2026-10-23 已將本機 20 個教材製作技能完整匯入 Windows Runner 的 `local/tobi-benchpress` 與 `local/seminar`。每個專案保存 302 個來源檔案，安裝位置為 Runner `state_dir/extensions/<project>/skill/<name>`，與教材工作樹分開。使用既有擴充安裝器複製技能包，沒有執行安裝 hooks，Server／Runner／client 設定與核准政策保持原值。

ChatGPT 在指定專案建立或續接任務後，先呼叫 `skills_list(task_id)`，再以回傳的完整名稱讀取。已從實際 Windows 外掛驗證兩份 20 項清單、40 次主文件及 6 次支援文件讀取，全部成功、內容雜湊一致，讀取核准次數為 0。

`skill_read` 影片技能的參數：

```json
{
  "task_id": "TASK_ID",
  "name": "installed:video-to-illustrated-lesson",
  "path": "SKILL.md"
}
```

簡報技能使用 `installed:Presentations`（大寫 P）；支援文件可指定 `path="references/template_following.md"`。其他教材技能包含文字編輯、HTML／PPTX、前端驗證、版面調整、PDF、程式審查及依情境使用的桌面操作、影像與手機存取指引。依專案工作計畫選讀；執行腳本與桌面輸入仍依原政策核准。

匯入與雲端驗證紀錄保存在本次安裝的 `windows-teaching-skills/installation.json`、`manifest.json` 與 `verification.json`。

## 安裝與啟動

主流程將 **Server 與 Runner 都放在 Windows**，讓 MCP 用戶端操作這台 Windows 的桌面與軟體。安裝完成後，執行環境不依賴 ll1。在 Windows 已登入、解鎖的桌面開啟 PowerShell，依序貼上各段。

目前原始碼位於 `\\sshfs.r\nvidia@ll1\home\nvidia\ping\webcodex`。本次多螢幕修改還在工作區，因此安裝會複製目前原始碼到 Windows 本機，再建立 Windows 的 Python 環境。Linux 工作區原有的 `.venv` 留在 ll1。

| 位置或連線 | 用途 |
| --- | --- |
| `%LOCALAPPDATA%\webcodex-windows\harness` | Windows 原始碼與 `.venv` |
| `%LOCALAPPDATA%\webcodex-windows\config` | Windows Server、Runner、client 設定與工作紀錄 |
| `%USERPROFILE%\webcodex-desktop-test` | 登記給 ChatGPT 的 Windows 測試目錄 |
| `local/project` | 本機 Server 登記的 Windows 專案 ID |
| Windows `http://127.0.0.1:18765` | Windows Server 的本機入口 |

若要沿用 ll1 的既有 Server，使用文末附錄。兩種方案使用不同設定目錄、token 與 state；每個桌面一次啟動一個 Runner。

## 1. 安裝工具

已有的工具可略過對應行。`winget` 完成後重開 PowerShell，使新的 PATH 生效。

```powershell
winget install --id Git.Git -e
winget install --id BurntSushi.ripgrep.MSVC -e
winget install --id astral-sh.uv -e
```

在新的 PowerShell 確認：

```powershell
git --version
rg --version
uv --version
ssh -V
```

uv 的 Windows 安裝方式見[官方安裝文件](https://docs.astral.sh/uv/getting-started/installation/)。

## 2. 複製目前原始碼並建立 Windows 環境

這是首次安裝區塊，目的地已存在時會停止，保留原有檔案。只複製列出的原始碼、測試與文件，不複製 `.git`、Linux `.venv` 或 Server 設定。

```powershell
$ErrorActionPreference = 'Stop'
$Source = '\\sshfs.r\nvidia@ll1\home\nvidia\ping\webcodex'
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
$Harness = Join-Path $LocalRoot 'harness'
$Project = Join-Path $env:USERPROFILE 'webcodex-desktop-test'

if (-not (Test-Path -LiteralPath (Join-Path $Source 'pyproject.toml'))) {
    throw '找不到 SSHFS 工作區，請先連上 ll1 的檔案掛載。'
}
if (Test-Path -LiteralPath $LocalRoot) {
    throw "目的地已存在，請保留並先檢查：$LocalRoot"
}
New-Item -ItemType Directory -Path $Harness | Out-Null
foreach ($Name in @('src', 'tests', 'examples', 'docs', 'README.md', 'README.zh-TW.md', 'pyproject.toml', 'uv.lock')) {
    Copy-Item -LiteralPath (Join-Path $Source $Name) -Destination $Harness -Recurse
}
New-Item -ItemType Directory -Path $Project -Force | Out-Null
Set-Location -LiteralPath $Harness

uv sync --locked --python 3.12 --extra computer
if ($LASTEXITCODE -ne 0) { throw 'Windows Python 環境建立失敗。' }
uv run --extra computer webcodex doctor
if ($LASTEXITCODE -ne 0) { throw '環境檢查失敗。' }
```

`doctor` 應顯示 `platform: "win32"`、`computer.available: true`；`computer.monitors` 列出螢幕 ID、主螢幕、位置、像素尺寸與 DPI。若桌面後端不可用，先處理回傳的原因。

## 3. 初始化 Windows 本機 Server 與 Runner

此區塊建立獨立設定；既有設定目錄存在時會停止。

```powershell
$ErrorActionPreference = 'Stop'
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
$StandaloneCfg = Join-Path $LocalRoot 'config'
$Project = Join-Path $env:USERPROFILE 'webcodex-desktop-test'
Set-Location -LiteralPath (Join-Path $LocalRoot 'harness')
if (Test-Path -LiteralPath $StandaloneCfg) { throw "設定目錄已存在：$StandaloneCfg" }

uv run --extra computer webcodex init --config-dir $StandaloneCfg --project $Project --port 18765 --computer-use
if ($LASTEXITCODE -ne 0) { throw 'Windows Server 與 Runner 初始化失敗。' }
```

`--computer-use` 會啟用截圖與輸入權限，預設桌面輸入需核准。若希望這個新安裝的命令與桌面輸入免逐筆核准，可在**首次**執行上述 `init` 時加上 `--trusted`。

第一個 PowerShell 視窗啟動本機 Server，保持執行：

```powershell
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
Set-Location -LiteralPath (Join-Path $LocalRoot 'harness')
uv run --extra computer webcodex serve --config (Join-Path $LocalRoot 'config\server.json')
```

第二個 PowerShell 視窗啟動桌面 Runner，保持執行：

```powershell
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
Set-Location -LiteralPath (Join-Path $LocalRoot 'harness')
uv run --extra computer webcodex runner --config (Join-Path $LocalRoot 'config\runner.json')
```

第三個 PowerShell 視窗驗證本機連線：

```powershell
$ErrorActionPreference = 'Stop'
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
Set-Location -LiteralPath (Join-Path $LocalRoot 'harness')
uv run --extra computer webcodex call projects_list --config (Join-Path $LocalRoot 'config\client.json')
if ($LASTEXITCODE -ne 0) { throw 'Windows 本機專案清單查詢失敗。' }
```

應看到 `local/project` 且 `online: true`。Server、Runner 與 client 都讀取 Windows 本機設定；此時可中斷 SSHFS 掛載與 ll1 連線。

## 4. 連接 MCP 用戶端與 ChatGPT

支援本機 stdio 的 MCP 用戶端可使用以下指令產生的 JSON。執行檔與 client 設定均為目前 Windows 使用者的絕對路徑：

```powershell
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
@{
    mcpServers = @{
        webcodex = @{
            command = Join-Path $LocalRoot 'harness\.venv\Scripts\webcodex.exe'
            args = @('bridge', '--config', (Join-Path $LocalRoot 'config\client.json'))
        }
    }
} | ConvertTo-Json -Depth 6
```

將輸出的 `webcodex` 設定加入該 MCP 用戶端。啟動 bridge 時，Windows Server 與 Runner 需持續執行。

### ChatGPT：Windows 原生 Secure MCP Tunnel

Windows 可直接執行 OpenAI 官方 `tunnel-client`，透過上面的本機 stdio bridge 連接 Windows Server。

先在 [Platform 的 Tunnels 設定](https://platform.openai.com/settings/organization/tunnels)建立或選取 tunnel，取得 tunnel ID，並關聯要使用的 ChatGPT 工作區。在 [API keys 設定](https://platform.openai.com/settings/organization/api-keys)準備執行用的 API key；首次以隱藏輸入讀取，通過檢查後加密保存在 Windows 本機。

以下從[官方最新 release](https://github.com/openai/tunnel-client/releases/latest)選取 Windows amd64 ZIP，依 release 的 SHA-256 digest 驗證下載檔，再解壓到新目錄：

```powershell
$ErrorActionPreference = 'Stop'
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
$TunnelZip = Join-Path $LocalRoot 'tunnel.zip'
$TunnelDir = Join-Path $LocalRoot 'tunnel'
if ((Test-Path -LiteralPath $TunnelZip) -or (Test-Path -LiteralPath $TunnelDir)) {
    throw 'Tunnel 下載檔或目錄已存在，請先檢查既有安裝。'
}
$Release = Invoke-RestMethod -Uri 'https://api.github.com/repos/openai/tunnel-client/releases/latest'
$Assets = @($Release.assets | Where-Object { $_.name -match '^tunnel-client-v.+-windows-amd64\.zip$' })
if ($Assets.Count -ne 1) { throw '官方 release 未提供唯一的 Windows amd64 ZIP。' }
$Asset = $Assets[0]
if ($Asset.digest -notmatch '^sha256:[0-9a-fA-F]{64}$') { throw 'Release 缺少可驗證的 SHA-256 digest。' }
Invoke-WebRequest -UseBasicParsing -Uri $Asset.browser_download_url -OutFile $TunnelZip
if ((Get-FileHash -LiteralPath $TunnelZip -Algorithm SHA256).Hash -ne $Asset.digest.Substring(7)) {
    throw 'Tunnel ZIP 的 SHA-256 驗證失敗。'
}
Expand-Archive -LiteralPath $TunnelZip -DestinationPath $TunnelDir
$Executables = @(Get-ChildItem -LiteralPath $TunnelDir -Filter 'tunnel-client.exe' -Recurse -File)
if ($Executables.Count -ne 1) { throw '解壓後找不到唯一的 tunnel-client.exe。' }
$TunnelExe = $Executables[0].FullName
& $TunnelExe --help
if ($LASTEXITCODE -ne 0) { throw 'Tunnel 執行檔檢查失敗。' }
```

保持 Windows Server 與 Runner 執行，在另一個 PowerShell 貼上下列區塊。API key 保存在 `config\tunnel-runtime.dpapi`，以目前 Windows 帳號的 DPAPI 加密；日後啟動直接讀取。Tunnel 程序使用環境變數取得解密後的值，啟動視窗結束時清除該環境變數。

```powershell
$ErrorActionPreference = 'Stop'
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
$Executables = @(Get-ChildItem -LiteralPath (Join-Path $LocalRoot 'tunnel') -Filter 'tunnel-client.exe' -Recurse -File)
if ($Executables.Count -ne 1) { throw '找不到唯一的 tunnel-client.exe。' }
$TunnelExe = $Executables[0].FullName
$ProfileDir = Join-Path $LocalRoot 'tunnel-profiles'
$TunnelId = Read-Host '輸入 Platform 的 tunnel ID'
if ([string]::IsNullOrWhiteSpace($TunnelId)) { throw 'Tunnel ID 不可空白。' }
$McpExe = (Join-Path $LocalRoot 'harness\.venv\Scripts\webcodex.exe').Replace('\', '/')
$McpConfig = (Join-Path $LocalRoot 'config\client.json').Replace('\', '/')
$McpCommand = '"' + $McpExe + '" bridge --config "' + $McpConfig + '"'
$NativeMcpCommand = $McpCommand
if ($PSVersionTable.PSVersion -lt [version]'7.3' -or $PSNativeCommandArgumentPassing -eq 'Legacy') {
    $NativeMcpCommand = $McpCommand.Replace('"', '\"')
}
$KeyPath = Join-Path $LocalRoot 'config\tunnel-runtime.dpapi'
$SecureKey = if (Test-Path -LiteralPath $KeyPath) {
    Get-Content -LiteralPath $KeyPath -Raw | ConvertTo-SecureString
} else {
    Read-Host '首次輸入 API key' -AsSecureString
}
try {
    $env:MCP_STDIO_SEND_INITIALIZED_NOTIFICATION = 'true'
    $KeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
    try {
        $env:CONTROL_PLANE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($KeyPointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($KeyPointer)
    }
    & $TunnelExe init --sample sample_mcp_stdio_local --profile webcodex-windows --profile-dir $ProfileDir --tunnel-id $TunnelId --mcp-command $NativeMcpCommand
    if ($LASTEXITCODE -ne 0) { throw 'Tunnel profile 初始化失敗。' }
    & $TunnelExe doctor --profile webcodex-windows --profile-dir $ProfileDir --explain
    if ($LASTEXITCODE -ne 0) { throw 'Tunnel 檢查失敗。' }
    if (-not (Test-Path -LiteralPath $KeyPath)) {
        $SecureKey | ConvertFrom-SecureString | Set-Content -LiteralPath $KeyPath -Encoding ASCII
    }
    & $TunnelExe run --profile webcodex-windows --profile-dir $ProfileDir
    if ($LASTEXITCODE -ne 0) { throw 'Tunnel 執行失敗。' }
}
finally {
    Remove-Item Env:CONTROL_PLANE_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:MCP_STDIO_SEND_INITIALIZED_NOTIFICATION -ErrorAction SilentlyContinue
    $SecureKey.Dispose()
}
```

`init` 只在首次建立 profile 時執行。日後啟動沿用相同區塊，略過 tunnel ID 輸入、`init` 與緊接的初始化失敗檢查，保留加密金鑰讀取、`doctor`、`run` 與 `finally` 清理。加密檔沿用初始化時建立的私有 `config` 目錄權限，使用相同 Windows 帳號解密。

`MCP_STDIO_SEND_INITIALIZED_NOTIFICATION=true` 啟用官方的初始化完成通知設定。它在成功的 `initialize` 回覆後，先送出 `notifications/initialized` 再放行後續工具請求，並抑制呼叫端稍後送來的重複通知。這會關閉 stdio 握手的空窗，不重送工具操作。設定說明見 [tunnel-client 官方文件](https://github.com/openai/tunnel-client/blob/v0.0.17/docs/configuration.md#L612-L623)。

到 ChatGPT「外掛程式 → 新增 → 新增自訂 MCP 伺服器」，填入名稱，連線選「通道」及剛建立的 tunnel。此 stdio bridge 方案選「無需身分驗證」；Server 的 Bearer 憑證仍由本機 bridge 讀取。完成「建立為外掛程式」與「連接」，確認外掛工具已出現，再執行 `projects_list`。保持 Windows Server、Runner 和 tunnel 運作。官方流程見 [Secure MCP Tunnels](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)；詳細連線驗證見 [ChatGPT 連線指南](CHATGPT.md)。

### 替代方式：公開 HTTPS 入口

若已有公開網域與反向代理，將 HTTPS `/mcp` 轉發至 `http://127.0.0.1:18765/mcp`；把 Windows `server.json` 的 `public_url` 設為公開 HTTPS origin、`allowed_hosts` 加入公開主機名稱，再重啟 Windows Server。

Server 使用靜態 Bearer 驗證，支援 Bearer 的 MCP 用戶端使用這個 Windows 安裝的 client token。要求 OAuth 的 ChatGPT 連線需要提供 OAuth 2.1、資源探索與 PKCE 的公開入口；完整契約見[官方驗證文件](https://developers.openai.com/plugins/build/auth)。自己的 ChatGPT 桌面操作可採上方 Secure MCP Tunnel。

Server 載入新的工具 schema 後，到 ChatGPT 的 Plugins 連線執行 **Refresh**，再開新對話。參考[官方連線與刷新步驟](https://developers.openai.com/plugins/deploy/connect-chatgpt)。

## 5. 檢查桌面與每個螢幕

可直接將以下文字貼進已連線的 ChatGPT：

> 呼叫 projects_list，使用 local/project 建立任務。先呼叫 computer_status，列出 monitors，然後使用各螢幕的 monitor_id 分別呼叫 computer_screenshot，讓我確認每個螢幕的截圖。接著在我指定的軟體執行測試，每次操作後重新截圖確認。

Windows 的 `computer_status.monitors` 提供每個螢幕的 `id`、`primary`、`x`、`y`、`width`、`height`、`dpi_x`、`dpi_y`。`computer_screenshot` 新增可選的 `monitor_id`，省略時截取主螢幕；回傳包含 `monitor_id`、`origin_x`、`origin_y`。

輸入的 `x/y`、拖曳的 `start_x/start_y` 均以**所選螢幕左上角**為原點，使用 `source_width/source_height` 的像素座標。後端負責加上螢幕原點；呼叫端不另外加 `origin_x/origin_y`。縮圖顯示座標需先依 `source_width / width`、`source_height / height` 換算。螢幕布局或 DPI 變更後需取得新的截圖。

桌面輸入回報等待核准時，使用回傳的 `approve_command`，並帶入本機 `server.json` 的完整路徑。例如下列指令列出此安裝的核准項目：

```powershell
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
Set-Location -LiteralPath (Join-Path $LocalRoot 'harness')
uv run --extra computer webcodex approvals --config (Join-Path $LocalRoot 'config\server.json')
```

Windows Runner 應在本機 PowerShell 啟動；SSH 終端機、Windows 服務、鎖定桌面、UAC 安全桌面及斷線 RDP 不提供這份流程所需的互動桌面。

## 6. 驗證多螢幕

先跑模擬後端測試；這段不操作真實桌面：

```powershell
Set-Location -LiteralPath (Join-Path $env:LOCALAPPDATA 'webcodex-windows\harness')
uv run --extra computer pytest -q tests/test_computer_monitors.py
if ($LASTEXITCODE -ne 0) { throw '多螢幕模擬測試失敗。' }
```

真實桌面測試使用獨立測試帳號與至少兩個已啟用的實體螢幕。測試會建立視窗，移動滑鼠、輸入繁體中文，並檢查副螢幕截圖、鍵盤、點擊、移動、捲動及拖曳。

```powershell
Set-Location -LiteralPath (Join-Path $env:LOCALAPPDATA 'webcodex-windows\harness')
$env:WEBCODEX_TEST_DESKTOP = '1'
$env:WEBCODEX_TEST_MULTIMONITOR = '1'
$env:WEBCODEX_TEST_MONITOR_ID = '\\.\DISPLAY2'
$env:WEBCODEX_TEST_SCREENSHOT = Join-Path $PWD 'windows-secondary-monitor.jpg'
try {
    uv run --extra computer pytest -q -ra tests/test_computer_live.py -k multimonitor
    if ($LASTEXITCODE -ne 0) { throw '多螢幕真實桌面測試失敗。' }
}
finally {
    Remove-Item Env:WEBCODEX_TEST_DESKTOP -ErrorAction SilentlyContinue
    Remove-Item Env:WEBCODEX_TEST_MULTIMONITOR -ErrorAction SilentlyContinue
    Remove-Item Env:WEBCODEX_TEST_MONITOR_ID -ErrorAction SilentlyContinue
    Remove-Item Env:WEBCODEX_TEST_SCREENSHOT -ErrorAction SilentlyContinue
}
```

`WEBCODEX_TEST_MONITOR_ID` 填入 `computer_status.monitors` 回傳的 `id`。省略時自動選副螢幕；填入主螢幕 ID 也可驗證。逐一更換 ID，並為截圖指定不同檔名，即可驗收每個螢幕。少於兩個螢幕時測試會顯示 `skipped`。

2026-10-22 已完成以下驗證：

| 項目 | 結果 |
| --- | --- |
| Windows 桌面後端與輸入回歸 | 68 passed |
| Windows 螢幕選取與截圖相關測試 | 5 passed，包含 layered window 擷取旗標 |
| Linux 指定測試集 | 67 passed、1 skipped |
| Xvfb 桌面測試 | 1 passed、1 skipped |
| Windows 完全本機 Server／Runner | CLI、REST、MCP 的查詢、讀檔、命令執行與桌面狀態通過 |
| Windows 螢幕列舉 | 3 個螢幕，DPI 為 144／96／96 |
| Windows 真實三螢幕 MCP 操作 | 3 passed；截圖、點擊、拖曳、Unicode／快捷鍵與滾輪全部通過 |
| 官方 Windows Tunnel 執行檔 | 下載 SHA-256 驗證、`help quickstart` 與 `init`／`doctor`／`run` 參數檢查通過；帳戶連線驗證見下方 2026-10-23 紀錄 |

本次在主螢幕 150%、左側副螢幕 100% 與右側直向副螢幕 100% 各執行一次實測。每個螢幕均核對截圖標記像素、按鈕收件、拖曳按下／釋放的全域座標、游標終點及左鍵已放開。詳細結果見[平台驗證](PLATFORMS.md)。

2026-10-23 已完成實際 ChatGPT 雲端外掛驗收：Windows 本機 Server／Runner 經官方 `tunnel-client` 0.0.17 連線，`healthy=true`、`ready=true`。三個螢幕各取得操作前、點擊後與拖曳後的原生 JPEG，共 9 張；每個測試視窗均收到一次點擊及一次 200 像素拖曳。6 筆輸入皆經本機逐筆核准，沒有更改核准政策。

早期驗收曾出現 Tunnel MCP 初始化錯誤 409。已用原生 Tunnel 對照測試定位到 `initialize` 回覆與 `notifications/initialized` 之間的空窗，並在 Windows 啟動程式啟用上述官方通知設定。修正後 12 輪初始化、4 路並行，共 60 次本機工具呼叫成功；ChatGPT 端 34 次實際呼叫全部成功，0 失敗、0 工具重試。停止 Server、Runner、Tunnel 後，以登入捷徑冷啟動，再從 ChatGPT 查詢、讀檔、讀取桌面狀態與回傳三個原生 JPEG 均成功。16 筆工作紀錄皆成功且無重複。

2026-10-23 15:32–15:36（台北時間）解鎖 Windows 後，再經 ChatGPT 外掛完成三螢幕截圖、點擊及拖曳重驗。9 張原生 JPEG 均通過尺寸與標記像素核對；每個視窗收到一次點擊、16 次拖曳移動及一次釋放，方塊位移 200 像素。左側負座標、主螢幕 150% 與副螢幕 100% 的座標及視窗 DPI 均正確。本輪 25 次雲端呼叫，0 失敗、0 重試、0 次 409；17 筆工作紀錄成功且無重複。6 筆輸入逐筆核准後觀察同一 job，沒有放寬核准政策。測試視窗已關閉，三個服務保持執行。詳細證據位置見[平台驗證](PLATFORMS.md)。本輪未測跨螢幕拖曳。

同日另完成左側 100% 與主螢幕 150% 之間的兩輪來回拖曳，共 4 次。先以 `computer_status.desktop_monitor_id`（`desktop`）擷取整個 Windows 桌面，再將兩端位置換算為這張截圖的來源像素，交給既有 `computer_drag`。兩端皆使用實體像素，不額外乘上螢幕縮放比例，也不把桌面原點加進輸入座標；螢幕間的空白區域不能當作端點。

4 次拖曳的游標終點與標題列抓取點誤差均為 0 像素，DPI 正確切換 96／144，左鍵均已放開；拖曳後 4 次按鈕點擊全數收到。完整驗收段 25 次雲端呼叫成功；含試跑與服務冷啟動後檢查共 46 次呼叫，0 次 409。Windows 79 項回歸與新增跨螢幕實機案例均通過（實機案例 1 passed、1 deselected）。上方多螢幕實機命令現在也會執行跨螢幕往返，詳細證據見[平台驗證](PLATFORMS.md)。

2026-10-23 14:32 已為本次 Windows 安裝保存 DPAPI 加密金鑰，並建立目前使用者的 `Startup\WebCodex Windows.lnk`。登入 Windows 後自動啟動本機 Server、Runner 與 Tunnel，執行時使用 Windows 本機金鑰，不需要 ll1。已在 Windows PowerShell 5.1 停止三個程序後，以同一捷徑重新啟動，全程無金鑰輸入；重複啟動沒有新增程序，ChatGPT 雲端 `projects_list` 與 `computer_status` 成功。Server、Runner、client 設定及核准政策均保持原值。尚未實際重開 Windows 驗證。

停止時先在 Windows Runner 視窗按 `Ctrl+C`，再停止 Windows Server。

## 附錄：改用 ll1 既有 Server

這是另一種部署方式：ll1 Server 維持既有 ChatGPT MCP 入口，Windows 只執行桌面 Runner。完成步驟 1、2 的工具與原始碼準備後，改用本附錄；獨立目錄 `config-ll1` 保存此方案的新 token 與 state。

先初始化獨立的遠端連線設定：

```powershell
$ErrorActionPreference = 'Stop'
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
$Ll1Cfg = Join-Path $LocalRoot 'config-ll1'
$Project = Join-Path $env:USERPROFILE 'webcodex-desktop-test'
Set-Location -LiteralPath (Join-Path $LocalRoot 'harness')
if (Test-Path -LiteralPath $Ll1Cfg) { throw "設定目錄已存在：$Ll1Cfg" }
uv run --extra computer webcodex init --config-dir $Ll1Cfg --project $Project --port 18766 --computer-use
if ($LASTEXITCODE -ne 0) { throw 'll1 方案初始化失敗。' }
```

此方案使用生成的 `runner.json`；Server 讀取 ll1 的既有設定。Windows 的 `init --trusted` 不會改變 ll1 核准政策。

以下使用 Runner ID `windows-desktop`、ll1 client ID `owner`。若 ChatGPT 的既有連線使用其他 client，先將 `$ClientId` 改成該 ID；配對程式會檢查它是否存在。

配對保留 ll1 該 client 的核准政策；輸入需要核准時，使用回傳的 `approve_command` 核准該筆操作。

Runner token 只經 SSH 標準輸入傳送，指令不會顯示它。遠端程式先驗證完整設定，保留其他欄位，在私有目錄備份原檔後原子替換；同名 Runner 已使用不同 token 時會停止。

```powershell
$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
$Harness = Join-Path $LocalRoot 'harness'
$Ll1Cfg = Join-Path $LocalRoot 'config-ll1'
$RunnerPath = Join-Path $Ll1Cfg 'runner.json'
$ClientId = 'owner'

$Runner = Get-Content -LiteralPath $RunnerPath -Raw | ConvertFrom-Json
$Runner.runner_id = 'windows-desktop'
$Runner.server_url = 'http://127.0.0.1:18766'
$Runner | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $RunnerPath -Encoding UTF8

$Payload = @{
    runner_id = $Runner.runner_id
    token = $Runner.token
    project = 'project'
    client_id = $ClientId
} | ConvertTo-Json -Compress
$PayloadBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Payload))
$PairScript = @'
import base64
import json
import os
import secrets
import tempfile
from pathlib import Path

from pydantic import ValidationError
from webcodex.config import ServerConfig, identifier, is_link, private_dir


def pair():
    incoming = json.loads(base64.b64decode('__PAIRING_PAYLOAD_BASE64__'))
    runner_id = identifier(incoming['runner_id'])
    project = runner_id + '/' + identifier(incoming['project'])
    client_id = identifier(incoming['client_id'])
    path = Path('/home/nvidia/.config/webcodex-harness/server.json')
    if is_link(path):
        raise ValueError('Server 設定不可是符號連結。')
    original = path.read_bytes()
    data = json.loads(original.decode('utf-8-sig'))
    current = ServerConfig.model_validate(data)
    if client_id not in current.clients:
        raise ValueError('找不到指定 client；請確認 ClientId。')
    existing = current.runners.get(runner_id)
    if existing is not None and existing != incoming['token']:
        raise ValueError('同名 Runner 已使用其他 token；請先確認既有安裝。')
    data['runners'][runner_id] = incoming['token']
    policy = data['clients'][client_id]
    client = current.clients[client_id]
    policy['projects'] = list(dict.fromkeys([*client.projects, project]))
    policy['permissions'] = sorted(client.permissions | {'computer_observe', 'computer_control'})
    ServerConfig.model_validate(data)
    backup_dir = private_dir(path.parent / 'backups' / ('windows-' + secrets.token_hex(8)))
    backup = backup_dir / 'server.json'
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(original)
    os.chmod(backup, 0o600)
    fd, temporary = tempfile.mkstemp(prefix='.windows-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if is_link(path) or path.read_bytes() != original:
            raise ValueError('設定已被其他程序變更，請重新配對。')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps({'runner': runner_id, 'project': project, 'client': client_id,
                      'backup': str(backup), 'restart_required': ['server']}, ensure_ascii=False))


try:
    pair()
except ValidationError as error:
    raise SystemExit('設定驗證失敗：' + json.dumps(error.errors(include_input=False, include_context=False)))
except (ValueError, OSError, KeyError, TypeError) as error:
    raise SystemExit(str(error))
'@
$PairScript = $PairScript.Replace('__PAIRING_PAYLOAD_BASE64__', $PayloadBase64)
$PairScript | ssh -T nvidia@ll1 'cd /home/nvidia/ping/webcodex && /home/nvidia/ping/webcodex/.venv/bin/python3 -'
if ($LASTEXITCODE -ne 0) { throw 'll1 配對失敗，請依上方訊息處理。' }
Remove-Variable Runner, Payload, PayloadBase64, PairScript
```

配對完成後，**由操作員按原啟動方式重啟 ll1 上的 WebCodex Server**，讓新的 Runner 和 client 權限生效。配對指令本身不重啟服務。

### 開啟 SSH 通道與 Windows Runner

第一個 PowerShell 視窗保持執行：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18766:127.0.0.1:8787 nvidia@ll1
```

`18766` 是此方案的 Windows 本機通道入口，`8787` 是目前 ll1 Server 監聽的埠。若 ll1 日後改用其他埠，修改 `-L` 最後的數字。SSH 登入前先完成主機指紋確認或正常的金鑰／密碼登入。

第二個 PowerShell 視窗在 Windows 正常桌面執行，保持開啟：

```powershell
$LocalRoot = Join-Path $env:LOCALAPPDATA 'webcodex-windows'
Set-Location -LiteralPath (Join-Path $LocalRoot 'harness')
uv run --extra computer webcodex runner --config (Join-Path $LocalRoot 'config-ll1\runner.json')
```

看到 `Runner windows-desktop connected` 後，在另一個 PowerShell 查 ll1 的專案清單：

```powershell
ssh -T nvidia@ll1 'cd /home/nvidia/ping/webcodex && /home/nvidia/ping/webcodex/.venv/bin/python3 -m webcodex call projects_list'
if ($LASTEXITCODE -ne 0) { throw '專案清單查詢失敗。' }
```

`windows-desktop/project` 應出現且 `online: true`。此查詢使用 ll1 的 `client.json`；ChatGPT 連線使用的 client 應已在上方配對區塊授權。Refresh 連線並開新對話後，使用步驟 5 的提示詞，將專案 ID 改成 `windows-desktop/project`。

停止時先在 Windows Runner 視窗按 `Ctrl+C`，再關閉 SSH 通道。ll1 既有 ChatGPT MCP 入口與其他 Runner 可繼續使用。
