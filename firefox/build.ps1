$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent $Root
$Dist = Join-Path $Repo 'dist'

$Zip = Join-Path $Dist 'stashlibrary-firefox.zip'
$Xpi = Join-Path $Dist 'stashlibrary-firefox.xpi'

# Make sure dist exists.
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

# Remove previous build files if they exist.
Remove-Item $Zip -Force -ErrorAction SilentlyContinue
Remove-Item $Xpi -Force -ErrorAction SilentlyContinue

# Package with explicit forward-slash entry names. WebExtension resource URLs
# use URL paths, while Compress-Archive on Windows writes backslash entries that
# can make images fail to resolve after installation.
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Archive = [System.IO.Compression.ZipFile]::Open(
    $Zip,
    [System.IO.Compression.ZipArchiveMode]::Create
)
try {
    $Files = Get-ChildItem -Path $Root -Recurse -Force -File |
        Where-Object { $_.FullName -ne $MyInvocation.MyCommand.Path }
    foreach ($File in $Files) {
        $Relative = $File.FullName.Substring($Root.Length + 1).Replace('\', '/')
        $Entry = $Archive.CreateEntry($Relative, [System.IO.Compression.CompressionLevel]::Optimal)
        $Input = $File.OpenRead()
        $Output = $Entry.Open()
        try { $Input.CopyTo($Output) }
        finally { $Output.Dispose(); $Input.Dispose() }
    }
}
finally {
    $Archive.Dispose()
}

# An XPI is a ZIP-formatted Firefox extension package.
Move-Item $Zip $Xpi

Write-Host 'Built dist\stashlibrary-firefox.xpi' -ForegroundColor Green
