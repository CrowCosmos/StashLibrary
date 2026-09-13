$ErrorActionPreference = 'Stop'

$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildScripts = @(
    'firefox\build.ps1'
    'windows-installer\build.ps1'
    'zotero\build.ps1'
)

foreach ($RelativePath in $BuildScripts) {
    $BuildScript = Join-Path $Repo $RelativePath

    Write-Host "Building $RelativePath..." -ForegroundColor Cyan
    & $BuildScript
}

Write-Host 'All builds completed successfully.' -ForegroundColor Green
