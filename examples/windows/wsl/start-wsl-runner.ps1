param([string]$WslUser = $env:WEBCODEX_WSL_USER)

if ([string]::IsNullOrWhiteSpace($WslUser)) { throw 'Set WEBCODEX_WSL_USER or pass -WslUser for this installation.' }

$ErrorActionPreference = 'Stop'
$wslCommand = Join-Path $env:SystemRoot 'System32\wsl.exe'
function Invoke-WslControl([string[]]$Arguments, [int]$TimeoutMilliseconds = 15000) {
    $process = [Diagnostics.Process]::new()
    $process.StartInfo.FileName = $wslCommand
    $process.StartInfo.Arguments = (($Arguments | ForEach-Object {
        if ($_ -match '[\s"]' -or $_.Length -eq 0) {
            '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
        } else {
            $_
        }
    }) -join ' ')
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    try {
        if (-not $process.Start()) { throw 'WSL control command did not start.' }
        $output = $process.StandardOutput.ReadToEndAsync()
        $errorOutput = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            # End only this foreground control call, leaving WSL services running.
            $process.Kill()
            $process.WaitForExit(3000) | Out-Null
            throw 'WSL control command timed out.'
        }
        $text = $output.GetAwaiter().GetResult()
        $errorText = $errorOutput.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw ('WSL control command failed ({0}): {1}' -f $process.ExitCode, $errorText.Trim())
        }
        return $text
    } finally {
        $process.Dispose()
    }
}

Invoke-WslControl -Arguments @('-d','Ubuntu','--cd','/','--user','root','--','systemctl','start','webcodex-runner.service') | Out-Null
# A foreground WSL command keeps the distro alive independently of systemd's idle policy.
$wslKeeper = @(Get-CimInstance Win32_Process -OperationTimeoutSec 8 -Filter "Name = 'wsl.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine -match '\s-d\s+Ubuntu\b' -and $_.CommandLine -match '\s--exec\s+/usr/bin/sleep\s+infinity(?:\s|$)'
})
if (-not $wslKeeper.Count) {
    Start-Process -FilePath $wslCommand -ArgumentList @('-d','Ubuntu','--cd','/','--user',$WslUser,'--exec','/usr/bin/sleep','infinity') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'keeper.stdout.log') -RedirectStandardError (Join-Path $PSScriptRoot 'keeper.stderr.log') | Out-Null
}
$wslKey = Join-Path $PSScriptRoot 'id_ed25519'
$wslExisting = @(Get-CimInstance Win32_Process -OperationTimeoutSec 8 -Filter "Name = 'ssh.exe'" | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($wslKey) })
if (-not $wslExisting.Count) {
    $wslAddresses = (Invoke-WslControl -Arguments @('-d','Ubuntu','--cd','/','--','hostname','-I')) -split '\s+'
    $wslAddress = $wslAddresses | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1
    if (-not $wslAddress) { throw 'WSL has no IPv4 address for its SSH connection.' }
    $wslArguments = @('-N','-T','-i',$wslKey,'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','HostKeyAlias=webcodex-wsl-Ubuntu','-o',('UserKnownHostsFile=' + (Join-Path $PSScriptRoot 'known_hosts')),'-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3','-R','127.0.0.1:18765:127.0.0.1:18765',($WslUser + '@' + $wslAddress))
    Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe') -ArgumentList $wslArguments -WindowStyle Hidden -RedirectStandardError (Join-Path $PSScriptRoot 'ssh.stderr.log') | Out-Null
}
