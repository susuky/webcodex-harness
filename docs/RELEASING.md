# 公開與發布準備

本專案的著作權及第三方授權聲明位於中英文 README 最下方。本專案不附 LICENSE，也未授予一般開源授權；第三方依賴保留各自的授權條款。

## 已具備

- 正式倉庫為 [susuky/webcodex-harness](https://github.com/susuky/webcodex-harness)，套件 metadata 已提供原始碼、文件與 issue 連結。
- GitHub 已啟用 [私密漏洞回報](https://github.com/susuky/webcodex-harness/security/advisories)。
- Ubuntu／Windows coding 後端、選用的桌面工具，以及標明實測範圍的文件。
- 中英文 README、[協作指南](../CONTRIBUTING.md)、問題／PR 範本與[安全回報指南](../.github/SECURITY.md)。
- 以 `src/webcodex/__init__.py` 為單一版本來源的套件、CLI、MCP 與 health endpoint。
- uv lockfile、Ubuntu／Windows CI 設定、建置與基本憑證誤提交防護。

## 首次公開前

| 待完成事項 | 驗收方式 |
| --- | --- |
| 雲端 CI | 推送後檢查實際 Ubuntu／Windows 矩陣結果 |
| Windows 桌面 | 在獨立登入帳號下完成截圖、輸入及 DPI 驗收；目前仍須保留待驗收標記 |
| 發布內容與權利 | 檢查完整 Git 歷史、來源、資料及依賴的散布條件；保留 README 的原文權利聲明 |

目前沒有設定自動發布工作流程。

## 依賴授權

基礎 wheel 的依賴會在安裝時另行取得；若再製作包含依賴的安裝包、容器或其他組合成品，必須另外確認各套件的授權義務。

選用桌面功能的 PyAutoGUI 會帶入 MouseInfo 與 Linux 的 python3-xlib。[PyAutoGUI 依賴定義](https://github.com/asweigart/pyautogui/blob/master/setup.py)；目前 lockfile 對應的 MouseInfo 0.1.3 metadata 標記 GPLv3+，python3-xlib 0.15 的來源檔標示 GPLv2 或更新版本。[MouseInfo 授權](https://github.com/asweigart/mouseinfo/blob/master/LICENSE.txt)、[python3-xlib 套件](https://pypi.org/project/python3-xlib/)

首次提供整合安裝包前，需確認本專案權利聲明與各依賴授權在該散布方式下的適用範圍，或另行評估替換桌面依賴。

## 建置檢查

更新 `__version__` 後重新執行 `uv lock`。於擁有必要權限的開發環境執行：

```sh
uv sync --locked --extra computer
uv run pytest -q -ra
uv run ruff check src tests examples
uv run ruff format --check src tests examples
uv build
uvx twine check dist/*
```

另外在乾淨環境安裝實際 wheel，確認 `webcodex --version`、套件 metadata 與 Server 回報一致，且未安裝桌面依賴也能載入基本服務。版本目錄中不要混入以前的建置成品；只交付確認過的檔案。

目前尚未建立完整秘密掃描流程。對外發布前應檢查工作區、完整 Git 歷史、私人資料與成品內容。

## 值得優先補的功能

先完成 Windows 桌面驗收，再實作工作紀錄／截圖的可控保留期限、指定視窗控制及簡易任務監看。完整取捨見 [上游比對](ROADMAP.md)。
