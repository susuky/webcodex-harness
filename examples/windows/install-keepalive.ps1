param([Parameter(Mandatory = $true)][string]$ConfigDir)

$ErrorActionPreference = 'Stop'
$taskConfig = (Resolve-Path -LiteralPath $ConfigDir).ProviderPath
$taskStart = Join-Path $taskConfig 'start-windows-webcodex.ps1'
if (-not (Test-Path -LiteralPath $taskStart -PathType Leaf)) {
    throw 'The existing start-windows-webcodex.ps1 is required.'
}
$taskConnect = Join-Path $taskConfig 'connect-windows-tunnel.ps1'
if (Test-Path -LiteralPath $taskConnect -PathType Leaf) {
    $taskBody = [IO.File]::ReadAllText($taskConnect)
    if ($taskBody.Contains('harness\.venv\Scripts\webcodex.exe')) {
        if (-not $taskBody.Contains(" + ' bridge --config '")) {
            throw 'Inspect the custom Tunnel launcher before changing its command.'
        }
        $taskBackup = $taskConnect + '.before-direct-python'
        if (-not (Test-Path -LiteralPath $taskBackup)) {
            Copy-Item -LiteralPath $taskConnect -Destination $taskBackup
        }
        # The uv console trampoline can fail outside the original terminal environment.
        $taskBody = $taskBody.Replace('harness\.venv\Scripts\webcodex.exe', 'harness\.venv\Scripts\python.exe').Replace(" + ' bridge --config '", " + ' -m webcodex bridge --config '")
        [IO.File]::WriteAllText($taskConnect, $taskBody, (New-Object Text.UTF8Encoding($true)))
    }
}
$taskName = 'WebCodex Windows Keepalive'
$taskShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$taskUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction = New-ScheduledTaskAction -Execute $taskShell -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $taskStart + '"') -WorkingDirectory $taskConfig
$taskTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $taskUser),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1))
)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
# Parallel checks may renew a component while another instance still owns live children.
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances Parallel -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTriggers -Principal $taskPrincipal -Settings $taskSettings -Description 'Check the existing Windows WebCodex services every minute and start missing processes.' -Force | Out-Null
[xml]$taskXml = Export-ScheduledTask -TaskName $taskName
if ($taskXml.Task.Principals.Principal.LogonType -ne 'InteractiveToken' -or
    $taskXml.Task.Principals.Principal.RunLevel -eq 'HighestAvailable' -or
    $taskXml.Task.Settings.ExecutionTimeLimit -ne 'PT0S' -or
    $taskXml.Task.Settings.MultipleInstancesPolicy -ne 'Parallel' -or
    $taskXml.Task.Triggers.TimeTrigger.Repetition.Interval -ne 'PT1M' -or
    $taskXml.Task.Triggers.TimeTrigger.Repetition.Duration) {
    throw 'The registered keepalive settings did not match the requested lifecycle.'
}
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
