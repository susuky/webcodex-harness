$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot 'harness\.venv\Scripts\python.exe'
$taskTunnel = Join-Path $taskRoot 'tunnel\tunnel-client.exe'
$taskSessionRunner = Join-Path $PSScriptRoot 'session_runner.py'
$taskBackground = [Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0
$taskInteractiveAvailable = $false
if ($taskBackground) {
    & $taskPython $taskSessionRunner --interactive-available
    if ($LASTEXITCODE -notin @(0, 1)) { throw 'Could not check the Windows desktop session.' }
    $taskInteractiveAvailable = $LASTEXITCODE -eq 0
}
$taskState = Join-Path $PSScriptRoot 'windows-startup.json'
$taskMutexSecurity = [Security.AccessControl.MutexSecurity]::new()
$taskMutexSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'), [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
foreach ($taskMutexSid in $taskMutexSids) {
    $taskMutexSecurity.AddAccessRule([Security.AccessControl.MutexAccessRule]::new($taskMutexSid, [Security.AccessControl.MutexRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow))
}
$taskMutexCreated = $false
$taskMutex = [System.Threading.Mutex]::new($false, 'Global\WebCodexWindowsStartup', [ref]$taskMutexCreated, $taskMutexSecurity)
$taskOwned = $false
function Set-StartupStage([string]$Stage, [string]$ErrorType = '') {
    @{ stage = $Stage; error_type = $ErrorType; session_id = [Diagnostics.Process]::GetCurrentProcess().SessionId; updated = [DateTimeOffset]::Now.ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $taskState -Encoding utf8
}
try {
    try { $taskOwned = $taskMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $taskOwned = $true }
    if (-not $taskOwned) { exit 0 }
    Set-StartupStage 'starting'
    $taskPythonProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'")
    foreach ($taskService in @(
        @{ Role = 'serve'; Name = 'server' },
        @{ Role = 'runner'; Name = 'runner' },
        @{ Role = 'runner'; Name = 'course-hub-runner' }
    )) {
        $taskRole = $taskService.Role
        $taskName = $taskService.Name
        $taskConfig = Join-Path $PSScriptRoot ($taskName + '.json')
        if (-not (Test-Path -LiteralPath $taskConfig -PathType Leaf)) { continue }
        # Session 0 command lines can be unreadable to the interactive Keepalive.
        if ($taskRole -eq 'serve' -and @(Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue).Count) { continue }
        $taskExisting = @($taskPythonProcesses | Where-Object {
            $_.CommandLine -and $_.CommandLine.Contains($taskConfig) -and (
                $_.CommandLine -match ('\bwebcodex\s+' + $taskRole + '\b') -or
                ($taskRole -eq 'runner' -and $_.CommandLine.Contains($taskSessionRunner))
            )
        })
        if (-not $taskExisting.Count) {
            if ($taskRole -eq 'runner' -and $taskBackground -and $taskInteractiveAvailable) { continue }
            $taskArguments = @('-m', 'webcodex', $taskRole, '--config', ('"' + $taskConfig + '"'))
            if ($taskRole -eq 'runner' -and $taskBackground) {
                $taskArguments = @(('"' + $taskSessionRunner + '"'), ('"' + $taskConfig + '"'))
            }
            Start-Process -FilePath $taskPython -ArgumentList $taskArguments -WorkingDirectory (Join-Path $taskRoot 'harness') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot ('logs\' + $taskName + '.stdout.log')) -RedirectStandardError (Join-Path $PSScriptRoot ('logs\' + $taskName + '.stderr.log')) | Out-Null
        }
    }
    $taskHealth = $null
    for ($taskAttempt = 0; $taskAttempt -lt 30; $taskAttempt++) {
        try { $taskHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:18765/healthz' -TimeoutSec 2; break } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $taskHealth) { throw 'WebCodex Server did not become healthy.' }
    Set-StartupStage 'connecting_tunnel'
    $taskStatus = & $taskTunnel runtimes status webcodex-windows --json | ConvertFrom-Json
    if (-not ($taskStatus.process_running -and $taskStatus.healthy -and $taskStatus.ready)) {
        & ([Diagnostics.Process]::GetCurrentProcess().MainModule.FileName) -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'connect-windows-tunnel.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Tunnel connection failed; check tunnel-startup.json and logs.' }
    }
    $taskStatus = & $taskTunnel runtimes status webcodex-windows --json | ConvertFrom-Json
    if (-not ($taskStatus.process_running -and $taskStatus.healthy -and $taskStatus.ready)) { throw 'Tunnel is not ready.' }
    # Restore the main Tunnel before optional task-center or WSL startup.
    $taskFailures = @()
    foreach ($taskOptional in @('task-center\start-task-center-ssh.ps1', 'wsl\start-wsl-runner.ps1')) {
        $taskOptionalPath = Join-Path $PSScriptRoot $taskOptional
        if (-not (Test-Path -LiteralPath $taskOptionalPath -PathType Leaf)) { continue }
        try { & $taskOptionalPath }
        catch {
            $taskFailures += @{
                component = $taskOptional
                error_type = $_.Exception.GetType().FullName
                error_id = $_.FullyQualifiedErrorId
                line = $_.InvocationInfo.ScriptLineNumber
            }
        }
    }
    if ($taskFailures.Count) {
        @{
            stage = 'degraded'
            errors = $taskFailures
            session_id = [Diagnostics.Process]::GetCurrentProcess().SessionId
            updated = [DateTimeOffset]::Now.ToString('o')
        } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $taskState -Encoding utf8
        exit 1
    }
    Set-StartupStage 'ready'
    exit 0
}
catch {
    @{ stage = 'failed'; error_type = $_.Exception.GetType().FullName; error_id = $_.FullyQualifiedErrorId; line = $_.InvocationInfo.ScriptLineNumber; session_id = [Diagnostics.Process]::GetCurrentProcess().SessionId; updated = [DateTimeOffset]::Now.ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $taskState -Encoding utf8
    exit 1
}
finally {
    if ($taskOwned) { $taskMutex.ReleaseMutex() }
    $taskMutex.Dispose()
}
