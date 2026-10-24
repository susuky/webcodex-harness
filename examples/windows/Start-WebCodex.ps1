$ErrorActionPreference = 'Stop'

# Let Task Scheduler own the services after this PowerShell window closes.
$task = Get-ScheduledTask -TaskName 'WebCodex Windows Keepalive'
Enable-ScheduledTask -InputObject $task | Out-Null
Start-ScheduledTask -InputObject $task
