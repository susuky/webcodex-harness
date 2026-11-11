param(
    [string]$SshTarget = $env:WEBCODEX_TASK_CENTER_SSH_TARGET,
    [string]$SshExecutable = (Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe')
)

if ([string]::IsNullOrWhiteSpace($SshTarget)) { throw 'Set WEBCODEX_TASK_CENTER_SSH_TARGET or pass -SshTarget for this installation.' }

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$TaskCfg = Split-Path -Parent $PSScriptRoot
$TaskProfileSsh = Join-Path $env:USERPROFILE '.ssh'
$TaskPythonw = Join-Path (Split-Path -Parent $TaskCfg) 'harness\.venv\Scripts\pythonw.exe'
$TaskClient = Join-Path $PSScriptRoot 'ssh-heartbeat-client.py'
$TaskSecurity = [Security.AccessControl.MutexSecurity]::new()
foreach ($TaskSid in @([Security.Principal.WindowsIdentity]::GetCurrent().User,[Security.Principal.SecurityIdentifier]::new('S-1-5-18'),[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
    $TaskSecurity.AddAccessRule([Security.AccessControl.MutexAccessRule]::new($TaskSid,[Security.AccessControl.MutexRights]::FullControl,[Security.AccessControl.AccessControlType]::Allow))
}
$TaskCreated = $false
$TaskMutex = [Threading.Mutex]::new($false,'Global\WebCodexTaskCenterSSH',[ref]$TaskCreated,$TaskSecurity)
$TaskOwned = $false
function Write-TaskCenterState([string]$State, [int]$ProcessId = 0) {
    @{stage=$State;ssh_pid=$ProcessId;caller_pid=$PID;session_id=[Diagnostics.Process]::GetCurrentProcess().SessionId;updated=[DateTimeOffset]::Now.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'startup.json') -Encoding UTF8
}
try {
    try { $TaskOwned = $TaskMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $TaskOwned = $true }
    if (-not $TaskOwned) { return }
    $TaskExisting = @(Get-CimInstance Win32_Process -OperationTimeoutSec 8 -Filter "Name='ssh.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine.Contains('127.0.0.1:18769:127.0.0.1:18765') -and $_.CommandLine.Contains('127.0.0.1:8790:127.0.0.1:8790') -and $_.CommandLine.Contains($SshTarget)
    })
    if ($TaskExisting.Count) { Write-TaskCenterState 'existing' $TaskExisting[0].ProcessId; return }
    # Preserve an unreadable or unrelated listener instead of creating a conflicting SSH process.
    if (@(Get-NetTCPConnection -LocalPort 8790 -State Listen -ErrorAction SilentlyContinue).Count) { Write-TaskCenterState 'port_preserved'; return }
    $TaskArgs = @('--ssh-executable',$SshExecutable,'-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8','-o','IdentitiesOnly=yes','-o',('UserKnownHostsFile=' + (Join-Path $TaskProfileSsh 'known_hosts')),'-i',(Join-Path $TaskProfileSsh 'id_ed25519'),'-i',(Join-Path $TaskProfileSsh 'id_rsa'),'-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=10','-o','ServerAliveCountMax=3','-E',(Join-Path $PSScriptRoot 'ssh-20261104.log'),'-R','127.0.0.1:18769:127.0.0.1:18765','-L','127.0.0.1:8790:127.0.0.1:8790',$SshTarget,'exec /usr/bin/python3 ~/.config/webcodex-task-center/windows-ssh-watchdog.py')
    $TaskCommand = '"' + $TaskPythonw + '" "' + $TaskClient + '" ' + (($TaskArgs | ForEach-Object { '"' + $_ + '"' }) -join ' ')
    $TaskStartup = New-CimInstance Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0;CreateFlags=[uint32]0x10}
    $TaskLaunch = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$TaskCommand;CurrentDirectory=$TaskCfg;ProcessStartupInformation=$TaskStartup} -OperationTimeoutSec 8
    if ($TaskLaunch.ReturnValue -ne 0) { throw "Task center SSH launch failed: $($TaskLaunch.ReturnValue)" }
    Write-TaskCenterState 'starting'
    for ($TaskAttempt = 0; $TaskAttempt -lt 12; $TaskAttempt++) {
        Start-Sleep -Seconds 1
        if (-not (Get-Process -Id $TaskLaunch.ProcessId -ErrorAction SilentlyContinue)) {
            Write-TaskCenterState 'failed'
            throw 'Task center SSH exited; check heartbeat-client.log and ssh-20261104.log.'
        }
        try {
            $TaskHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8790/healthz' -TimeoutSec 2
            if ($TaskHealth.status -eq 'ok' -and $TaskHealth.service.product -eq 'webcodex-harness') {
                $TaskListener = Get-NetTCPConnection -LocalPort 8790 -State Listen -ErrorAction Stop | Select-Object -First 1
                Write-TaskCenterState 'connected' $TaskListener.OwningProcess
                return
            }
        } catch [System.Net.WebException] {}
    }
    Write-TaskCenterState 'failed'
    throw 'Task center SSH did not become reachable.'
} finally {
    if ($TaskOwned) { $TaskMutex.ReleaseMutex() }
    $TaskMutex.Dispose()
}
