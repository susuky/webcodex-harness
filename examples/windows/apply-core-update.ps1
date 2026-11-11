param(
    [string]$InstallRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$configDirectory = Join-Path $InstallRoot 'config'
$python = Join-Path $InstallRoot 'harness\.venv\Scripts\python.exe'
$destination = Join-Path $InstallRoot 'harness\src\webcodex'
$startupScript = Join-Path $configDirectory 'start-hidden.py'
$helperScript = Join-Path $configDirectory 'task-center\ssh-heartbeat-client.py'
$sessionRunnerScript = Join-Path $configDirectory 'session_runner.py'
$runnerConfigs = @((Join-Path $configDirectory 'runner.json'), (Join-Path $configDirectory 'course-hub-runner.json')) |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
$serverConfig = Join-Path $configDirectory 'server.json'
$gitSsh = 'C:\Program Files\Git\usr\bin\ssh.exe'

$workProbe = @'
import json, sqlite3, sys
from pathlib import Path
directory = Path(sys.argv[1])
report = []
for name in ('runner', 'course-hub-runner'):
    path = directory / (name + '.json')
    if name == 'course-hub-runner' and not path.exists():
        continue
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    state = Path(config['state_dir'])
    row = {'runner': name, 'processes': [], 'deliveries': []}
    for relative, key, query in (
        ('processes/processes.sqlite3', 'processes', "SELECT id,task_id FROM processes WHERE status='running'"),
        ('deliveries.sqlite3', 'deliveries', "SELECT id FROM deliveries WHERE status='running'"),
    ):
        database = state / relative
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as connection:
            row[key] = [list(item) for item in connection.execute(query)]
    report.append(row)
print(json.dumps(report))
'@

function Read-RunnerWork {
    # stdin preserves Python quotes under Windows PowerShell 5.1.
    $result = $workProbe | & $python - $configDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Could not read Runner work; no update was applied.' }
    return @($result | ConvertFrom-Json)
}

function Show-ActiveWork($Work) {
    $active = $false
    foreach ($runner in $Work) {
        foreach ($process in $runner.processes) {
            Write-Host ($runner.runner + ': process ' + $process[0] + ' (task ' + $process[1] + ')')
            $active = $true
        }
        foreach ($delivery in $runner.deliveries) {
            Write-Host ($runner.runner + ': operation ' + $delivery[0])
            $active = $true
        }
    }
    return $active
}

function Read-Processes {
    return @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='ssh.exe'" -OperationTimeoutSec 8)
}

function Test-ScriptCommand($Process, [string]$Script) {
    if ($Process.Name -notin @('python.exe', 'pythonw.exe') -or -not $Process.CommandLine) { return $false }
    $path = [regex]::Escape($Script)
    return $Process.CommandLine -match ('^\s*(?:"[^"]+"|\S+)\s+(?:"' + $path + '"|' + $path + ')(?:\s|$)')
}

function Test-ModuleCommand($Process, [string]$Mode, [string]$Config) {
    if ($Process.Name -notin @('python.exe', 'pythonw.exe') -or -not $Process.CommandLine) { return $false }
    $path = [regex]::Escape($Config)
    return ($Process.CommandLine -match ('^\s*(?:"[^"]+"|\S+)\s+-m\s+webcodex\s+' + $Mode + '(?:\s|$)') -and
        $Process.CommandLine -match ('(?:^|\s)--config\s+(?:"' + $path + '"|' + $path + ')(?:\s|$)'))
}

function Test-RunnerCommand($Process, [string]$Config) {
    if (Test-ModuleCommand $Process 'runner' $Config) { return $true }
    $path = [regex]::Escape($Config)
    return ((Test-ScriptCommand $Process $sessionRunnerScript) -and
        $Process.CommandLine -match ('(?:^|\s)(?:"' + $path + '"|' + $path + ')(?:\s|$)'))
}

function Stop-OwnedProcess([int]$ProcessId) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return }
    try {
        $process.Kill()
        if (-not $process.WaitForExit(10000)) { throw ('Process did not stop: ' + $ProcessId) }
    } finally {
        $process.Dispose()
    }
}

$work = Read-RunnerWork
if (Show-ActiveWork $work) {
    Write-Host 'Update deferred. Finish these WebCodex operations, then run this script again.'
    exit 2
}
if ($CheckOnly) {
    Write-Host 'Both Runners are idle. CheckOnly made no changes.'
    exit 0
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an administrator PowerShell window.'
}
$runtimeFiles = @(Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'runtime') -File)
if (-not $runtimeFiles.Count) { throw 'The staged runtime package is empty.' }
$startupTask = Get-ScheduledTask -TaskName 'WebCodex Windows Keepalive' -TaskPath '\'
if ([int]$startupTask.Principal.RunLevel -ne 0 -or
    -not @($startupTask.Actions | Where-Object { $_.Arguments -and $_.Arguments.Contains($startupScript) }).Count) {
    throw 'The existing Limited Keepalive task does not match this installation.'
}

$mutex = [Threading.Mutex]::new($false, 'Global\WebCodexWindowsStartup')
$owned = $false
$restart = $false
$installed = $false
try {
    try { $owned = $mutex.WaitOne(10000) } catch [Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { throw 'The existing startup check is busy. Run this script again after it finishes.' }
    $processes = Read-Processes
    $serverIds = @(Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($serverId in $serverIds) {
        $server = @($processes | Where-Object {
            $_.ProcessId -eq $serverId -and (Test-ModuleCommand $_ 'serve' $serverConfig)
        })
        if ($server.Count -ne 1) { throw 'Port 18765 does not belong to this installation; no process was stopped.' }
    }
    # Stop dispatch before the second idle check so another conversation cannot start work.
    $restart = $true
    foreach ($server in @($processes | Where-Object { Test-ModuleCommand $_ 'serve' $serverConfig })) {
        Stop-OwnedProcess $server.ProcessId
    }
    $freshHeartbeatAfter = [DateTime]::UtcNow.AddSeconds(16)
    if (Show-ActiveWork (Read-RunnerWork)) {
        throw 'New work arrived before dispatch stopped. The existing startup task will restore the Server.'
    }
    foreach ($process in $processes) {
        foreach ($config in $runnerConfigs) {
            if (Test-RunnerCommand $process $config) {
                Stop-OwnedProcess $process.ProcessId
                break
            }
        }
    }
    $helpers = @($processes | Where-Object { Test-ScriptCommand $_ $helperScript })
    $helperIds = @($helpers | Select-Object -ExpandProperty ProcessId)
    foreach ($helper in $helpers) { Stop-OwnedProcess $helper.ProcessId }
    # The helper's SSH child may have changed during its reconnect loop.
    foreach ($child in (Read-Processes)) {
        if ($child.Name -ne 'ssh.exe' -or $child.ParentProcessId -notin $helperIds -or -not $child.CommandLine) { continue }
        if ($child.CommandLine.Contains('127.0.0.1:18769:127.0.0.1:18765') -and
            $child.CommandLine.Contains('127.0.0.1:8790:127.0.0.1:8790')) {
            Stop-OwnedProcess $child.ProcessId
        }
    }
    foreach ($file in $runtimeFiles) {
        Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $destination $file.Name) -Force
    }
    $installed = $true
} finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
    if ($restart) {
        # Reuse the existing user's Limited task, including if a check or copy failed.
        Start-ScheduledTask -InputObject $startupTask
    }
}
if ($installed) {
    $client = Get-Content -LiteralPath (Join-Path $configDirectory 'client.json') -Raw | ConvertFrom-Json
    $headers = @{ Authorization = 'Bearer ' + $client.token }
    $runnerIds = @($runnerConfigs | ForEach-Object {
        (Get-Content -LiteralPath $_ -Raw | ConvertFrom-Json).runner_id
    })
    $healthy = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $serverHealth = Invoke-RestMethod 'http://127.0.0.1:18765/healthz' -TimeoutSec 2
            $portalHealth = Invoke-RestMethod 'http://127.0.0.1:8790/healthz' -TimeoutSec 2
            $inventory = Invoke-RestMethod 'http://127.0.0.1:18765/api/call' -Method Post -Headers $headers -ContentType 'application/json' -Body '{"name":"projects_list","arguments":{}}' -TimeoutSec 2
            $online = @($runnerIds | Where-Object {
                $prefix = $_ + '/'
                @($inventory.projects | Where-Object { $_.online -and $_.id.StartsWith($prefix) }).Count -gt 0
            }).Count -eq $runnerIds.Count
            $current = Read-Processes
            $portalIds = @(Get-NetTCPConnection -LocalPort 8790 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
            $gitTunnel = @($current | Where-Object {
                $_.ProcessId -in $portalIds -and $_.ExecutablePath -eq $gitSsh -and
                $_.CommandLine -and $_.CommandLine.Contains('127.0.0.1:18769:127.0.0.1:18765') -and
                $_.CommandLine.Contains('127.0.0.1:8790:127.0.0.1:8790')
            }).Count -gt 0
            # The old heartbeat lease lasts 15 seconds; require a new Runner heartbeat.
            if ([DateTime]::UtcNow -ge $freshHeartbeatAfter -and $serverHealth.status -eq 'ok' -and $portalHealth.status -eq 'ok' -and $online -and $gitTunnel) {
                $healthy = $true
                break
            }
        } catch [System.Net.WebException] {}
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) { throw 'Code was copied; check config\logs and config\task-center\heartbeat-client.log for startup errors.' }
    Write-Host ('Updated the runtime package (' + $runtimeFiles.Count + ' files). Both Runners are online; Server and task center respond through Git SSH.')
}
