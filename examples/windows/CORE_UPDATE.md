# 套用 Windows 核心更新

目前這份更新放在安裝目錄的 `updates\20261111-reliability`，包含 `apply-core-update.ps1` 與已測試的完整 `runtime` 套件（28 個檔案）。

在「以系統管理員身分執行」的 PowerShell 執行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<安裝目錄>\updates\20261111-reliability\apply-core-update.ps1"
```

要先檢查目前的工作，在同一命令最後加 `-CheckOnly`；這個模式可在一般 PowerShell 執行。

腳本會列出 `runner`、`course-hub-runner` 仍在執行的 process ID／operation ID。有工作時以代碼 2 結束，等這些工作結束後重跑。這次檢查時，課程網站 8795 還在執行；51510 預覽已到期正常結束。更新當下以腳本列出的即時狀態為準。

兩個 Runner 都空閒後，腳本會取得現有啟動鎖、停止 Server 接收新工作，再檢查一次；接著停止這份安裝的 Runner 與 task-center SSH helper，更新既有 editable 套件，最後使用原本的 `WebCodex Windows Keepalive` Limited 排程重新啟動。第二次檢查或複製發生錯誤，也會釋放啟動鎖並啟動既有排程。完成時會確認兩個 Runner 上線、Server／task center 回應，以及 8790 使用 Git SSH。

套用需要管理員，因為目前 Server 與 task-center helper 位於 Session 0，一般使用者權限無法停止它們。腳本沿用排程的使用者與 Limited 權限；管理員只負責這次更新。`-ExecutionPolicy Bypass` 僅用於這次 PowerShell 程序。

## 其他安裝位置

從 repo 的 example 直接使用時，請傳入 `-InstallRoot`，並將已測試的完整 `src/webcodex` 檔案放在腳本旁的 `runtime` 目錄。這個腳本對應目前使用 18765／8790 與既有 Keepalive 排程的 Windows 安裝。
