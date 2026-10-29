param(
    [Parameter(Mandatory = $true)]
    [string]$Installer
)

$ErrorActionPreference = "Stop"
$testRoot = Join-Path $env:RUNNER_TEMP ("desktop-install-test-" + [guid]::NewGuid())
$installDir = Join-Path $testRoot "安装测试"
$appProcess = $null

function Wait-ForExit($Process, $Description) {
    if (-not $Process.WaitForExit(120000)) {
        $Process.Kill()
        throw "$Description timed out"
    }
    if ($Process.ExitCode -ne 0) {
        throw "$Description failed with exit code $($Process.ExitCode)"
    }
}

try {
    # NSIS requires /D to be last and unquoted, including paths with spaces.
    $setup = Start-Process -FilePath (Resolve-Path $Installer).Path `
        -ArgumentList "/S /D=$installDir" -PassThru
    Wait-ForExit $setup "Installation"
    $binary = Join-Path $installDir "coding-tools-mcp-desktop.exe"
    $manifest = Join-Path $installDir "runtime\manifest.json"
    if (-not (Test-Path $binary)) { throw "Installed desktop executable is missing" }
    if (-not (Test-Path $manifest)) { throw "Installed runtime manifest is missing" }
    $runtime = Get-Content $manifest -Raw | ConvertFrom-Json
    if (-not (Test-Path (Join-Path $installDir "runtime\$($runtime.wheel)"))) {
        throw "Installed runtime wheel is missing"
    }
    if (-not (Test-Path (Join-Path $installDir "runtime\requirements.txt"))) {
        throw "Installed runtime requirements are missing"
    }

    $appProcess = Start-Process -FilePath $binary -PassThru
    Start-Sleep -Seconds 10
    $appProcess.Refresh()
    if ($appProcess.HasExited) {
        throw "Installed desktop exited during startup: $($appProcess.ExitCode)"
    }
    Stop-Process -Id $appProcess.Id -Force
    $appProcess.WaitForExit()
    $appProcess = $null

    $uninstaller = Join-Path $installDir "uninstall.exe"
    if (-not (Test-Path $uninstaller)) { throw "Uninstaller is missing" }
    # _?= keeps NSIS in this process so the test can wait for removal.
    $remove = Start-Process -FilePath $uninstaller `
        -ArgumentList "/S _?=$installDir" -PassThru
    Wait-ForExit $remove "Uninstallation"
    if (Test-Path $binary) { throw "Uninstallation left the app executable behind" }
    if (Test-Path $manifest) { throw "Uninstallation left runtime resources behind" }
    Write-Host "Windows installer passed installation, startup and uninstallation checks."
} finally {
    if ($null -ne $appProcess -and -not $appProcess.HasExited) {
        Stop-Process -Id $appProcess.Id -Force
    }
    if (Test-Path $testRoot) { Remove-Item $testRoot -Recurse -Force }
}
