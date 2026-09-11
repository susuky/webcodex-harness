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

支援 HTTP MCP 的 `url`／`headers`；環境參數只展開 `${PLUGIN_ROOT}`、`${CODEX_PLUGIN_ROOT}`、`${CLAUDE_PLUGIN_ROOT}`、`${PROJECT_ROOT}` 和操作員明確配置的 Runner `environment`。不會自動把整份父程序環境變數交給 provider。

## 執行與延續

`plugins_list` 只讀取安裝 metadata。`plugin_tools`、`plugin_resources` 與 `plugin_prompts` 需要啟動或連線 provider，因此也需要 `execute` 權限。

每個 task／plugin／provider 保留自己的 MCP session，連續工具呼叫能沿用瀏覽器或其他 provider 狀態。內建上限為 16 個 active provider sessions；可用 `plugin_disconnect` 關閉。Runner 重啟後需要重新連線，不宣稱能還原 provider 記憶體。

`plugin_call` 保留 MCP 原生文字、圖片、resource content、structuredContent 和 isError。對方工具回報的錯誤不會包裝成虛假的成功結果；逾時或斷線時不自動重送可能有副作用的呼叫。

## 完整範例

`examples/plugins/project-tools` 提供可安裝的 plugin、skill、`project_stats` 工具、README resource 與摘要 prompt。統計值取自 Runner 的實際工作目錄，`observations` 可驗證連續呼叫是否使用同一個 provider session。

本專案不內建外掛市集帳號、第三方 OAuth、hooks、apps UI 或 provider 的依賴安裝器；安裝成功代表 bundle 已被驗證並保存，是否能執行仍由實際工具探索／呼叫驗證。
