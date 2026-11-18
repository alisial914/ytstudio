# Starts the live-stream watcher in the background (hidden). Extra args are passed through, e.g.
#   .\start_watcher.ps1 --chunk 180 --clips-per-chunk 3
$root = $PSScriptRoot
$pidFile = Join-Path $root "work\live\watcher.pid"
if ((Test-Path $pidFile) -and (Get-Process -Id (Get-Content $pidFile) -ErrorAction SilentlyContinue)) {
    Write-Host "Watcher already running (PID $(Get-Content $pidFile)). Log: work\live\watcher.log"; exit
}
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
$env:PYTHONUNBUFFERED = '1'
New-Item -ItemType Directory -Force (Join-Path $root "work\live") | Out-Null
$p = Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") -ArgumentList (@("watcher.py") + $args) `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $root "work\live\watcher.out.log") -RedirectStandardError (Join-Path $root "work\live\watcher.err.log")
$p.Id | Set-Content $pidFile
Write-Host "Watcher started (PID $($p.Id)). Paste a live URL into stream_url.txt and save. Log: work\live\watcher.log"
