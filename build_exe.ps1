$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$exePath = Join-Path $projectRoot "release\PCleaner\PCleaner.exe"
$zipPath = Join-Path $projectRoot "release\PCleaner-portable.zip"

Push-Location $projectRoot
try {
    python -m PyInstaller --version
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller bulunamadı. Önce 'python -m pip install pyinstaller' komutunu çalıştır."
    }

    python -m PyInstaller `
        --noconfirm `
        --windowed `
        --name PCleaner `
        --distpath (Join-Path $projectRoot "release") `
        --workpath (Join-Path $projectRoot "build\pyinstaller-portable") `
        (Join-Path $projectRoot "pcleaner.py")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller derlemesi başarısız oldu."
    }
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "Derleme komutu bitti ancak PCleaner.exe bulunamadı."
    }
    Compress-Archive -Path (Join-Path $projectRoot "release\PCleaner") -DestinationPath $zipPath -CompressionLevel Optimal -Force
    if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf)) {
        throw "Taşınabilir paket ZIP dosyası oluşturulamadı."
    }
    Write-Host "Hazır: $exePath"
    Write-Host "Taşınabilir paket: $zipPath"
} finally {
    Pop-Location
}
