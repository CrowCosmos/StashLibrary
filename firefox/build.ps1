$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent $Root
$Dist = Join-Path $Repo 'dist'

$Zip = Join-Path $Dist 'locero-firefox.zip'
$Xpi = Join-Path $Dist 'locero-firefox.xpi'

# Make sure dist exists.
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

# Remove previous build files if they exist.
Remove-Item $Zip -Force -ErrorAction SilentlyContinue
Remove-Item $Xpi -Force -ErrorAction SilentlyContinue

# Package everything in firefox/ except this build script.
$Files = Get-ChildItem -Path $Root -Force |
    Where-Object { $_.Name -ne 'build.ps1' }

Compress-Archive -Path $Files.FullName -DestinationPath $Zip -CompressionLevel Optimal

# An XPI is a ZIP-formatted Firefox extension package.
Move-Item $Zip $Xpi

Write-Host 'Built dist\locero-firefox.xpi' -ForegroundColor Green