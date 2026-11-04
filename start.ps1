$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    py -3.11 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 is required.' }
    & $taskPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
$taskPort = 8765
Get-Content -LiteralPath '.env' | ForEach-Object {
    if ($_ -match '^HEALTH_PORT\s*=\s*(\d+)') { $taskPort = [int]$Matches[1] }
}
try {
    $taskHealthClient = New-Object System.Net.WebClient
    $taskHealthClient.Encoding = [System.Text.Encoding]::UTF8
    $taskHealthClient.Proxy = $null
    $taskExisting = $taskHealthClient.DownloadString("http://127.0.0.1:$taskPort/health") | ConvertFrom-Json
    $taskHealthClient.Dispose()
    if ($taskExisting.service -eq 'lark-receipt-bot') {
        Write-Host "Bot already running. PID: $($taskExisting.pid)"
        exit 0
    }
} catch {}
New-Item -ItemType Directory -Path 'logs' -Force | Out-Null
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList @('-u','-m','receipt_bot.main') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'logs\stdout.log') -RedirectStandardError (Join-Path $PSScriptRoot 'logs\stderr.log') -PassThru
for ($taskAttempt = 0; $taskAttempt -lt 30; $taskAttempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $taskHealthClient = New-Object System.Net.WebClient
        $taskHealthClient.Encoding = [System.Text.Encoding]::UTF8
        $taskHealthClient.Proxy = $null
        $taskHealth = $taskHealthClient.DownloadString("http://127.0.0.1:$taskPort/health") | ConvertFrom-Json
        $taskHealthClient.Dispose()
        if ($taskHealth.service -eq 'lark-receipt-bot') {
            Write-Host "Bot running. PID: $($taskHealth.pid)"
            Write-Host "Status: http://127.0.0.1:$taskPort/health"
            exit 0
        }
    } catch {}
}
throw 'Startup not verified. Inspect logs\stderr.log.'
