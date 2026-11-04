$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPort = 8765
Get-Content -LiteralPath '.env' | ForEach-Object {
    if ($_ -match '^HEALTH_PORT\s*=\s*(\d+)') { $taskPort = [int]$Matches[1] }
}
$taskHealthClient = New-Object System.Net.WebClient
$taskHealthClient.Encoding = [System.Text.Encoding]::UTF8
$taskHealthClient.Proxy = $null
$taskState = $taskHealthClient.DownloadString("http://127.0.0.1:$taskPort/health") | ConvertFrom-Json
$taskHealthClient.Dispose()
if ($taskState.service -ne 'lark-receipt-bot') { throw 'The local service is not the receipt bot.' }
$taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($taskState.pid)"
if ($taskProcess.CommandLine -notmatch 'receipt_bot\.main') { throw 'Process identity check failed.' }
Stop-Process -Id $taskState.pid
Write-Host 'Bot stopped.'
