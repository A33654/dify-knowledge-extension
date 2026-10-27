"""Acceptance tests for the document-version read-through cache.

The cache layer is exercised through an in-memory Redis stand-in so hit/miss and
invalidation can be asserted without a Redis server. The three behaviours the
cache was introduced for are pinned down here:

1. Repeated reads of the same version list hit the database exactly once.
2. Creating a version invalidates the key, so the next read sees the new data.
3. A cold key under concurrent access is loaded by exactly one caller, so a
   stampede cannot reach the database.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from redis.exceptions import RedisError
from sqlalchemy.orm import Session, sessionmaker

from models.dataset import Dataset, Document, DocumentVersion
from models.enums import DataSourceType, DocumentCreatedFrom
from services.knowledge import document_version_cache as cache_module
from services.knowledge import document_version_service as service_module
from services.knowledge.document_version_cache import (
    CACHED_NULL,
    VERSION_LIST_TTL_SECONDS,
    VERSION_NULL_TTL_SECONDS,
    VERSION_SUMMARY_TTL_SECONDS,
    DocumentVersionCache,
)
from services.knowledge.document_version_service import DocumentVersionService

VersionSummary = dict[str, object]


class _FakeLock:
    """Stand-in for ``redis.lock`` giving real mutual exclusion without Redis."""

    def __init__(self, guard: threading.Lock, timeout: int | None, blocking_timeout: float | None) -> None:
        self._guard = guard
        self._timeout = timeout
        self._blocking_timeout = blocking_timeout

    def acquire(self, blocking: bool = True, blocking_timeout: float | None = None) -> bool:
        if not blocking:
            return self._guard.acquire(blocking=False)
        timeout = blocking_timeout if blocking_timeout is not None else self._blocking_timeout
        return self._guard.acquire(timeout=5 if timeout is None else timeout)

    def release(self) -> None:
        self._guard.release()


class FakeRedis:
    """Minimal in-memory Redis covering only what the version cache uses."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._guard:
            return self.store.get(key)

    def setex(self, key: str, ttl: int, value: bytes) -> None:
        with self._guard:
            self.store[key] = value
            self.ttls[key] = ttl

    def delete(self, key: str) -> None:
        with self._guard:
            self.store.pop(key, None)
            self.ttls.pop(key, None)

    def lock(self, name: str, timeout: int | None = None, blocking_timeout: float | None = None) -> _FakeLock:
        with self._guard:
            guard = self._locks.setdefault(name, threading.Lock())
        return _FakeLock(guard, timeout, blocking_timeout)


class DbReadCounter:
    """Count real database round trips performed by the version read paths."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[str] = []

    def record(self, label: str) -> None:
        with self._lock:
            self._calls.append(label)

    def count(self, label: str) -> int:
        with self._lock:
            return self._calls.count(label)

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()


@pytest.fixture
def fake_redis() -> Iterator[FakeRedis]:
    """Swap the Redis client used by both the cache and the service for a fake."""
    redis = FakeRedis()
    with (
        patch.object(cache_module, "redis_client", redis),
        patch.object(service_module, "redis_client", redis),
    ):
        yield redis


@pytest.fixture
def db_reads() -> Iterator[DbReadCounter]:
    """Instrument the two database read helpers without changing their behaviour."""
    counter = DbReadCounter()
    original_list = DocumentVersionService.list_versions
    original_get = DocumentVersionService.get_version

    def counting_list(session: Session, *, tenant_id: str, dataset_id: str, document_id: str) -> list[DocumentVersion]:
        counter.record("list")
        return original_list(session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id)

    def counting_get(session: Session, *, tenant_id: str, dataset_id: str, version_id: str) -> DocumentVersion | None:
        counter.record("get")
        return original_get(session, tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id)

    with (
        patch.object(DocumentVersionService, "list_versions", counting_list),
        patch.object(DocumentVersionService, "get_version", counting_get),
    ):
        yield counter


def _make_dataset(session: Session, *, tenant_id: str, dataset_id: str, operator_id: str) -> None:
    session.add(
        Dataset(
            id=dataset_id,
            tenant_id=tenant_id,
            name=f"ds-{dataset_id}",
            indexing_technique="high_quality",
            created_by=operator_id,
        )
    )


def _make_document(session: Session, *, tenant_id: str, dataset_id: str, name: str = "notes.txt") -> Document:
    document = Document(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        dataset_id=dataset_id,
        position=1,
        data_source_type=DataSourceType.UPLOAD_FILE,
        batch="batch",
        name=name,
        created_from=DocumentCreatedFrom.WEB,
        created_by=str(uuid.uuid4()),
    )
    session.add(document)
    session.flush()
    return document


def _seed_document(session: Session) -> tuple[str, str, str, Document]:
    """Create one dataset with one document and return the ids used for lookups."""
    tenant_id = str(uuid.uuid4())
    dataset_id = str(uuid.uuid4())
    operator_id = str(uuid.uuid4())
    _make_dataset(session, tenant_id=tenant_id, dataset_id=dataset_id, operator_id=operator_id)
    document = _make_document(session, tenant_id=tenant_id, dataset_id=dataset_id)
    return tenant_id, dataset_id, operator_id, document


class TestDocumentVersionCacheReadThrough:
    """Case 1 - a warm key is served from Redis without touching the database."""

    def test_repeated_list_reads_hit_database_once(
        self, sqlite_session: Session, fake_redis: FakeRedis, db_reads: DbReadCounter
    ) -> None:
        tenant_id, dataset_id, operator_id, document = _seed_document(sqlite_session)
        for file_hash in ("hash-a", "hash-b"):
            DocumentVersionService.resolve_or_create_version(
                sqlite_session, document=document, file_hash=file_hash, operator_id=operator_id
            )
        sqlite_session.commit()
        db_reads.reset()

        results = [
            DocumentVersionService.list_version_summaries(
                sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
            )
            for _ in range(5)
        ]

        assert db_reads.count("list") == 1
        assert all(result == results[0] for result in results)
        assert [item["version"] for item in results[0]] == [2, 1]

        list_key = DocumentVersionCache.version_list_key(
            tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )
        assert fake_redis.get(list_key) is not None
        assert fake_redis.ttls[list_key] == VERSION_LIST_TTL_SECONDS

    def test_repeated_summary_reads_hit_database_once(
        self, sqlite_session: Session, fake_redis: FakeRedis, db_reads: DbReadCounter
    ) -> None:
        tenant_id, dataset_id, operator_id, document = _seed_document(sqlite_session)
        version, _ = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        sqlite_session.commit()
        db_reads.reset()

        results = [
            DocumentVersionService.get_version_summary(
                sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, version_id=version.id
            )
            for _ in range(5)
        ]

        assert db_reads.count("get") == 1
        assert all(result == results[0] for result in results)
        first: VersionSummary | None = results[0]
        assert first is not None
        assert first["version"] == 1

        summary_key = DocumentVersionCache.version_summary_key(
            tenant_id=tenant_id, dataset_id=dataset_id, version_id=version.id
        )
        assert fake_redis.get(summary_key) is not None
        assert fake_redis.ttls[summary_key] == VERSION_SUMMARY_TTL_SECONDS

    def test_missing_version_is_cached_as_null(
        self, sqlite_session: Session, fake_redis: FakeRedis, db_reads: DbReadCounter
    ) -> None:
        """A 404 lookup is cached too, so repeated misses cannot saturate the DB."""
        tenant_id = str(uuid.uuid4())
        dataset_id = str(uuid.uuid4())
        missing_version_id = str(uuid.uuid4())

        for _ in range(3):
            summary = DocumentVersionService.get_version_summary(
                sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, version_id=missing_version_id
            )
            assert summary is None

        assert db_reads.count("get") == 1
        key = DocumentVersionCache.version_summary_key(
            tenant_id=tenant_id, dataset_id=dataset_id, version_id=missing_version_id
        )
        assert fake_redis.get(key) == CACHED_NULL.encode("utf-8")
        assert fake_redis.ttls[key] == VERSION_NULL_TTL_SECONDS


class TestDocumentVersionCacheInvalidation:
    """Case 2 - the write path deletes keys so the next read observes new data."""

    def test_new_version_invalidates_cached_list(
        self, sqlite_session: Session, fake_redis: FakeRedis, db_reads: DbReadCounter
    ) -> None:
        tenant_id, dataset_id, operator_id, document = _seed_document(sqlite_session)
        list_key = DocumentVersionCache.version_list_key(
            tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )

        DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        sqlite_session.commit()
        db_reads.reset()

        first = DocumentVersionService.list_version_summaries(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )
        assert [item["version"] for item in first] == [1]
        assert fake_redis.get(list_key) is not None

        # Warm read: still no extra database round trip.
        DocumentVersionService.list_version_summaries(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )
        assert db_reads.count("list") == 1

        DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-b", operator_id=operator_id
        )
        sqlite_session.commit()

        # Invalidation runs only after commit, never on a rollback.
        assert fake_redis.get(list_key) is None

        second = DocumentVersionService.list_version_summaries(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )
        assert [item["version"] for item in second] == [2, 1]
        assert db_reads.count("list") == 2

    def test_rollback_keeps_cache_intact(self, sqlite_session: Session, fake_redis: FakeRedis) -> None:
        """A failed write must not evict a key that still reflects committed data."""
        tenant_id, dataset_id, operator_id, document = _seed_document(sqlite_session)
        list_key = DocumentVersionCache.version_list_key(
            tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )

        DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        sqlite_session.commit()
        DocumentVersionService.list_version_summaries(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )
        assert fake_redis.get(list_key) is not None

        DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-b", operator_id=operator_id
        )
        sqlite_session.rollback()

        assert fake_redis.get(list_key) is not None

    def test_archiving_version_invalidates_summary(self, sqlite_session: Session, fake_redis: FakeRedis) -> None:
        tenant_id, dataset_id, operator_id, document = _seed_document(sqlite_session)
        first, _ = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-b", operator_id=operator_id
        )
        sqlite_session.commit()

        summary_key = DocumentVersionCache.version_summary_key(
            tenant_id=tenant_id, dataset_id=dataset_id, version_id=first.id
        )
        archived = DocumentVersionService.get_version_summary(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, version_id=first.id
        )
        assert archived is not None
        assert archived["archived"] is True
        assert fake_redis.get(summary_key) is not None

        DocumentVersionService.set_version_archived(sqlite_session, version=first, archived=False)
        sqlite_session.commit()

        assert fake_redis.get(summary_key) is None
        restored = DocumentVersionService.get_version_summary(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, version_id=first.id
        )
        assert restored is not None
        assert restored["archived"] is False


class TestDocumentVersionCacheConcurrency:
    """Case 3 - a cold key is loaded by a single caller under concurrent access."""

    def test_cold_key_under_concurrency_loads_once(
        self,
        sqlite_session: Session,
        sqlite_session_factory: sessionmaker[Session],
        fake_redis: FakeRedis,
        db_reads: DbReadCounter,
    ) -> None:
        tenant_id, dataset_id, operator_id, document = _seed_document(sqlite_session)
        DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        sqlite_session.commit()
        db_reads.reset()

        thread_count = 8
        barrier = threading.Barrier(thread_count)
        results: list[list[VersionSummary] | None] = [None] * thread_count
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            # Each thread owns its session: SQLAlchemy sessions are not shareable.
            with sqlite_session_factory() as session:
                try:
                    barrier.wait()
                    results[index] = DocumentVersionService.list_version_summaries(
                        session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
                    )
                except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert db_reads.count("list") == 1
        assert all(result == results[0] for result in results)
        winner = results[0]
        assert winner is not None
        assert [item["version"] for item in winner] == [1]

        list_key = DocumentVersionCache.version_list_key(
            tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )
        assert fake_redis.get(list_key) is not None


class TestDocumentVersionCacheKeys:
    """Key layout, tenant isolation and the TTL safety net."""

    def test_keys_carry_tenant_and_dataset(self) -> None:
        list_key = DocumentVersionCache.version_list_key(
            tenant_id="tenant-1", dataset_id="dataset-1", document_id="doc-1"
        )
        summary_key = DocumentVersionCache.version_summary_key(
            tenant_id="tenant-1", dataset_id="dataset-1", version_id="ver-1"
        )

        assert "tenant-1" in list_key
        assert "dataset-1" in list_key
        assert "doc-1" in list_key
        assert "tenant-1" in summary_key
        assert "dataset-1" in summary_key
        assert "ver-1" in summary_key
        assert list_key != summary_key

        other_tenant = DocumentVersionCache.version_list_key(
            tenant_id="tenant-2", dataset_id="dataset-1", document_id="doc-1"
        )
        other_dataset = DocumentVersionCache.version_list_key(
            tenant_id="tenant-1", dataset_id="dataset-2", document_id="doc-1"
        )
        assert len({list_key, other_tenant, other_dataset}) == 3

    def test_datasets_do_not_share_cached_lists(self, sqlite_session: Session, fake_redis: FakeRedis) -> None:
        tenant_id = str(uuid.uuid4())
        operator_id = str(uuid.uuid4())
        dataset_one = str(uuid.uuid4())
        dataset_two = str(uuid.uuid4())
        _make_dataset(sqlite_session, tenant_id=tenant_id, dataset_id=dataset_one, operator_id=operator_id)
        _make_dataset(sqlite_session, tenant_id=tenant_id, dataset_id=dataset_two, operator_id=operator_id)

        document_one = _make_document(sqlite_session, tenant_id=tenant_id, dataset_id=dataset_one, name="one.txt")
        document_two = _make_document(sqlite_session, tenant_id=tenant_id, dataset_id=dataset_two, name="two.txt")
        for document in (document_one, document_two):
            DocumentVersionService.resolve_or_create_version(
                sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
            )
            DocumentVersionService.resolve_or_create_version(
                sqlite_session, document=document, file_hash="hash-b", operator_id=operator_id
            )
        sqlite_session.commit()

        one = DocumentVersionService.list_version_summaries(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_one, document_id=document_one.id
        )
        two = DocumentVersionService.list_version_summaries(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_two, document_id=document_two.id
        )

        assert len(one) == 2
        assert len(two) == 2
        for item in one:
            assert item["document_id"] == document_one.id
            assert item["dataset_id"] == dataset_one
        for item in two:
            assert item["document_id"] == document_two.id
            assert item["dataset_id"] == dataset_two

        key_one = DocumentVersionCache.version_list_key(
            tenant_id=tenant_id, dataset_id=dataset_one, document_id=document_one.id
        )
        key_two = DocumentVersionCache.version_list_key(
            tenant_id=tenant_id, dataset_id=dataset_two, document_id=document_two.id
        )
        assert fake_redis.get(key_one) is not None
        assert fake_redis.get(key_two) is not None

    def test_cached_payload_excludes_vectors_and_segments(self, sqlite_session: Session, fake_redis: FakeRedis) -> None:
        tenant_id, dataset_id, operator_id, document = _seed_document(sqlite_session)
        DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id, content="original text"
        )
        sqlite_session.commit()
        DocumentVersionService.list_version_summaries(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )

        raw = fake_redis.get(
            DocumentVersionCache.version_list_key(tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id)
        )
        assert raw is not None
        payload = raw.decode("utf-8")
        assert "content" not in payload
        assert "embedding" not in payload
        assert "index_node_id" not in payload
        assert "segment" not in payload

    def test_cached_values_always_expire(self, sqlite_session: Session, fake_redis: FakeRedis) -> None:
        """Every write carries a TTL so a missed delete still self-heals."""
        tenant_id, dataset_id, operator_id, document = _seed_document(sqlite_session)
        version, _ = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        sqlite_session.commit()
        DocumentVersionService.list_version_summaries(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )
        DocumentVersionService.get_version_summary(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, version_id=version.id
        )

        list_key = DocumentVersionCache.version_list_key(
            tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )
        summary_key = DocumentVersionCache.version_summary_key(
            tenant_id=tenant_id, dataset_id=dataset_id, version_id=version.id
        )
        assert fake_redis.ttls[list_key] == VERSION_LIST_TTL_SECONDS
        assert fake_redis.ttls[summary_key] == VERSION_SUMMARY_TTL_SECONDS

    def test_redis_outage_degrades_to_database(
        self, sqlite_session: Session, fake_redis: FakeRedis, db_reads: DbReadCounter
    ) -> None:
        """Redis failures must surface as a cache miss, never as a request error."""
        tenant_id, dataset_id, operator_id, document = _seed_document(sqlite_session)
        DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        sqlite_session.commit()
        db_reads.reset()

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RedisError("redis is down")

        fake_redis.get = boom  # type: ignore[method-assign]
        fake_redis.setex = boom  # type: ignore[method-assign]

        result = DocumentVersionService.list_version_summaries(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document.id
        )

        assert [item["version"] for item in result] == [1]
        assert db_reads.count("list") == 1
