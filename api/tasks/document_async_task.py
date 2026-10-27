"""Asynchronous document upload task.

The upload endpoint persists a ``pending`` ``document_async_task`` row and
returns immediately. This worker owns the heavy half of the original
``DocumentService.save_prepared_documents`` upload path: it rebuilds the trusted
document from committed state, reuses the unchanged version comparison, and only
then hands the document to the existing indexing task owners.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import click
from celery import shared_task
from celery.exceptions import Retry
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.db.session_factory import session_factory
from extensions.ext_redis import redis_client
from extensions.ext_storage import storage
from libs.datetime_utils import naive_utc_now
from models.dataset import Document, DocumentAsyncTask
from models.enums import DocumentAsyncTaskStatus
from services.knowledge.dataset_service import DatasetService, DocumentService
from services.knowledge.document_async_task_service import DocumentAsyncTaskService, error_code_for
from services.knowledge.document_version_service import DocumentVersionService
from services.knowledge.entities.document_creation import DocumentIndexingJobs
from services.knowledge.entities.knowledge_entities import KnowledgeConfig
from services.knowledge.resource_scope import DatasetRef

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
# Exponential backoff: 2s, 4s, 8s for retries 0, 1, 2.
RETRY_DELAY_SECONDS = 2
# Long enough for a large document to be split and embedded without another
# worker rediscovering the same task.
UPLOAD_LOCK_TIMEOUT = 3600
# Consumed by the opt-in FlaskTask hook in extensions/ext_celery.py; it is popped
# from the task kwargs before `run()` so this task's signature stays unchanged.
TASK_EXECUTION_ID_KEY = "task_execution_id"


@shared_task(queue="dataset", bind=True, max_retries=MAX_RETRIES, default_retry_delay=RETRY_DELAY_SECONDS)
def document_async_task(self, task_id: str) -> None:
    """Process one queued document upload.

    Only a task identifier crosses the broker boundary: every ORM value is
    reloaded from committed state, including the payload stored by the endpoint.
    """
    start_at = time.perf_counter()
    try:
        outcome = _process(task_id)
    except Exception as exc:
        if self.request.retries >= MAX_RETRIES:
            logger.exception("Document async upload retry budget exhausted, task_id=%s", task_id)
            _mark_dead_letter(task_id, exc)
            raise
        countdown = RETRY_DELAY_SECONDS * (2**self.request.retries)
        # `failed` is not terminal: the row keeps `next_attempt_at` so a polling
        # client can tell that an automatic retry is still scheduled.
        _record_attempt_failure(task_id, exc, next_attempt_at=naive_utc_now() + timedelta(seconds=countdown))
        logger.warning(
            "Document async upload failed, scheduling retry %d/%d, task_id=%s",
            self.request.retries + 1,
            MAX_RETRIES,
            task_id,
            exc_info=True,
        )
        # `self.retry()` publishes the retry message and then raises `Retry`. When
        # the broker is unavailable the publish error escapes instead, and the task
        # would end silently without ever reaching `on_failure`. The terminal
        # dead-letter write therefore happens here, mirroring
        # tasks/ops_trace_task.py:132-142.
        try:
            raise self.retry(
                exc=exc,
                args=(),
                kwargs={"task_id": task_id, TASK_EXECUTION_ID_KEY: task_id},
                countdown=countdown,
            ) from exc
        except Retry:
            raise
        except Exception:
            logger.exception("Failed to schedule document async upload retry, task_id=%s", task_id)
            _mark_dead_letter(task_id, exc)
            raise

    logger.info(
        click.style(
            f"Processed document async upload task: {task_id} outcome={outcome} "
            f"latency: {time.perf_counter() - start_at}",
            fg="green",
        )
    )


def _process(task_id: str) -> str:
    with session_factory.create_session() as session:
        task = DocumentAsyncTaskService.get_by_id(session, task_id)
        if task is None:
            logger.warning("Document async upload task not found: %s", task_id)
            return "missing"

    lock = redis_client.lock(f"document_async_task_lock_{task_id}", timeout=UPLOAD_LOCK_TIMEOUT)
    with lock:
        with session_factory.create_session() as session:
            task = DocumentAsyncTaskService.get_by_id(session, task_id)
            if task is None:
                logger.warning("Document async upload task not found before processing: %s", task_id)
                return "missing"
            # Every attempt that reaches the worker is counted, including the two
            # short-circuit replays below. Manual retries re-enter this path, so
            # skipping the increment here would silently drift `attempt_count`.
            task.attempt_count = (task.attempt_count or 0) + 1
            task.last_error_code = None
            task.next_attempt_at = None
            session.add(task)
            session.commit()
            if task.status == DocumentAsyncTaskStatus.SUCCESS:
                return "already_succeeded"
            if task.status == DocumentAsyncTaskStatus.PROCESSING:
                # A previous attempt already claimed the row. Re-entering would
                # resolve another version and dispatch duplicate indexing tasks.
                return "already_processing"

            task.status = DocumentAsyncTaskStatus.PROCESSING
            task.processing_started_at = naive_utc_now()
            task.last_error = None
            session.add(task)
            session.commit()

        context = _prepare_document(task_id)
        if context is None:
            # The uploaded content matches the document's current version, so
            # there is nothing to index.
            _mark_success(task_id)
            return "already_up_to_date"

        document_id, is_new_document, dataset_ref = context
        DocumentService.dispatch_document_indexing(
            DocumentIndexingJobs(
                dataset_ref,
                created=(document_id,) if is_new_document else (),
                duplicated=() if is_new_document else (document_id,),
            )
        )
        _mark_success(task_id, document_id=document_id)
        logger.info(
            "Document async upload dispatched indexing, task_id=%s document_id=%s dataset_id=%s",
            task_id,
            document_id,
            dataset_ref.dataset_id,
        )
        return "indexing_dispatched"


def _prepare_document(task_id: str) -> tuple[str, bool, DatasetRef] | None:
    """Rebuild the trusted document and resolve its version.

    Returns ``None`` when the uploaded content matches the document's current
    version, so the caller can complete the task without reindexing.
    """
    with session_factory.create_session() as session:
        task = DocumentAsyncTaskService.get_by_id(session, task_id)
        if task is None:
            raise ValueError(f"document async upload task not found: {task_id}")
        dataset = DatasetService.get_dataset_for_tenant(task.dataset_id, task.tenant_id, session=session)
        if dataset is None:
            raise ValueError(f"dataset not found for async upload task: {task_id}")

        # The stored payload is transport data: rebuild trusted configuration
        # from validated models before any document write.
        payload = dict(task.payload or {})
        knowledge_config = KnowledgeConfig.model_validate(payload.get("knowledge_config", {}))
        knowledge_config = DocumentService.prepare_document_creation(
            DocumentService.document_creation_dataset(dataset), knowledge_config
        )
        DatasetService.check_doc_form(dataset, knowledge_config.doc_form, session=session)
        DocumentService.apply_indexing_configuration(dataset, knowledge_config, session=session)

        upload_file = DocumentService.get_upload_file(
            tenant_id=task.tenant_id, upload_file_id=task.file_id, session=session
        )
        file_hash = DocumentVersionService.compute_file_sha256(storage.load(upload_file.key))
        return DocumentService.create_or_update_document_for_async_task(
            dataset=dataset,
            knowledge_config=knowledge_config,
            tenant_id=task.tenant_id,
            operator_id=task.created_by,
            operator_name=DocumentService.get_operator_name(task.created_by, session=session),
            upload_file_id=upload_file.id,
            file_name=upload_file.name,
            file_size=upload_file.size,
            file_hash=file_hash,
            session=session,
        )


def _mark_success(task_id: str, *, document_id: str | None = None) -> None:
    with session_factory.create_session() as session:
        task = DocumentAsyncTaskService.get_by_id(session, task_id)
        if task is None:
            return
        task.status = DocumentAsyncTaskStatus.SUCCESS
        task.completed_at = naive_utc_now()
        task.last_error = None
        task.last_error_code = None
        task.next_attempt_at = None
        if document_id is not None:
            task.document_id = document_id
        if task.document_id is not None:
            task.version_id = session.scalar(select(Document.current_version_id).where(Document.id == task.document_id))
        session.add(task)
        session.commit()


def _record_attempt_failure(task_id: str, error: Exception, *, next_attempt_at: datetime | None = None) -> None:
    with session_factory.create_session() as session:
        task = DocumentAsyncTaskService.get_by_id(session, task_id)
        if task is None:
            return
        task.status = DocumentAsyncTaskStatus.FAILED
        task.last_error = str(error)
        task.last_error_code = error_code_for(error)
        task.next_attempt_at = next_attempt_at
        session.add(task)
        session.commit()


def _mark_dead_letter(task_id: str, error: Exception) -> None:
    with session_factory.create_session() as session:
        task = DocumentAsyncTaskService.get_by_id(session, task_id)
        if task is None:
            return
        document_id = task.document_id
        _write_dead_letter(session, task, error)
    if document_id is None:
        return
    with session_factory.create_session() as session:
        _mark_document_error(session, document_id, str(error))


def _write_dead_letter(session: Session, task: DocumentAsyncTask | None, error: Exception) -> None:
    if task is None:
        return
    task.status = DocumentAsyncTaskStatus.DEAD_LETTER
    task.last_error = str(error)
    task.last_error_code = error_code_for(error)
    task.next_attempt_at = None
    task.completed_at = naive_utc_now()
    session.add(task)
    session.commit()


def _mark_document_error(session: Session, document_id: str, error: str) -> None:
    document = session.scalar(select(Document).where(Document.id == document_id))
    if document is None:
        return
    DocumentService.mark_document_indexing_error(document, error, session=session)
    session.commit()
