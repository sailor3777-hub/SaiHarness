# Installs Python for the current user when Run.cmd finds none.
# With winget: winget install. Without winget (Windows Server, LTSC, locked-down
# images): downloads the official installer from python.org, checks its
# Authenticode signature (Python Software Foundation) and runs it silently.
# Exit code 0 when the installer succeeded. Keep this file ASCII-only.
param([string]$Version = '3.14.7')
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # the progress bar makes Invoke-WebRequest very slow on PowerShell 5.1

try {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing Python $Version with winget..."
        winget install -e --id Python.Python.3.14 --scope user
        exit $LASTEXITCODE
    }

    Write-Host "winget not found; downloading Python $Version from python.org..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $exe = Join-Path $env:TEMP "python-$Version-amd64.exe"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$Version/python-$Version-amd64.exe" -OutFile $exe -UseBasicParsing

    $sig = Get-AuthenticodeSignature $exe
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike '*O=Python Software Foundation*') {
        Remove-Item $exe -Force -ErrorAction SilentlyContinue
        Write-Host "The downloaded installer is not signed by the Python Software Foundation ($($sig.Status)); not running it."
        exit 1
    }

    Write-Host "Installing Python $Version (per user, takes a minute)..."
    $p = Start-Process -FilePath $exe -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_test=0' -Wait -PassThru
    Remove-Item $exe -Force -ErrorAction SilentlyContinue
    exit $p.ExitCode
} catch {
    Write-Host "Installing Python failed: $($_.Exception.Message)"
    exit 1
}
