# 平台支援與驗證

以下整理各作業系統目前支援的功能範圍、限制與測試重現方式。

## 平台支援矩陣

| 平台 | Coding／Git／命令 | 桌面操作（Computer Use） | 瀏覽器（Playwright plugin） | 備註 |
| --- | --- | --- | --- | --- |
| Ubuntu Linux（X11） | 完整支援（PTY、directory fd、`O_NOFOLLOW`） | 支援 X11 桌面與 AT-SPI 語意控制 | 支援（headless Chrome） | Wayland 不支援桌面輸入 |
| Windows 原生 | 完整支援（ConPTY、Job Object、file handle pin） | 支援多螢幕、負座標、混合 DPI 與跨螢幕拖曳；支援 UI Automation | 支援（headless Chrome） | 需在已登入解鎖之互動桌面執行 Runner；見 [Windows 安裝指南](WINDOWS_RUNNER.md) |
| Windows WSL2 | 完整支援（Linux home + bash/PTY） | 不支援桌面輸入 | 可於 Ubuntu 環境執行 | 與 Windows 原生分開維護 `.venv` 與 state |
| macOS | 基本命令與檔案支援；結構化 Git 與 `apply_patch` 不可用 | 實驗性支援主螢幕 Quartz（Retina 邏輯尺寸 + Accessibility） | — | 不支援語意桌面自動化 |

## 已知邊界

- Wayland、WSLg 與鎖定螢幕皆不支援桌面輸入。
- 同一 Runner 共用桌面輸入鎖。`snapshot_id` 限一次輸入，新截圖、螢幕排列或 DPI 變更、Runner 重啟或五分鐘期限到期都會讓舊 ID 失效。
- 已移除外部 Codex／Work worker 啟動路徑；子代理由 MCP 用戶端負責，詳見 [子代理指南](SUBAGENTS.md)。

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

分支 push 時，Ubuntu 和 Windows 的 Python 3.11 跑完整 pytest，Python 3.12 跑工作區邊界測試。Pull request、tag 和手動觸發在兩個 Python 版本都跑完整 pytest。四組都執行套件安裝及 CLI、Server、Runner 操作驗證。

ChatGPT 帳號、tunnel、公開 TLS 與外部 provider 的連線，需依實際目標環境驗證。
