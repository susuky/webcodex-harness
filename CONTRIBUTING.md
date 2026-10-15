# 參與開發

歡迎問題回報與改善建議。WebCodex Harness 目前仍是開發預覽，支援功能見 [README](README.zh-TW.md)。

權利範圍以 README 最下方聲明為準。下列安裝、修改與 PR 流程適用於已取得所需書面許可的協作者；本指南不另外授予使用、複製、修改或散布權利。

## 開發環境

需要 Python 3.11+、uv、Git 與 ripgrep。請以獨立測試 repo 和臨時憑證驗證，不使用正式工作環境。

```sh
uv sync --locked --extra computer
uv run webcodex --version
uv run pytest -q -ra
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv build
```

一般測試不操作桌面。改動桌面後端時，依 [Computer use](docs/COMPUTER_USE.md#重現桌面測試) 使用獨立桌面驗證；Windows API 需在 Windows 執行。Rust 與固定 commit 的安裝測試，啟用方式見 [平台驗證](docs/PLATFORMS.md)。

## 問題回報

請提供版本、作業系統、Python 版本、最小重現步驟、預期／實際結果，以及刪除敏感資訊後的錯誤。`webcodex --version` 和 `webcodex doctor` 可協助定位環境，但請先遮蔽私人路徑。

不要提交真實設定 JSON、token、SQLite 工作紀錄或未檢查的桌面截圖。涉及漏洞或資料暴露時，依 [安全回報指南](.github/SECURITY.md) 使用私密管道。

## Pull Request

- 一個 PR 處理一個明確問題；保留無關變更，優先沿用現有程式與依賴。
- 說明觸發條件、變更後行為、實際執行的驗證及略過項目。邏輯或權限變更應有可重現檢查。
- 保留用戶端／Runner 雙重權限檢查、操作員核准、路徑與 hash 邊界及不確定結果處理。
- 安裝方式、功能或限制改變時，同步更新英文與繁體中文 README。其餘 Markdown 預設使用繁體中文。
- 新增依賴需說明用途與授權；不要把下載的程式、模型、字型或測試資料當成本專案自有內容。

提交前執行 `git diff --check` 並檢查實際 diff。Commit 標題使用 Conventional Commits，例如 `fix(runner): preserve output after reconnect`。

## 版本與發布

版本來源為 `src/webcodex/__init__.py` 的 `__version__`，套件 metadata、CLI、MCP 與 health endpoint 共用此版本。修改版本後執行 `uv lock`，並依 [發布指南](docs/RELEASING.md) 檢查建置成品。
