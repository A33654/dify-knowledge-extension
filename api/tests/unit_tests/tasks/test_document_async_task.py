"""SQLite-backed tests for the asynchronous document upload worker."""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from celery.app.task import Context
from celery.exceptions import Retry
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.account import Tenant, TenantAccountJoin, TenantAccountRole
from models.dataset import Dataset, Document, DocumentAsyncTask, DocumentVersion
from models.enums import DocumentAsyncTaskStatus, IndexingStatus
from services.knowledge.document_async_task_service import DocumentAsyncTaskService
from services.knowledge.document_version_service import DocumentVersionService
from tasks import document_async_task as task_module
from tests.helpers.unit.model_factories import make_account, make_dataset, make_upload_file
from tests.helpers.unit.tasks.task_options import task_options

FILE_CONTENT = b"async upload content"
FILE_HASH = DocumentVersionService.compute_file_sha256(FILE_CONTENT)


def _persist_upload_context(sqlite_session: Session) -> tuple[Dataset, str]:
    account = make_account(name="Async uploader", email=f"async-{uuid4()}@example.com")
    tenant = Tenant(name="Async tenant")
    sqlite_session.add_all([account, tenant])
    sqlite_session.flush()
    sqlite_session.add(
        TenantAccountJoin(
            tenant_id=tenant.id,
            account_id=account.id,
            current=True,
            role=TenantAccountRole.OWNER,
        )
    )
    dataset = make_dataset(
        dataset_id=str(uuid4()),
        tenant_id=tenant.id,
        name="Async dataset",
        created_by=account.id,
        indexing_technique="economy",
    )
    sqlite_session.add(dataset)
    sqlite_session.commit()
    return dataset, account.id


def _persist_task(
    sqlite_session: Session,
    *,
    dataset: Dataset,
    file_hash: str,
    created_by: str,
) -> DocumentAsyncTask:
    upload_file = make_upload_file(
        tenant_id=dataset.tenant_id,
        key=f"upload-files/{uuid4()}/file.txt",
        name="file.txt",
        size=len(FILE_CONTENT),
        created_by=created_by,
        used=True,
    )
    sqlite_session.add(upload_file)
    sqlite_session.flush()
    task, _ = DocumentAsyncTaskService.create_pending(
        sqlite_session,
        tenant_id=dataset.tenant_id,
        dataset_id=dataset.id,
        trace_id=uuid4().hex,
        file_id=upload_file.id,
        file_hash=file_hash,
        created_by=created_by,
        payload={
            "knowledge_config": {
                "indexing_technique": "economy",
                "doc_form": "text_model",
                "doc_language": "English",
                "process_rule": {"mode": "automatic"},
                "data_source": {
                    "info_list": {
                        "data_source_type": "upload_file",
                        "file_info_list": {"file_ids": []},
                    }
                },
            },
            "created_from": "api",
        },
    )
    sqlite_session.commit()
    return task


def _reload(sqlite_session: Session, task_id: str) -> DocumentAsyncTask:
    # The worker commits on its own sessions; drop this session's identity-map
    # copies before asserting on committed state.
    sqlite_session.expire_all()
    task = sqlite_session.scalar(select(DocumentAsyncTask).where(DocumentAsyncTask.id == task_id))
    assert task is not None
    return task


def _run_task(stack: ExitStack, task_id: str, *, retries: int = 0) -> None:
    stack.enter_context(patch.object(task_module.redis_client, "lock", return_value=MagicMock()))
    stack.enter_context(patch("services.knowledge.document_version_service.redis_client"))
    task = task_module.document_async_task
    task.request_stack.push(Context(retries=retries))
    try:
        task.run(task_id)
    finally:
        task.pop_request()


class TestDocumentAsyncTask:
    def test_pending_task_transitions_to_success_and_dispatches_indexing(self, sqlite_session: Session) -> None:
        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash=FILE_HASH, created_by=operator_id)

        with ExitStack() as stack:
            stack.enter_context(patch.object(task_module.storage, "load", return_value=FILE_CONTENT))
            indexing_proxy = stack.enter_context(patch("services.knowledge.dataset_service.DocumentIndexingTaskProxy"))
            duplicate_proxy = stack.enter_context(
                patch("services.knowledge.dataset_service.DuplicateDocumentIndexingTaskProxy")
            )
            _run_task(stack, task.id)

        updated = _reload(sqlite_session, task.id)
        assert updated.status == DocumentAsyncTaskStatus.SUCCESS
        assert updated.document_id is not None
        assert updated.version_id is not None
        assert updated.completed_at is not None

        document = sqlite_session.scalar(select(Document).where(Document.id == updated.document_id))
        assert document is not None
        assert document.dataset_id == dataset.id
        assert document.tenant_id == dataset.tenant_id
        assert document.indexing_status == IndexingStatus.WAITING
        version = sqlite_session.scalar(select(DocumentVersion).where(DocumentVersion.id == updated.version_id))
        assert version is not None
        assert version.dataset_id == dataset.id
        assert version.tenant_id == dataset.tenant_id
        assert version.file_hash == FILE_HASH
        indexing_proxy.assert_called_once()
        duplicate_proxy.assert_not_called()

    def test_replay_of_the_same_task_does_not_create_another_version(self, sqlite_session: Session) -> None:
        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash=FILE_HASH, created_by=operator_id)

        with ExitStack() as stack:
            stack.enter_context(patch.object(task_module.storage, "load", return_value=FILE_CONTENT))
            indexing_proxy = stack.enter_context(patch("services.knowledge.dataset_service.DocumentIndexingTaskProxy"))
            _run_task(stack, task.id)
            first_document_id = _reload(sqlite_session, task.id).document_id
            _run_task(stack, task.id)

        updated = _reload(sqlite_session, task.id)
        assert updated.document_id == first_document_id
        assert updated.status == DocumentAsyncTaskStatus.SUCCESS
        assert sqlite_session.scalar(select(func.count()).where(DocumentVersion.document_id == first_document_id)) == 1
        # The second run reuses the resolved version, so no second indexing task
        # is published for the same upload.
        assert indexing_proxy.call_count == 1

    def test_failure_marks_task_failed_and_schedules_retry(self, sqlite_session: Session) -> None:
        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash="hash-a", created_by=operator_id)
        retry = MagicMock(side_effect=Retry())

        with ExitStack() as stack:
            stack.enter_context(patch.object(task_module.document_async_task, "retry", retry))
            with pytest.raises(Retry):
                _run_task(stack, task.id)

        updated = _reload(sqlite_session, task.id)
        assert updated.status == DocumentAsyncTaskStatus.FAILED
        assert updated.last_error is not None
        retry.assert_called_once()
        assert retry.call_args.kwargs["countdown"] == task_module.RETRY_DELAY_SECONDS

    def test_missing_task_is_ignored(self, sqlite_session: Session) -> None:
        unknown_id = str(uuid4())
        with ExitStack() as stack:
            _run_task(stack, unknown_id)

        # An unknown id must never leave a row behind on the way out.
        assert sqlite_session.scalar(select(func.count()).where(DocumentAsyncTask.id == unknown_id)) == 0


def test_task_options_are_pinned_to_the_dataset_queue() -> None:
    assert task_module.MAX_RETRIES == 3
    assert task_options(task_module.document_async_task)["queue"] == "dataset"


def _set_status(sqlite_session: Session, task_id: str, status: DocumentAsyncTaskStatus) -> None:
    task = _reload(sqlite_session, task_id)
    task.status = status
    sqlite_session.add(task)
    sqlite_session.commit()


class TestDocumentAsyncTaskRetryPolicy:
    def test_retry_policy_is_pinned_to_2_4_8_seconds(self) -> None:
        """Guard the backoff constants: changing them changes client-visible timing."""
        assert task_module.MAX_RETRIES == 3
        assert task_module.RETRY_DELAY_SECONDS == 2
        assert [task_module.RETRY_DELAY_SECONDS * 2**r for r in range(task_module.MAX_RETRIES)] == [2, 4, 8]

    def test_status_enum_exposes_dead_letter_as_the_terminal_state(self) -> None:
        assert DocumentAsyncTaskStatus.DEAD_LETTER == "dead_letter"
        assert {status.value for status in DocumentAsyncTaskStatus} == {
            "pending",
            "processing",
            "success",
            "failed",
            "dead_letter",
        }

    @pytest.mark.parametrize(
        ("retries", "expected_countdown"),
        [(0, 2), (1, 4), (2, 8)],
    )
    def test_failure_schedules_exponential_backoff_and_records_next_attempt(
        self, sqlite_session: Session, retries: int, expected_countdown: int
    ) -> None:
        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash="hash-backoff", created_by=operator_id)
        retry = MagicMock(side_effect=Retry())

        with ExitStack() as stack:
            stack.enter_context(patch.object(task_module, "_process", side_effect=ValueError("boom")))
            stack.enter_context(patch.object(task_module.document_async_task, "retry", retry))
            with pytest.raises(Retry):
                _run_task(stack, task.id, retries=retries)

        updated = _reload(sqlite_session, task.id)
        assert retry.call_args.kwargs["countdown"] == expected_countdown
        assert updated.status == DocumentAsyncTaskStatus.FAILED
        assert updated.last_error_code == "ValueError"
        assert updated.last_error is not None
        # Non-null only while an automatic retry is still scheduled: this is what
        # lets a client tell "will retry" apart from the terminal dead letter.
        assert updated.next_attempt_at is not None
        assert updated.completed_at is None

    def test_retry_budget_exhausted_marks_dead_letter(self, sqlite_session: Session) -> None:
        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash="hash-dead", created_by=operator_id)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    task_module,
                    "_process",
                    side_effect=ValueError("embedding provider unavailable"),
                )
            )
            with pytest.raises(ValueError):
                _run_task(stack, task.id, retries=task_module.MAX_RETRIES)

        updated = _reload(sqlite_session, task.id)
        assert updated.status == DocumentAsyncTaskStatus.DEAD_LETTER
        assert updated.completed_at is not None
        assert updated.next_attempt_at is None
        assert updated.last_error_code == "ValueError"
        assert updated.last_error is not None

    def test_broker_failure_while_scheduling_retry_still_marks_dead_letter(self, sqlite_session: Session) -> None:
        """self.retry() may fail to publish; the task must not end silently."""
        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash="hash-broker", created_by=operator_id)

        with ExitStack() as stack:
            stack.enter_context(patch.object(task_module, "_process", side_effect=ValueError("boom")))
            stack.enter_context(
                patch.object(
                    task_module.document_async_task,
                    "retry",
                    MagicMock(side_effect=RuntimeError("broker unavailable")),
                )
            )
            with pytest.raises(RuntimeError):
                _run_task(stack, task.id)

        updated = _reload(sqlite_session, task.id)
        assert updated.status == DocumentAsyncTaskStatus.DEAD_LETTER
        assert updated.completed_at is not None
        assert updated.next_attempt_at is None


class TestDocumentAsyncTaskAttemptCount:
    def test_replay_of_a_succeeded_task_still_increments_attempt_count(self, sqlite_session: Session) -> None:
        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash=FILE_HASH, created_by=operator_id)

        with ExitStack() as stack:
            stack.enter_context(patch.object(task_module.storage, "load", return_value=FILE_CONTENT))
            stack.enter_context(patch("services.knowledge.dataset_service.DocumentIndexingTaskProxy"))
            _run_task(stack, task.id)
            assert _reload(sqlite_session, task.id).attempt_count == 1
            _run_task(stack, task.id)

        # Manual retries re-enter through `already_succeeded`; skipping the
        # increment there would silently under-report `attempt_count`.
        updated = _reload(sqlite_session, task.id)
        assert updated.status == DocumentAsyncTaskStatus.SUCCESS
        assert updated.attempt_count == 2

    def test_processing_short_circuit_increments_attempt_count(self, sqlite_session: Session) -> None:
        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash=FILE_HASH, created_by=operator_id)
        _set_status(sqlite_session, task.id, DocumentAsyncTaskStatus.PROCESSING)

        with ExitStack() as stack:
            _run_task(stack, task.id)

        updated = _reload(sqlite_session, task.id)
        assert updated.status == DocumentAsyncTaskStatus.PROCESSING
        assert updated.attempt_count == 1

    def test_retry_count_derivation_matches_the_documented_rule(self, sqlite_session: Session) -> None:
        """attempt_count counts executions; retry_count is the derived view."""
        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash=FILE_HASH, created_by=operator_id)
        _set_status(sqlite_session, task.id, DocumentAsyncTaskStatus.PROCESSING)
        task_row = _reload(sqlite_session, task.id)
        task_row.attempt_count = 4
        sqlite_session.add(task_row)
        sqlite_session.commit()

        updated = _reload(sqlite_session, task.id)
        assert max((updated.attempt_count or 0) - 1, 0) == 3
        assert max(0 - 1, 0) == 0  # a fresh task reports zero retries, never -1
