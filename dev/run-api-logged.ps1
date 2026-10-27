# Run the Dify API with console output (for capturing request tracebacks).
Remove-Item Env:LOG_FORMAT -ErrorAction SilentlyContinue
$env:PYTHONUNBUFFERED = "1"
Set-Location "E:\work\dify-main\dify-main\api"
uv run python -m app
