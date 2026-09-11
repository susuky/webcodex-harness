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
