# One-click start of the Dify backend from source (API + Worker + Beat) in three new windows.
$root = "E:\work\dify-main\dify-main\dev"
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "$root\start-api.ps1"
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "$root\start-worker.ps1"
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "$root\start-beat.ps1"
