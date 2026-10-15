# 平台支援與驗證

更新日期：2026-09-11。以下區分這次變更的驗證與既有版本的歷史證據。

## 本次驗證

| 項目 | 實際結果 |
| --- | --- |
| Ubuntu 24.04／Python 3.12.12，含 Xvfb 桌面的完整套件 | 20 passed、7 skipped，67.53 秒 |
| Ruff／格式檢查、wheel／sdist 建置 | 通過 |
| Windows 互動桌面 | 本次未驗收；已實作後端及可在該平台執行的測試 |
| GitHub Actions | 已更新，尚未執行；Ubuntu／Windows、Python 3.11／3.12 矩陣 |

完整套件的 7 個 skipped 包含 4 個既有 Windows 原生測試、1 個 Windows INPUT ABI 測試、未安裝 Cargo，以及未啟用固定 commit 的 HTTPS 安裝測試。不同執行環境請記錄實際 pytest 輸出。

Xvfb 測試透過真正的 TCP MCP／Runner 操作獨立 Tk 視窗，驗證原生 JPEG、繁體中文輸入、快捷鍵、點擊、移動、捲動與拖曳。其他測試涵蓋桌面權限、核准、task 綁定、原始座標、截圖失效、重試去重、未知結果、輸入清理與取消時的序列化。

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
