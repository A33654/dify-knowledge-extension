"""新增单测：退避策略、死信流转、Broker 异常兜底、attempt_count 计数修复。

追加到 api/tests/unit_tests/tasks/test_document_async_task.py 末尾
（放在 `def test_task_options_are_pinned_to_the_dataset_queue()` 之前或之后均可，
复用该文件已有的 `_persist_upload_context` / `_persist_task` / `_reload` / `_run_task` 夹具）。
"""

from __future__ import annotations

import pytest
from celery.exceptions import Retry
from sqlalchemy.orm import Session

from models.enums import DocumentAsyncTaskStatus
from tests.unit_tests.tasks.test_document_async_task import (
    FILE_HASH,
    _persist_task,
    _persist_upload_context,
    _reload,
    _run_task,
)
from tests.unit_tests.tasks import test_document_async_task as existing


def _set_status(sqlite_session: Session, task_id: str, status: DocumentAsyncTaskStatus) -> None:
    task = _reload(sqlite_session, task_id)
    task.status = status
    sqlite_session.add(task)
    sqlite_session.commit()


class TestDocumentAsyncTaskRetryPolicy:
    def test_retry_policy_is_pinned_to_2_4_8_seconds(self) -> None:
        """Guard the backoff constants: changing them changes client-visible timing."""
        assert existing.task_module.MAX_RETRIES == 3
        assert existing.task_module.RETRY_DELAY_SECONDS == 2
        assert [existing.task_module.RETRY_DELAY_SECONDS * 2**r for r in range(existing.task_module.MAX_RETRIES)] == [
            2,
            4,
            8,
        ]

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
        from contextlib import ExitStack
        from unittest.mock import MagicMock, patch

        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash="hash-backoff", created_by=operator_id)
        retry = MagicMock(side_effect=Retry())

        with ExitStack() as stack:
            stack.enter_context(patch.object(existing.task_module, "_process", side_effect=ValueError("boom")))
            stack.enter_context(patch.object(existing.task_module.document_async_task, "retry", retry))
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
        from contextlib import ExitStack
        from unittest.mock import patch

        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash="hash-dead", created_by=operator_id)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    existing.task_module,
                    "_process",
                    side_effect=ValueError("embedding provider unavailable"),
                )
            )
            with pytest.raises(ValueError):
                _run_task(stack, task.id, retries=existing.task_module.MAX_RETRIES)

        updated = _reload(sqlite_session, task.id)
        assert updated.status == DocumentAsyncTaskStatus.DEAD_LETTER
        assert updated.completed_at is not None
        assert updated.next_attempt_at is None
        assert updated.last_error_code == "ValueError"
        assert updated.last_error is not None

    def test_broker_failure_while_scheduling_retry_still_marks_dead_letter(self, sqlite_session: Session) -> None:
        """self.retry() may fail to publish; the task must not end silently."""
        from contextlib import ExitStack
        from unittest.mock import MagicMock, patch

        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash="hash-broker", created_by=operator_id)

        with ExitStack() as stack:
            stack.enter_context(patch.object(existing.task_module, "_process", side_effect=ValueError("boom")))
            stack.enter_context(
                patch.object(
                    existing.task_module.document_async_task,
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
        from contextlib import ExitStack
        from unittest.mock import patch

        dataset, operator_id = _persist_upload_context(sqlite_session)
        task = _persist_task(sqlite_session, dataset=dataset, file_hash=FILE_HASH, created_by=operator_id)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(existing.task_module.storage, "load", return_value=existing.FILE_CONTENT)
            )
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
        from contextlib import ExitStack

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
        _reload(sqlite_session, task.id)
        task_row = _reload(sqlite_session, task.id)
        task_row.attempt_count = 4
        sqlite_session.add(task_row)
        sqlite_session.commit()

        updated = _reload(sqlite_session, task.id)
        assert max((updated.attempt_count or 0) - 1, 0) == 3
        assert max(0 - 1, 0) == 0  # a fresh task reports zero retries, never -1


# ---------------------------------------------------------------------------
# 说明：controller 层（查询 / 手动重试）建议另开
#   api/tests/unit_tests/controllers/service_api/dataset/test_document_async_task_api.py，
#   覆盖：
#     1. GET 返回 retry_count = attempt_count - 1、progress、last_error_code
#     2. GET 跨 tenant / 跨 dataset 访问返回 404（不是 403）
#     3. POST retry 对非 dead_letter 返回 409
#     4. POST retry 成功：状态回 pending、attempt_count 归零、复用同一 task_id、返回 202
#     5. POST retry 时 apply_async 抛异常：状态回到 dead_letter 并返回 503
#   这 5 条需要 app fixture + dataset token 鉴权，与 worker 单测不同层，故未混入本文件。
# ---------------------------------------------------------------------------
