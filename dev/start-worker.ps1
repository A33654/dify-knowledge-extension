# Start the Dify Celery worker from source.
Remove-Item Env:LOG_FORMAT -ErrorAction SilentlyContinue
Set-Location "E:\work\dify-main\dify-main\api"
uv run celery -A app.celery worker -P gevent -c 1 --loglevel INFO -Q dataset,dataset_summary,priority_dataset,priority_pipeline,pipeline,mail,ops_trace,app_deletion,plugin,workflow_storage,conversation,workflow,schedule_poller,schedule_executor,triggered_workflow_dispatcher,trigger_refresh_executor,retention,workflow_based_app_execution,knowledge_fs_lifecycle
