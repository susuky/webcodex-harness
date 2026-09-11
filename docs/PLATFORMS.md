# 平台支援與驗證紀錄

更新日期：2026-09-11。Ubuntu、Windows 原生及 Windows WSL2 均已實機測試；以下記錄實際版本、結果與驗證範圍。

## 目前證據

| 環境 | 狀態 | 驗證結果 |
| --- | --- | --- |
| Ubuntu 24.04.2 x86_64／Python 3.12.12 | 已實測 | 15 passed、4 skipped；跳過項目全部是 Windows 專用 API 測試 |
| Ubuntu 24.04.2 x86_64／Python 3.11.15 | 已實測 | 14 passed、5 skipped；另外跳過一次需網路的固定 commit 安裝測試 |
| Windows 11 Enterprise 10.0.26200 x64／Python 3.12.13 | 已透過 SSH 實機測試 | 19 passed，無 skipped；69.20 秒 |
| Windows WSL2／Ubuntu 22.04.4／Python 3.12.14 | 已在同一台 Windows 主機實測 | 15 passed、4 skipped；64.17 秒；跳過項目全部是 Windows 專用 API 測試 |
| GitHub Actions | 已加入，尚未執行 | Ubuntu 24.04、Windows Server 2022，Python 3.11／3.12 矩陣 |

三個平台的完整測試均包含真實 MCP TCP 連線、stdio bridge、讀改 repo、pytest 先失敗再修正通過、cargo test、Git diff、shell、互動終端、核准、去重、任務延續、provider 連續呼叫及固定 commit HTTPS 安裝。Rust 測試使用明確指定的本機工具鏈；不代表所有登入 shell 的 PATH 都已有 Cargo。

三個平台也另外以獨立暫存 repo 實際執行 CLI 的 `init` → `serve`／`runner` → `projects_list` → `task_create` → `approvals`／`approve` → 命令結束碼 0 → `task_update`／`task_get`，並驗證 UTF-8 BOM 參數檔。Ubuntu CLI 另涵蓋讀寫檔案；各平台完整 MCP 測試也驗證檔案操作。測試服務結束後已停止。

Ubuntu 與 Windows 原生的 Ruff 檢查、格式檢查及 wheel／sdist 建置已通過。Windows x64 Python 3.11 另外通過僅允許 binary wheel 的依賴解析，但未在該 Python 版本執行 Windows 測試。以上驗證不代表 ChatGPT 帳號、tunnel 或其他外部服務已完成連線。

本次 Windows 使用系統 Windows PowerShell 5.1、ConPTY、既有 Visual Studio 2022／MSVC 14.32 與 Windows SDK 10.0.19041。WSL2 核心為 `5.15.133.1-microsoft-standard-WSL2`。兩者均使用獨立安裝的 Rust 1.98.1 與 ripgrep 15.2.0；沒有更換系統 Python、修改全域 PATH 或使用正式 Server 憑證。

Windows 實測修正了 Win32 常數的模組來源、未命名 Job Object 的建立參數，以及命令環境缺少 `PROGRAMDATA` 造成的 MSVC linker 探索失敗；上述完整 Windows 測試使用修正後的程式。

## 原生 Windows 的實作差異

| 項目 | Windows 原生 | Ubuntu／WSL2 |
| --- | --- | --- |
| `command` | PowerShell，優先 pwsh | Bash |
| 明確命令 | `argv`，使用 Windows 執行檔路徑 | `argv`，使用 Linux 執行檔路徑 |
| 互動終端 | pywinpty／ConPTY | POSIX PTY |
| 程序回收 | Job Object、啟動前加入、關閉 handle 時回收 | 獨立 process group、signal |
| Runner 排他鎖 | msvcrt byte-range lock | flock |
| 檔案保護 | 逐層 pin 目錄，拒絕 reparse point、junction、ADS、裝置名稱 | directory fd、O_NOFOLLOW |
| 設定權限 | 目前帳號與 SYSTEM 的 DACL | 0700 目錄、0600 設定檔 |
| 文字 | UTF-8 JSON／檔案，支援參數檔 BOM | UTF-8 JSON／檔案 |

以上行為已納入實機驗收。Windows 使用本機 NTFS；WSL2 使用 Linux home 內的檔案。未驗收網路磁碟、Windows 雲端占位檔、`/mnt/c` 的檔案權限、Windows 10 或 macOS。

## 本次 SSH 測試副本

提供的 Windows 主機保留以下獨立目錄，方便重現；CLI 冒煙測試建立的臨時 repo 與憑證已移除：

- Windows：`%LOCALAPPDATA%\webcodex-validation\20260911-a72f301c\`，程式在 `harness\`；`pytest-windows.log`、`pytest-windows.xml`、`doctor-windows.json` 與 `runtime-smoke-windows.log` 保存最終結果。
- WSL2 Ubuntu：`~/.cache/webcodex-validation/20260911-a72f301c/`，程式在 `harness/`；`pytest-wsl.log` 與 `doctor-wsl.json` 保存結果。

這些副本的額外工具只安裝在測試目錄。Windows 在新的 PowerShell 中可暫時設定目前程序的 PATH：

```powershell
$validationRoot = Join-Path $env:LOCALAPPDATA 'webcodex-validation\20260911-a72f301c'
$rustBin = Join-Path $validationRoot 'rustup\toolchains\stable-x86_64-pc-windows-msvc\bin'
$rgBin = Join-Path $validationRoot 'tools\ripgrep-15.2.0-x86_64-pc-windows-msvc'
Set-Location (Join-Path $validationRoot 'harness')
$env:PATH = "$PWD\.venv\Scripts;$rgBin;$rustBin;$env:PATH"
.venv\Scripts\python.exe -m webcodex doctor
```

在 WSL2 Ubuntu 的 Bash 中：

```bash
validation_root="$HOME/.cache/webcodex-validation/20260911-a72f301c"
cd "$validation_root/harness"
export PATH="$PWD/.venv/bin:$validation_root/tools:$validation_root/rustup/toolchains/stable-x86_64-unknown-linux-gnu/bin:$PATH"
python -m webcodex doctor
```

接著可從 [操作教學第 2 節](OPERATIONS.md#2-第一次初始化) 選擇自己的工作 repo 並初始化。測試沒有建立預設正式設定，也沒有留下常駐 Server／Runner。

## 在 Windows 原生驗收

完成 [操作教學](OPERATIONS.md) 的安裝，在 **PowerShell** 中切換到 Harness 目錄。確認 `git`、`rg`、`cargo` 都能執行：

```powershell
uv sync --locked --python 3.12
uv run webcodex doctor
git --version
rg --version
cargo --version
$env:WEBCODEX_TEST_SKILL_COMMIT = '41bbe19d1a1a7eaab5e7bb9050a417e5c6cffc8f'
uv run pytest -q -ra
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv build
```

設定的 commit 屬於公開 `anthropics/skills`；該測試會連網下載固定版本。若不執行網路測試，可省略環境變數，但驗收紀錄須保留該 skipped 項目。

在完整工具鏈及網路條件下，現在共有 19 項測試，Windows 專用的 4 項不得跳過；Cargo 不存在而被跳過，也不能算 Rust 執行已驗收。跨平台主測試本身還包含 ConPTY 輸入／resize、PowerShell 中文輸出與完整 MCP 操作。

`tests/test_windows.py` 額外檢查：

1. 中文與空白路徑、設定 DACL、ADS／裝置名稱拒絕與 UTF-8。
2. Job 指派失敗時，不可執行目標命令。
3. 取消命令後，孫程序確實結束。
4. 管理程序被強制終止後，Job 內的子程序確實結束。

在其他 Windows 主機重跑時，請記錄 Windows 版本、doctor 輸出與完整 pytest 摘要。測試不需要使用真實 repo 或正式憑證，會建立自己的暫存資料。

## 在 WSL2 驗收

PowerShell 先執行 `wsl --list --verbose`，確認目標 Ubuntu 的 VERSION 為 2。進入該 Ubuntu，將本專案放在 Linux home 目錄後執行：

```bash
uv sync --locked --python 3.12
uv run webcodex doctor
cargo --version
WEBCODEX_TEST_SKILL_COMMIT=41bbe19d1a1a7eaab5e7bb9050a417e5c6cffc8f uv run pytest -q -ra
```

doctor 應顯示 `platform: linux` 與 `wsl: true`。具備 Cargo 及網路時，預期為 15 passed、4 skipped；這 4 項是 Windows 原生測試，不適用於 WSL2。接著在 WSL2 啟動 Server／Runner，照操作教學完成一次命令核准與測試結果讀取。

## CI 與驗收界線

[GitHub Actions 設定](../.github/workflows/test.yml) 在 push、pull request 或手動觸發時跑四個組合。CI 會驗證必要工具存在，並啟用固定 commit 安裝測試；不需要正式 Server 憑證。

目前沒有設定 Git remote，也尚未 push 或觸發雲端 CI，因此工作流程檔存在不代表 CI 已通過。本次 Windows 原生證據來自提供的 Windows 11 主機；Windows Server 2022、其他桌面版本及組織政策仍需各自驗證。
