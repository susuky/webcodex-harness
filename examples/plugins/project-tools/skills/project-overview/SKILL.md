---
name: project-overview
description: 當使用者需要理解目前專案的檔案分布、主要語言或程式結構時使用；不適用於修改程式或部署。
---

# 專案概覽

1. 使用目前任務的 `workspace_info` 與 `read_instructions` 讀取專案狀態及規則。
2. 透過 `plugin_tools` 探索 `project-tools` 外掛的 `project` provider。
3. 呼叫 `project_stats` 取得真實的檔案統計，再以 `list_files` 與 `read_file` 核對主要入口。
4. 根據實際檔案簡短說明專案結構；不要把檔案數量解釋為程式品質，也不要推測未驗證的功能。
