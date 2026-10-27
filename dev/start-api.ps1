# Start the Dify API server from source (port 5001).
Remove-Item Env:LOG_FORMAT -ErrorAction SilentlyContinue
Set-Location "E:\work\dify-main\dify-main\api"
uv run python -m app
