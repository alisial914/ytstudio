# Stops the watcher and everything it started (ffmpeg recorder, renders).
$pidFile = Join-Path $PSScriptRoot "work\live\watcher.pid"
if (Test-Path $pidFile) { taskkill /PID (Get-Content $pidFile) /T /F; Remove-Item $pidFile } else { Write-Host "Watcher not running." }
