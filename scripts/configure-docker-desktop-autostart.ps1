[CmdletBinding()]
param(
    [switch]$Disable
)

$ErrorActionPreference = "Stop"

$desktopExecutable = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
$settingsPath = Join-Path $env:APPDATA "Docker\settings-store.json"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$autoStart = -not $Disable.IsPresent

if (-not (Test-Path -LiteralPath $desktopExecutable -PathType Leaf)) {
    throw "Docker Desktop executable was not found."
}
if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
    throw "Docker Desktop settings-store.json was not found."
}

$settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
if ($settings.PSObject.Properties.Name -contains "AutoStart") {
    $settings.AutoStart = $autoStart
} else {
    $settings | Add-Member -NotePropertyName AutoStart -NotePropertyValue $autoStart
}

$serialized = $settings | ConvertTo-Json -Depth 64
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($settingsPath, $serialized + [Environment]::NewLine, $utf8NoBom)

New-Item -Path $runKey -Force | Out-Null
if ($autoStart) {
    Set-ItemProperty `
        -Path $runKey `
        -Name "Docker Desktop" `
        -Value ('"{0}"' -f $desktopExecutable)
} else {
    Remove-ItemProperty `
        -Path $runKey `
        -Name "Docker Desktop" `
        -ErrorAction SilentlyContinue
}

$runEntry = $null
if ($autoStart) {
    $runEntry = Get-ItemPropertyValue `
        -Path $runKey `
        -Name "Docker Desktop"
}

[pscustomobject]@{
    AutoStart = $autoStart
    RunEntry = $runEntry
    SettingsPath = $settingsPath
}
