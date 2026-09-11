# 連接 ChatGPT 與其他 MCP 用戶端

## 先確認本機可用

啟動 Server 與 Runner，執行 `webcodex call projects_list` 確認專案出現，並完成一個 `read_file` 或測試命令。Ubuntu、Windows 原生與 WSL2 的完整命令見 [操作教學](OPERATIONS.md)。

一般用戶端可直接使用 `/mcp` 與 `client.json` 的 Bearer token，也可以啟動 `webcodex bridge --config /absolute/path/client.json`。bridge 對外是 stdio MCP，並在本機讀取 token；它不會把 token 放進工具參數、URL 或 stdout。

例如支援 stdio 的 MCP 用戶端可設定：

```json
{
  "mcpServers": {
    "webcodex": {
      "command": "/absolute/path/webcodex/.venv/bin/webcodex",
      "args": ["bridge", "--config", "/absolute/path/client.json"]
    }
  }
}
```

Windows 原生用戶端的範例，請把帳號與路徑換成實際值：

```json
{
  "mcpServers": {
    "webcodex": {
      "command": "C:\\src\\webcodex\\.venv\\Scripts\\webcodex.exe",
      "args": ["bridge", "--config", "C:\\Users\\YOUR_USER\\AppData\\Local\\webcodex-harness\\client.json"]
    }
  }
}
```

Windows 用戶端要連到 WSL2 裡的 Harness，可以讓它啟動 `wsl.exe`：

```json
{
  "mcpServers": {
    "webcodex": {
      "command": "wsl.exe",
      "args": ["--distribution", "Ubuntu", "--exec", "/home/YOUR_USER/src/webcodex/.venv/bin/webcodex", "bridge", "--config", "/home/YOUR_USER/.config/webcodex-harness/client.json"]
    }
  }
}
```

WSL2 範例要求 Server、Runner 與 client 設定都在相同 Ubuntu 發行版內。這些是供支援 stdio 的本機 MCP 用戶端使用的設定，不能直接貼進 ChatGPT 的遠端 URL 欄位。Windows 原生及 WSL2 內的 stdio bridge 均已通過實機測試；Windows 用戶端透過 `wsl.exe` 啟動的組合，以及實際 ChatGPT 帳號連線，仍需在選定的用戶端配置後驗證。

## ChatGPT：Secure MCP Tunnel

OpenAI 官方文件提供從私有網路主動連線的 Secure MCP Tunnel，支援 stdio 與 HTTP MCP。它需要你帳號下的 tunnel ID、執行用 API key，以及對應工作區權限。[官方設定指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

1. 在 Platform 的 tunnel 設定建立或選擇 tunnel，確認已關聯要使用的 ChatGPT workspace。
2. 依官方指南安裝 `tunnel-client`，在本機安全設定 `CONTROL_PLANE_API_KEY`。不要把值貼入對話、repo 或命令輸出。
3. 使用本專案已驗證的 stdio bridge 作為 tunnel 的本機 MCP command：

```bash
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile webcodex \
  --tunnel-id YOUR_TUNNEL_ID \
  --mcp-command "/absolute/path/webcodex/.venv/bin/webcodex bridge --config /absolute/path/client.json"

tunnel-client doctor --profile webcodex --explain
tunnel-client run --profile webcodex
```

4. 在 ChatGPT 啟用 developer mode，新增 MCP 連線，選擇 Tunnel 與對應 tunnel。保留本機 Server、Runner 和 tunnel-client 運作。
5. 確認探索到的工具清單後，開啟對話並加入這個連線；先試 `projects_list`，再建立任務。介面可用性由帳號及工作區政策決定。[ChatGPT 官方連線步驟](https://developers.openai.com/plugins/deploy/connect-chatgpt)

Server 的 Bearer 驗證仍由 bridge 處理，不必為了接入而關閉本機驗證。本專案沒有替你建立 tunnel、使用帳號 key 或宣稱已完成 ChatGPT 端驗收。

## 公開 HTTPS 入口

若你已有公開網域與反向代理，可將 HTTPS `/mcp` 轉發至 Server，並將 `server.json` 的 `public_url`、`allowed_hosts` 設為實際入口。其他機器的 Runner 必須使用 HTTPS；憑證驗證保持開啟。

本專案目前實作靜態 Bearer 驗證，**沒有 OAuth authorization server**。對支援 Bearer 的 MCP 用戶端可直接使用；對要求 OAuth 的 ChatGPT 連線或公開 plugin，需加上合規的 OAuth 入口，不能假設填入 Bearer token 就能使用所有 ChatGPT 表面。OpenAI 的公開 plugin 驗證契約包含 OAuth 2.1、資源探索與 PKCE。[官方驗證文件](https://developers.openai.com/plugins/build/auth)

如果只需要自己的 ChatGPT 操作本機 repo，前述 stdio bridge／Secure MCP Tunnel 是已準備好的私有接入路徑。

## 建議的第一個請求

> 請列出 WebCodex 專案，選擇我指定的專案建立任務。先讀取專案規則與 Git 狀態，執行相關測試；需要修改時保留其他變更，最後顯示 diff 與真實測試結果。把任務 ID、摘要與下一步保存，讓下一次對話可以延續。

下次對話先使用 `task_list` 和 `task_get` 讀取同一個任務。Server 能保存工作摘要與證據，不會存取或還原 ChatGPT 的私有推理與完整對話上下文。

## 工具看得到，但無法執行

工具註冊、目前對話的執行權限、Runner 狀態是三個不同的檢查。看得到 schema 只代表工具 metadata 可取得，不能據此判定當前對話能呼叫工具。`projects_list` 在 Server 讀取最近的 Runner 心跳，不會建立 job，也不會直接執行 repo 操作；`online` 表示 15 秒內有心跳，不保證後續每個工具都成功。

| 觀察到的證據 | 可判定的狀態 | 下一步 |
| --- | --- | --- |
| 只有工具名稱／schema | 工具已註冊；執行與 Runner 狀態未確認 | 呼叫一次 `projects_list` |
| `FORBIDDEN: This conversation does not support developer MCPs` | 依錯誤訊息判讀為用戶端拒絕此對話執行；本次無法取得 Runner 狀態 | 停止重試相同呼叫，檢查下方對話與權限設定 |
| `projects_list` 成功且 `online: true` | 已取得 Server 回覆，該 Runner 心跳有效 | 使用回傳的 project ID 建立／延續任務 |
| `projects_list` 成功且 `online: false` | 已取得 Server 回覆，該 Runner 心跳過期 | 檢查 Runner 及其至 Server 的連線 |
| `projects_list` 成功但清單為空 | 此 client 看不到已註冊專案 | 比對 client 的專案授權與 Runner 註冊設定；不能直接標成離線 |
| Server 回覆 `401 Unauthorized` | Bearer 驗證失敗，Runner 狀態未確認 | 檢查所選 client 設定與憑證種類 |
| Server 回覆 `403 Origin is not allowed` | Server 拒絕請求的 Origin，Runner 狀態未確認 | 檢查實際入口與 `public_url`；這與 developer MCP 對話限制不同 |
| 逾時、DNS／TLS 或 transport 錯誤 | 連線未完成，Runner 狀態未確認 | 檢查發生錯誤的網路或 tunnel 路徑 |

本專案不產生上述 developer MCP 錯誤字串。若用戶端在請求送出前拒絕執行，Server 無法攔截、改寫回覆或自行解除限制；工具說明只能協助用戶端正確解讀。不要將所有 `FORBIDDEN`／HTTP 403 都歸類為此案例。

### Developer MCP 對話限制的排查

1. 在相同 Server 與 client 設定下執行 `uv run webcodex call projects_list`；使用自訂設定時加上 `--config /absolute/path/client.json`。這能驗證 CLI → Server 的工具呼叫，不能代替 ChatGPT → MCP／tunnel 的驗證。`webcodex doctor` 僅檢查本機環境。
2. 依官方介面確認 Settings → Security and login → Developer mode；其可用性受帳號與工作區政策影響。更新連線 metadata 後使用 Refresh，再開啟新對話並從工具選單加入該連線，重新測試 `projects_list`。[官方連線與更新步驟](https://developers.openai.com/plugins/deploy/connect-chatgpt)
3. 若仍被拒絕，請工作區管理者確認 plugin／MCP server 對該角色可用，以及連線的 action 權限。工具可被看到與具備執行權限是不同層次。[官方權限說明](https://learn.chatgpt.com/docs/enterprise/apps-and-connectors)
4. 保留用戶端介面／模式、時間與完整錯誤文字，供工作區管理者或用戶端支援管道排查。若有反向代理或 tunnel 日誌，可比對請求是否到達；`projects_list` 不建立 job，因此「沒有新 job」不能當成請求未到 Server 的證據。

公開文件未列出這個錯誤字串對應的所有內部條件；不能只憑它指定某個模型、方案或裝置一定不支援。更換對話是排查步驟，不保證能解除帳號或工作區限制。

### 驗收案例

保留「schema 可取得，但呼叫被 host 拒絕」作為用戶端驗收情境。預期回報為「工具已註冊；目前對話不允許執行；Runner 狀態未確認」，停止無效重試，且不宣稱已建立任務、讀取檔案或執行測試。

本機自動測試透過真實 stdio bridge 驗證初始化指引、工具說明，以及相同工具清單下的 Runner 上線、心跳過期與無可見專案結果。ChatGPT 的 host 拒絕需要在出錯的對話環境重測，不能用本機模擬宣稱已修復。
