# Start the Dify Celery beat scheduler from source.
Remove-Item Env:LOG_FORMAT -ErrorAction SilentlyContinue
Set-Location "E:\work\dify-main\dify-main"
uv --directory api run celery -A app.celery beat --loglevel INFO --scheduler celery.beat:PersistentScheduler
