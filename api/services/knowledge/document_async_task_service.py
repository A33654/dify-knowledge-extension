"""Asynchronous document upload task records.

The Service API upload endpoint only authenticates, stores the uploaded file,
computes its ``sha256`` and resolves a ``trace_id`` before creating one pending
``document_async_task`` row. The heavy version comparison, splitting, embedding
and vector loading then run in a Celery worker.

Idempotency relies on a database unique constraint over
``(tenant_id, dataset_id, trace_id)`` instead of an in-process check, so
concurrent replays of the same ``trace_id`` cannot create duplicate tasks.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db.session_factory import session_factory
from core.helper.trace_id_helper import is_valid_trace_id
from libs.datetime_utils import naive_utc_now
from libs.uuid_utils import uuidv7
from models.dataset import Document, DocumentAsyncTask
from models.enums import DataSourceType, DocumentAsyncTaskStatus

logger = logging.getLogger(__name__)

# A document may be indexed asynchronously, but the request still has to carry
# enough context for the worker to rebuild the original knowledge configuration.
ASYNC_UPLOAD_DATA_SOURCE_TYPE = DataSourceType.UPLOAD_FILE


def error_code_for(error: Exception) -> str:
    """Return a low-cardinality code for alerting and client-side branching."""
    code = getattr(error, "error_code", None)
    if isinstance(code, str) and code:
        return code[:255]
    return type(error).__name__[:255]


class DocumentAsyncTaskService:
    """Create, read and transition asynchronous upload task rows."""

    @staticmethod
    def resolve_trace_id(candidate: str | None) -> str:
        """Return a stable trace id, generating one when the caller omits it."""
        if candidate and is_valid_trace_id(candidate):
            return candidate
        return uuidv7().hex

    @staticmethod
    def get_by_trace_id(
        session: Session, *, tenant_id: str, dataset_id: str, trace_id: str
    ) -> DocumentAsyncTask | None:
        """Read a task scoped by its full owner chain."""
        return session.scalar(
            select(DocumentAsyncTask).where(
                DocumentAsyncTask.tenant_id == tenant_id,
                DocumentAsyncTask.dataset_id == dataset_id,
                DocumentAsyncTask.trace_id == trace_id,
            )
        )

    @staticmethod
    def get_by_id(session: Session, task_id: str) -> DocumentAsyncTask | None:
        return session.scalar(select(DocumentAsyncTask).where(DocumentAsyncTask.id == task_id))

    @staticmethod
    def create_pending(
        session: Session,
        *,
        tenant_id: str,
        dataset_id: str,
        trace_id: str,
        file_id: str,
        file_hash: str,
        created_by: str,
        payload: dict[str, Any],
    ) -> tuple[DocumentAsyncTask, bool]:
        """Insert a pending task, or return the existing one for the same trace id.

        The insert is attempted first so the database unique constraint is the
        single source of truth for idempotency. A duplicate raises
        ``IntegrityError`` inside the open transaction, so the session is rolled
        back before reading the committed winner.
        """
        task = DocumentAsyncTask(
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            trace_id=trace_id,
            file_id=file_id,
            file_hash=file_hash,
            created_by=created_by,
            payload=payload,
            status=DocumentAsyncTaskStatus.PENDING,
        )
        session.add(task)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = DocumentAsyncTaskService.get_by_trace_id(
                session, tenant_id=tenant_id, dataset_id=dataset_id, trace_id=trace_id
            )
            if existing is None:  # pragma: no cover - defensive, the unique index guarantees a row
                raise
            return existing, False
        return task, True

    @staticmethod
    def attach_document(session: Session, *, task: DocumentAsyncTask, document: Document) -> None:
        task.document_id = document.id
        task.batch = document.batch
        task.updated_at = naive_utc_now()
        session.add(task)

    # ------------------------------------------------------------------
    # Opt-in FlaskTask instrumentation. All three are safety nets: they only
    # write while the row is still owned by the worker, so a task that already
    # recorded its own outcome (success / failed / dead_letter) always wins.
    # ------------------------------------------------------------------

    @staticmethod
    def mark_execution_started(task_id: str) -> None:
        """Fill `processing_started_at` once; replays never rewrite the original."""
        with session_factory.create_session() as session:
            task = DocumentAsyncTaskService.get_by_id(session, task_id)
            if task is None or task.processing_started_at is not None:
                return
            task.processing_started_at = naive_utc_now()
            session.add(task)
            session.commit()

    @staticmethod
    def record_execution_failure(task_id: str, error: Exception) -> None:
        """Catch failures that escape before the task records them itself."""
        with session_factory.create_session() as session:
            task = DocumentAsyncTaskService.get_by_id(session, task_id)
            if task is None or task.status != DocumentAsyncTaskStatus.PROCESSING:
                return
            task.last_error = str(error)
            task.last_error_code = error_code_for(error)
            session.add(task)
            session.commit()

    @staticmethod
    def mark_execution_succeeded(task_id: str) -> None:
        with session_factory.create_session() as session:
            task = DocumentAsyncTaskService.get_by_id(session, task_id)
            if task is None or task.status != DocumentAsyncTaskStatus.PROCESSING:
                return
            task.status = DocumentAsyncTaskStatus.SUCCESS
            task.completed_at = naive_utc_now()
            task.last_error = None
            task.last_error_code = None
            task.next_attempt_at = None
            session.add(task)
            session.commit()
