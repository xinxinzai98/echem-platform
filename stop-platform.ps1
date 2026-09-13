$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$target = Split-Path -Parent $MyInvocation.MyCommand.Path
$expectedExecutable = Join-Path $target 'runtime\python.exe'
$pidFile = Join-Path $target 'state\server.pid'
$uri = 'http://127.0.0.1:8787'

try {
    $status = Invoke-RestMethod -Uri "$uri/api/status" -Method Get -TimeoutSec 2
    if ($status.instrument_control) {
        $runs = @(Invoke-RestMethod -Uri "$uri/api/control/runs" -Method Get -TimeoutSec 2)
        $active = @(
            $runs | Where-Object {
                $_.status -in @('starting', 'running', 'stop_requested')
            }
        )
        if ($active.Count -gt 0) {
            throw (
                'Platform stop blocked: an instrument run is active. ' +
                'Use the CHI software to stop safely and complete the review first.'
            )
        }
    }
}
catch {
    if ($_.Exception.Message -like 'Platform stop blocked:*') {
        throw
    }
}

$processes = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.ExecutablePath -ieq $expectedExecutable -and
            $_.CommandLine -match 'app\.py'
        }
)

$stopped = @()
foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force
    $stopped += $process.ProcessId
}
Start-Sleep -Milliseconds 300
Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue

$remaining = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.ExecutablePath -ieq $expectedExecutable -and
            $_.CommandLine -match 'app\.py'
        }
)
if ($remaining.Count -gt 0) {
    throw "One or more platform processes could not be stopped."
}

[Console]::Out.WriteLine(
    ([ordered]@{
        Status = 'stopped'
        ProcessIds = $stopped
        Remaining = 0
    } | ConvertTo-Json -Depth 3)
)
