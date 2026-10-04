$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent $Root
$Dist = Join-Path $Repo 'dist'
$Installer = Join-Path $Root 'installer'
$Payload = Join-Path $Installer 'payload'
$Patcher = Join-Path $Root 'manifestpatch'
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
New-Item -ItemType Directory -Force -Path $Payload | Out-Null

Push-Location (Join-Path $Root 'launcher')
$env:GOOS='windows'; $env:GOARCH='amd64'; $env:CGO_ENABLED='0'
go build -trimpath -ldflags '-s -w -H=windowsgui' -o (Join-Path $Payload 'StashLibraryNativeHelper.exe') .
Pop-Location

# Embed an explicit Windows application manifest so Windows knows that the
# native helper is a normal Windows 10/11 application and must run as the user.
Push-Location $Patcher
go run . (Join-Path $Payload 'StashLibraryNativeHelper.exe') (Join-Path $Root 'launcher.manifest')
Pop-Location

Copy-Item (Join-Path $Repo 'native\host.py') (Join-Path $Payload 'host.py') -Force

Push-Location $Installer
go build -trimpath -ldflags '-s -w -H=windowsgui' -o (Join-Path $Dist 'StashLibrary-Windows-Helper.exe') .
Pop-Location

# requestedExecutionLevel=asInvoker disables legacy installer-detection
# heuristics and the compatibility metadata prevents Program Compatibility
# Assistant from treating StashLibrary as an old/unknown installer.
Push-Location $Patcher
go run . (Join-Path $Dist 'StashLibrary-Windows-Helper.exe') (Join-Path $Root 'installer.manifest')
Pop-Location

Write-Host 'Built dist\StashLibrary-Windows-Helper.exe with embedded Windows manifests' -ForegroundColor Green
