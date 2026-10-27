from app import app as flask_app

celery = flask_app.extensions["celery"]
r = celery.send_task(
    "tasks.recover_document_indexing_task.recover_document_indexing_task",
    args=["844ec111-4ccc-42ba-9df6-9495354e69f2", "3e52ca79-4aa7-4dd1-8980-f3a640f7fa57"],
    queue="dataset",
)
print("TASK_ID", r.id)
