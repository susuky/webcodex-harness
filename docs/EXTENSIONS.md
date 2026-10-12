# Plugins 與 Skills

## 安裝與來源

安裝操作只寫入 Runner state 下的 `extensions/<project_id>/`。來源可為目前專案內的相對目錄，或 HTTPS Git URL 加上完整的 40 字元 commit SHA。Git URL 不接受內嵌帳密、query 或 fragment；branch／tag 不可取代固定 commit。

```json
{
  "task_id": "TASK_ID",
  "kind": "skill",
  "source": "https://github.com/OWNER/REPO.git",
  "ref": "FULL_40_CHARACTER_COMMIT_SHA",
  "subpath": "skills/SKILL_DIRECTORY",
  "request_id": "install-skill-1"
}
```

把上述參數傳給 `extension_install`。Git checkout 後再複製所選 bundle；複製範圍最多 2,000 個檔案／目錄、20 MiB 一般檔案，不接受 symlink、hardlink 或特殊檔案。安裝不執行 hooks、npm install 或 skill scripts。來源與 commit 保存於安裝紀錄中；這些上限限制安裝 bundle，不代表整份 Git 下載也只有 20 MiB。

本地來源與專案技能讀取沿用任務綁定的 Workspace。專案或已選來源目錄的身份改變時會停止，不會重新接受替換後的目錄；Git 中繼資料（含 worktree 的 `.git` 檔案）不會複製到 bundle。

同名套件不可靜默覆寫。更新時先檢查目前來源與版本，再明確使用 `extension_remove` 移除及重新安裝；沒有自動更新程序。

## Skill 格式

每個 skill 目錄需包含具有 `name` 和 `description` YAML frontmatter 的 `SKILL.md`。可附帶 `scripts/`、`references/` 或其他資源。本專案先公開名稱與用途，再透過 `skill_read` 載入全文，符合 agent skills 的漸進載入方式。[OpenAI skills 文件](https://learn.chatgpt.com/docs/build-skills)

探索來源依序為：

1. 專案 `.agents/skills`、`.codex/skills`、`.claude/skills`，名稱為 `project:名稱`。
2. 透過本工具安裝的 skill，名稱為 `installed:名稱`。
3. 已安裝 plugin 的 skill，名稱為 `plugin名稱:skill名稱`。

同名專案 skill 採上述目錄順序；全文不自動全部放入對話。使用 `skill_read(name, path)` 可讀取 bundle 內的相對參考檔案，不能跳出該目錄。`skill_run` 明確選擇 Python、Bash 或 Node 腳本，在目前 repo 執行並回傳 process ID；需要 `execute` 權限。

## Plugin 格式

最小套件結構：

```text
my-plugin/
├── .codex-plugin/plugin.json
├── .mcp.json
├── skills/
│   └── my-skill/SKILL.md
└── server.py
```

支援 `.codex-plugin/plugin.json` 和 `.claude-plugin/plugin.json` 的 `name`、`description`、`version`、`skills` 與 `mcpServers`。`skills` 可為相對路徑或路徑清單；`mcpServers` 可為設定物件或 JSON 檔案相對路徑。它是這些格式的可執行子集，不宣稱完整支援 Codex／Claude Code 的所有 manifest 功能。

```json
{
  "name": "my-plugin",
  "version": "0.1.0",
  "description": "這個外掛提供的用途",
  "skills": "./skills/",
  "mcpServers": "./.mcp.json"
}
```

`.mcp.json` 的 stdio 範例：

```json
{
  "mcpServers": {
    "project": {
      "command": "${WEBCODEX_PYTHON}",
      "args": ["${PLUGIN_ROOT}/server.py"]
    }
  }
}
```

初始化會將當前 Python 執行檔放入 Runner 的 `environment.WEBCODEX_PYTHON`。若 Runner 搬到另一台機器，需改為那台機器的實際 Python 路徑。其他 provider 的程式及依賴也必須在 Runner 上存在。

同一 bundle 的 Markdown skill 可跨平台使用；腳本及 provider 的執行檔仍要符合 Runner 平台。Windows Python 路徑通常以 `.exe` 結尾，Bash 腳本需要另外存在的 Bash；WSL2 的工具與路徑屬於 Linux 環境。不要直接共用 Windows 和 WSL2 的 `.venv`、Runner state 或絕對路徑設定。設定檔與 skill 請使用 UTF-8。

支援 HTTP MCP 的 `url`／`headers`；環境參數只展開 `${PLUGIN_ROOT}`、`${CODEX_PLUGIN_ROOT}`、`${CLAUDE_PLUGIN_ROOT}`、`${PROJECT_ROOT}`、`${TASK_STATE_DIR}` 和操作員明確配置的 Runner `environment`。不會自動把整份父程序環境變數交給 provider。

`${TASK_STATE_DIR}` 是 Runner 管理的保留變數，不可由 `environment` 覆寫。位置為 `<state_dir>/extensions/<project_id>/sessions/<plugin>/<server>/<task_id>/`，建立時沿用私有目錄保護。不同專案、task、plugin 與 server 使用不同目錄；重連同一組識別碼會取得相同路徑。Provider 必須明確使用此路徑保存狀態，它不會自動隔離遠端服務或共用瀏覽器。斷線與移除 plugin 不會刪除這些目錄，目前沒有自動清理期限。

每個 MCP server 可設定 `allowedTools` 字串清單。省略時維持原有行為、允許所有 provider 工具；空清單拒絕全部工具。設定後，`plugin_tools` 只列出清單內的工具，`plugin_call` 在送往 provider 前再檢查一次。清單最多 256 個名稱，每個名稱為 1–128 字元；只接受精確名稱，不支援萬用字元。已安裝設定的修改會在下一次取得該 provider 時套用到既有連線；其他啟動參數的變更則需先斷線。

這是 Harness 的呼叫限制，不是 provider 程序的 sandbox；也不限制 resources／prompts。安裝與執行外掛仍代表信任該程式，所有 provider 工具仍沿用 `execute` 權限。

## 執行與延續

`plugins_list` 只讀取安裝 metadata。`plugin_tools`、`plugin_resources` 與 `plugin_prompts` 需要啟動或連線 provider，因此也需要 `execute` 權限。

每個 task／plugin／provider 保留自己的 MCP session，連續工具呼叫能沿用瀏覽器或其他 provider 狀態。內建上限為 16 個 active provider sessions；可用 `plugin_disconnect` 關閉。Runner 重啟後需要重新連線，不宣稱能還原 provider 記憶體。

`plugin_call` 保留 MCP 原生文字、圖片、resource content、structuredContent 和 isError。對方工具回報的錯誤不會包裝成虛假的成功結果；逾時或斷線時不自動重送可能有副作用的呼叫。若呼叫已完成但回傳超過 2 MiB，job 標記為 `unknown`；先檢查實際效果，再決定是否發出新操作，不能以回傳過大推斷尚未執行。

## 完整範例

`examples/plugins/project-tools` 提供可安裝的 plugin、skill、`project_stats` 工具、README resource 與摘要 prompt。統計值取自 Runner 的實際工作目錄，`observations` 可驗證連續呼叫是否使用同一個 provider session。

`examples/plugins/browser-tools` 提供固定版本 Playwright MCP 的啟動設定、13 個允許工具、task 專屬輸出目錄與獨立瀏覽器狀態。安裝與操作見 [瀏覽器操作指南](BROWSER_USE.md)。

本專案不內建外掛市集帳號、第三方 OAuth、hooks、apps UI 或 provider 的依賴安裝器；安裝成功代表 bundle 已被驗證並保存，是否能執行仍由實際工具探索／呼叫驗證。
