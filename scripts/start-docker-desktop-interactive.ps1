[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$taskName = "StartStopAnalysis-DockerDesktop-Interactive"
$desktopExecutable = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $desktopExecutable -PathType Leaf)) {
    throw "Docker Desktop executable was not found."
}

$action = New-ScheduledTaskAction -Execute $desktopExecutable
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentIdentity `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

[pscustomobject]@{
    TaskName = $taskName
    UserId = $currentIdentity
    State = (Get-ScheduledTask -TaskName $taskName).State
}
