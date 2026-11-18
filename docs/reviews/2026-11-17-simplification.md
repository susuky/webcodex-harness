# WebCodex 系統精簡與 Windows 斷線調查

日期：2026-11-17（Asia/Taipei）

程式修整與隔離測試已完成。修改位於 `/home/nvidia/ping/webcodex`；Windows 的測試來源位於 `C:\Users\hpisj\webcodex-desktop-test\.state\webcodex-simplify-20261117-candidate`。正式 Server、Runner 與通道保持原程序，仍執行原來載入的版本。新核心待可換版時段載入。

## 斷線原因

Windows 正式連線紀錄保留了 Server 被同步歷史查詢阻塞的證據。`config/server/connections/events.jsonl` 目前保留的 101 筆事件中，55 筆是 `event_loop_blocked`，46 筆是 `runner_heartbeat_expired`；其中 33 筆停頓堆疊涉及 `/api/manage/state`。這是該檔保留的事件範圍。

2026-11-16 18:36–18:44 的事件記錄停頓 500,452.999 ms，呼叫鏈包含 `Store.rows → task_activity → task_progress → task_get`。其他多次 2–3 秒停頓也落在管理清單、目前活動計數與程序歷史分組。這些同步 SQL 佔住 Server 的事件迴圈，心跳、領取工作與完成回報一起延後，造成執行環境被判定離線。

開工時 Windows 的 `config/server/state.sqlite3` 為 55,788,941,312 bytes，約 52 GiB。最近 1,000 筆工作結果的抽樣中，圖片工具約占結果資料量的 96.2%。完整歷史、圖片與仍在執行的工作均保留；新版的清單與計數改讀目前狀態。正式資料庫沒有執行清理、VACUUM、備份或 schema migration。

Runner 另有兩條多餘工作：正式 Windows 舊版在每次工具執行前盤點所有專案；Windows 私有目錄的既有 ACL 也可能被反覆重新套用。這些檔案系統操作會增加工具與心跳延遲。本輪一併改成只核對目標專案、一次完成登記盤點，已符合要求的 ACL 直接沿用。

原生 OpenSSH 9.5.6.4 另有當日崩潰事件，涉及的短命 PID 與本輪保留的服務程序不同。任務中心既有啟動設定已指定 Git 所附的 `ssh.exe`，其通道保持原啟動時間。

## 已移除與改寫

| 原有成本 | 本輪修改 | 主要檔案 |
| --- | --- | --- |
| 讀檔計算整檔雜湊，修改／刪除要求 `expected_sha256`，重複讀取與逐次 fsync | 刪除檔案工具的內容雜湊及前置參數；編輯用原文定位，patch 用 Git context；寫入完成同目錄暫存檔後替換 | `filesystem.py`、`runtime.py` |
| 搜尋前複製整頁檔案到私有快照，另設 20 MiB 上限 | ripgrep 直接搜尋當頁檔案；保留 500 檔分頁、原始相對檔名、行號與輸出游標；Windows 使用原生 `--path-separator=/` | `runtime.py` |
| `git apply --check` 後再執行同一份 patch | 保留路徑與類型檢查，由 Git 一次比對 context 並套用 | `runtime.py` |
| 每次清單／卡片輪詢重新分組完整程序歷史 | 以 `process_states` 保存每個程序目前狀態；接受結果時同一 transaction 更新，清單直接讀取所需 metadata | `store.py`、`server.py`、`management.py`、`management_sources.py` |
| 為未使用欄位讀入完整 job 參數／結果 | 按所需欄位查詢；被選中的程序才讀取對應輸出 | `store.py`、`server.py`、`management.py` |
| 每次工具盤點全專案、登記時重複取目錄身分 | 每次操作只核對該任務專案；登記使用單次專案／父目錄盤點 | `runner.py`、`projects.py` |
| 事件迴圈內掃描 worker policy、skill 與 plugin 檔案 | 沿用原有判斷，檔案操作移至背景執行緒 | `processes.py`、`extensions.py`、`runtime.py` |
| Windows 已正確的 ACL 仍向子目錄重套 | 既有 DACL 已符合設定時直接返回 | `windows.py` |
| 授權父目錄時自動留下兩份設定備份；保留工具用內容雜湊重複比對 | 移除自動設定副本及內容 digest，沿用既有 atomic write 與 SQLite transaction | `config.py`、`maintenance.py` |

開發原則、架構、安裝、操作、圖片匯入與搜尋文件已同步修正。`backup` 保留為明確要求時執行的維護操作；日常工具與授權流程不再把備份當成必經步驟。

## 效能量測

以下比較本輪修改前後的工作目錄版本，兩者都從 repo 的 `src` 載入；固定 200 個程序，在隔離資料庫建立不同數量的工作觀察，取三次量測中位數。

| 歷史觀察筆數 | 活動查詢：修改前 → 後 | 清單查詢：修改前 → 後 |
| ---: | ---: | ---: |
| 1,000 | 0.454 → 0.127 ms | 0.864 → 0.053 ms |
| 10,000 | 1.574 → 0.184 ms | 6.177 → 0.073 ms |
| 100,000 | 21.495 → 0.138 ms | 61.469 → 0.057 ms |

十萬筆 metadata 回填成 200 列目前狀態，隔離測試耗時 23.255 ms。這項測試未包含正式 52 GiB 資料庫的圖片 payload。

另以 8 個專案、每次檔案系統查詢 10 ms 模擬較慢的目錄操作：綁定查詢由 26 次降至 9 次，登記總時間 264.81 → 92.71 ms；5 ms 事件迴圈計時器的延遲由 264.85 → 5.66 ms。

## 測試

| 驗證 | 結果 |
| --- | --- |
| Linux 完整 Python suite | 初輪 580 passed、1 failed、20 skipped，597.81 秒。唯一失敗使用較早載入的卡片查詢；最後修正版原案例通過，整個 continuation policy 11 passed |
| Linux 最後狀態回歸 | legacy 卡片與離線任務優先序 7 passed；legacy terminal 與相關組 10 passed |
| Windows 第一組 | 152 passed、17 skipped；一個 search 路徑格式案例修正後於第二組通過 |
| Windows 第二組 | search 重驗、store/history/migration、management/sources、card/handoff/liveness、connections/diagnostics 共 264 passed |
| Windows continuation policy | 11 passed |
| 管理頁瀏覽器 | 53 項流程通過，包含桌面／手機、明暗主題、捲動、焦點、輪詢、離線恢復與連線紀錄；沒有 page errors |
| 任務卡片 Node 測試 | 42 passed |
| Ruff 與差異檢查 | `ruff check`、`ruff format --check`、`git diff --check` 通過 |

Windows 測試使用獨立候選來源與測試暫存 state；正式 harness 的程式檔與資料庫沒有替換。未啟用真實桌面操作、外網安裝與 distribution 的 opt-in 測試。

## 正式服務核對

2026-11-17 15:13（Asia/Taipei）核對如下，PID 與啟動時間均與開工時一致。兩端各 11 個可見專案全部在線。

| 環境 | 保持原身分的程序 |
| --- | --- |
| ll1 | Server 3426590、任務中心 3844911、Runner 1477781、通道 1477813、既有 SSH watchdog 971522 |
| Windows | Server 26748／launcher 58140；local Runner 26104／launcher 69356；course-hub Runner 14172／launcher 42748；通道 32580 |

ll1 正式 Server／任務中心仍從 `releases/20261116-recurrence` 載入，Runner 仍從 `releases/20261111-connection-reliability` 載入；本輪原始碼在 `/home/nvidia/ping/webcodex`。本次沒有重啟、重載或向正式服務發出訊號。

## 換版與回復

新版首次啟動會從既有工作 metadata 建立 `process_states`，合入 legacy `agent_observations` 後刪除舊表。請在可換版時段依既有服務方式切換，對同一個 state 一次只保留一個 Server；Runner 在其工作結束後再換版。此次沒有建立之後自動部署或重啟的排程。

若新版已使用過而需回到舊版，先停止新版 Server，在啟動舊版前還原 legacy 狀態表：

```sql
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS agent_observations (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    agent_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL,
    exit_code INTEGER,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY(task_id, agent_id)
);
INSERT OR REPLACE INTO agent_observations
    (task_id, agent_id, job_id, status, exit_code, created, updated)
SELECT task_id, process_id, job_id, status, exit_code, started_at, observed_at
FROM process_states WHERE tool = 'subagent_get';
COMMIT;
```

舊版再次寫入工作後，要回到新版時，停止舊版 Server，保留 `agent_observations`，移除過時的目前狀態表，再啟動新版讓既有 migration 回填：

```sql
BEGIN IMMEDIATE;
DROP TABLE process_states;
COMMIT;
```

以上轉換保留歷史 `jobs`。若先前已經交錯使用版本而未還原 legacy 表，需先合併兩表中的 legacy 狀態，再重建目前狀態。這些 SQL 只作換版說明，本輪未對正式資料庫執行。

## 調查與測試定位

- ll1 工作：`238b83c730ea4371aa2e4e87e910242d`。
- Windows 工作：`79eadaca222f40389c006e5490b4ddd5`。
- Windows 連線證據：`C:\Users\hpisj\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\webcodex-windows\config\server\connections\events.jsonl`。
- 管理頁測試結果與畫面：repo 的 `.state/simplification-ui-test.log`、`.state/simplification-ui/`。
- Linux 完整測試程序：`88d01f78692943e780bd38f390bec4b4`；最後 continuation policy：`b1a6431f97ef48169cb1572f8121baf2`。
- Windows 測試程序：`3b03175aab7046deb16079a86efbfaf8`、`44e3f20cb7c849d09b83afea44333e19`、`9555ce542aae460eb59cfef99848a726`。

本輪保留開工時已有的工作目錄變更，未 commit／push。
