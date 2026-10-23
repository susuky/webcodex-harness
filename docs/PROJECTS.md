# 建立與登記專案

`project_create` 可以在沒有任何既有專案或 coding task 時，於 Runner 允許的父目錄建立新資料夾並登記專案。`project_register` 則登記父目錄內已存在的資料夾，不修改其內容。兩者完成後，使用回傳的 project ID 建立一般任務。

## 第一次設定

操作員先建立專用父目錄，再初始化設定；此目錄與設定、Runner state 必須分開：

```bash
mkdir -p "$HOME/projects"
uv run webcodex init --project-root "$HOME/projects"
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Path 'C:\projects' -Force
uv run webcodex init --project-root 'C:\projects'
```

可同時提供 `--project /absolute/path/existing-repo`，保留一個既有專案。只指定 `--project-root` 時，初始專案清單為空，但父目錄 `local/projects` 已可供建立專案。名稱與路徑可在設定中調整。

`init --project-root` 會在 client 與 Runner 加入 `projects` 權限，已授權的建立與登記請求會直接執行。僅使用 `--project` 初始化時，不會取得父目錄或建立專案權限。

## 在既有安裝啟用

既有安裝不必重跑 `init` 或手動修改兩份 JSON。停止 Server 與 Runner 後，以本機操作員身分執行：

```bash
uv run webcodex allow-root /absolute/path/to/projects
```

這會保留憑證、既有專案與其他用戶端設定，備份兩份原始設定，授予預設 `owner` 用戶端在該父目錄建立／登記專案的權限。可用 `--client`、`--name` 與 `--config-dir` 指定其他用戶端、父目錄 ID 與設定位置。日常開發沿用用戶端與 Runner 的工具權限。

重新啟動 Server 與 Runner 後，`project_roots_list` 應顯示父目錄及 `online: true`。其中 `can_create` 表示用戶端是否有專案管理權限；相容欄位 `approval_mode` 與 `new_project_approval_mode` 固定為 `automatic`。Runner 仍會獨立檢查工具權限。ChatGPT 更新連線工具清單後即可使用新增工具，無須為每個專案重建連線。

父目錄授權涵蓋其中**已登記或明確設定**的專案，不會自動開放所有子資料夾。共用同一父目錄授權的用戶端會看到這些專案；要隔離可見範圍，請配置不同父目錄。原有明確列於 `clients.CLIENT_ID.projects` 的專案授權保持有效。

## 建立新專案

先呼叫 `project_roots_list`，使用實際回傳的 root ID。以下 `args.json` 可搭配 `uv run webcodex call TOOL --arguments '@args.json'`，也可由 MCP 用戶端直接傳入：

```json
{"root":"local/projects","name":"my-app","git_init":true,"request_id":"create-my-app-1"}
```

呼叫 `project_create`。`name` 同時是 Runner 內的 project ID 與直接子目錄名稱，須為 1–64 個英文字母、數字、點、底線或連字號，且以字母或數字起始。`git_init` 預設為 `false`；明確設為 `true` 才初始化 `main` 分支，不建立 commit。

使用 `job_get` 等待同一工作：

```json
{"job_id":"JOB_ID","wait_seconds":10}
```

只有工作為 `succeeded` 且 `result.registered: true`，才使用 `result.project`。直接依回傳的 `next_action` 呼叫 `work_on_project`，便會建立 coding task 並讀取工作目錄；也可自行呼叫：

```json
{"project":"local/my-app","title":"建立應用程式","request_id":"start-my-app-1"}
```

`work_on_project` 回傳 `task.id` 與 `workspace`。後續讀寫、Git 或命令工具使用這個 task ID；工作目錄仍在讀取時依 `workspace.next_action` 等待。相同 `request_id` 重試不會建立重複任務。建立專案時回傳的 `task_id` 是供工作紀錄使用的管理紀錄，不能拿來執行 coding 工具，也不需要將它標為完成。

## 登記已存在的資料夾

呼叫 `project_register`：

```json
{"root":"local/projects","name":"existing-app","path":"team/existing-app","request_id":"register-existing-app-1"}
```

`path` 使用相對於父目錄的 POSIX 路徑；Windows 也使用 `/`。專案名稱與資料夾名稱可以不同。登記不初始化 Git，也不移動、清空或改寫現有檔案。

兩種操作都拒絕重複 project ID；同一資料夾不能以另一個 ID 重複登記。`project_create` 拒絕任何已存在的目的地，即使資料夾是空的；需要接用現有資料夾時，明確改用 `project_register`。絕對路徑、`..`、symlink／junction、私有 metadata 與登記父目錄本身均被拒絕。

## 重試、保存與限制

- `request_id` 必填。相同 client、父目錄身分、參數與 ID 會取得原工作；相同 ID 配上不同參數會被拒絕。等待或斷線後繼續查原 `job_id`，不要換 ID 重做。
- Runner 先將登記寫入私有 `deliveries.sqlite3` 的 `managed_projects`，再發布專案清單；Server 收到專案清單後才將建立工作回報成功。新增專案不會逐次改寫設定 JSON。
- Runner 重啟會恢復已登記專案；Server SQLite 會保留工作紀錄，舊資料庫會自動新增所需欄位。目錄、父目錄或其檔案系統身分已改變時，不把原任務接到新位置；登記紀錄保留，需操作員檢查。無法讀取的專案或父目錄會略過，不會讓其他專案停止回報心跳。
- 建立資料夾後若 Git 初始化、登記或程序中斷造成結果不確定，工作標示 `unknown`；不自動刪除資料夾或重跑。操作員檢查 `projects_list` 與目的地後，再決定是否登記已存在的資料夾。
- 每台 Runner 最多公布 100 個專案、100 個父目錄。尚未提供取消登記、搬移、刪除專案或模板套用工具。
- 父目錄清單限制這些結構化專案工具；有命令或 plugin 權限的 client 仍使用 Runner 帳號權限。

Ubuntu 已驗證：零專案建立、核准、Git 初始化、既有資料登記、路徑拒絕、並行建立、回報中斷重試、Runner 重啟恢復及舊 Server 資料庫 migration。Windows 新增專案流程仍待實機驗收。

## 上游參考

比對 [yyjeqhc/webcodex @ c9f1a54](https://github.com/yyjeqhc/webcodex/tree/c9f1a54fc9e9e3fd21d5005a140b9e97998134db) 的 [Runner 專案操作](https://github.com/yyjeqhc/webcodex/blob/c9f1a54fc9e9e3fd21d5005a140b9e97998134db/crates/webcodex-runner/src/webcodex_runner/projects.rs) 與 [工具入口](https://github.com/yyjeqhc/webcodex/blob/c9f1a54fc9e9e3fd21d5005a140b9e97998134db/src/tool_runtime/projects.rs)，採用「不依賴既有 task、Runner 驗證父目錄、持久登記後公布」的流程，重用既有 Python 工作佇列與權限檢查。
