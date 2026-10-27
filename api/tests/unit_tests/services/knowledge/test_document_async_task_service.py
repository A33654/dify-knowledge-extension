"""Unit tests for asynchronous document upload task rows."""

from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.dataset import DocumentAsyncTask
from models.enums import DocumentAsyncTaskStatus
from services.knowledge.document_async_task_service import DocumentAsyncTaskService


def _create(
    session: Session,
    *,
    tenant_id: str = "tenant-1",
    dataset_id: str = "dataset-1",
    trace_id: str = "trace-1",
) -> tuple[DocumentAsyncTask, bool]:
    return DocumentAsyncTaskService.create_pending(
        session,
        tenant_id=tenant_id,
        dataset_id=dataset_id,
        trace_id=trace_id,
        file_id=str(uuid4()),
        file_hash="hash-a",
        created_by="user-1",
        payload={"knowledge_config": {"indexing_technique": "economy"}},
    )


class TestDocumentAsyncTaskService:
    def test_repeated_trace_id_reuses_one_pending_row(self, sqlite_session: Session) -> None:
        first, first_created = _create(sqlite_session)
        sqlite_session.commit()
        second, second_created = _create(sqlite_session)

        assert first_created is True
        assert second_created is False
        assert second.id == first.id
        assert second.status == DocumentAsyncTaskStatus.PENDING
        assert sqlite_session.scalar(select(func.count()).select_from(DocumentAsyncTask)) == 1

    def test_trace_id_is_scoped_by_dataset(self, sqlite_session: Session) -> None:
        first, _ = _create(sqlite_session, dataset_id="dataset-1")
        other, created = _create(sqlite_session, dataset_id="dataset-2")

        assert created is True
        assert other.id != first.id

    def test_resolve_trace_id_keeps_valid_values_and_generates_missing_ones(self) -> None:
        assert DocumentAsyncTaskService.resolve_trace_id("client-1") == "client-1"
        assert DocumentAsyncTaskService.resolve_trace_id(None) != DocumentAsyncTaskService.resolve_trace_id(None)
        assert DocumentAsyncTaskService.resolve_trace_id("invalid/trace") is not None
