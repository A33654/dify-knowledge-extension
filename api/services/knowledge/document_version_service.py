from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from extensions.ext_redis import redis_client
from libs.datetime_utils import naive_utc_now
from models.dataset import Document, DocumentSegment, DocumentVersion
from services.knowledge.document_version_cache import (
    CACHED_NULL,
    LOCK_BLOCKING_TIMEOUT_SECONDS,
    LOCK_TTL_SECONDS,
    DocumentVersionCache,
)

# Metadata keys that may carry a user supplied business identifier. When present,
# the value is used to correlate successive uploads of the same logical document.
BUSINESS_KEY_METADATA_KEYS = ("business_id", "business_key", "businessId", "businessKey")


class DocumentVersionService:
    """Resolve and persist document versions bound to segments.

    A document is identified by ``business_key`` (defaults to the file/document
    name). A new version is created whenever the resolved file hash differs from
    the hash of the latest version; an identical hash reuses the current version
    and does not produce duplicate rows.
    """

    @staticmethod
    def compute_file_sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def get_business_key(document_name: str, doc_metadata: dict[str, Any] | None) -> str:
        if doc_metadata:
            for key in BUSINESS_KEY_METADATA_KEYS:
                value = doc_metadata.get(key)
                if value not in (None, ""):
                    return str(value)[:255]
        return (document_name or "")[:255]

    @staticmethod
    def get_latest_version(
        session: Session,
        *,
        tenant_id: str,
        dataset_id: str,
        document_id: str,
        business_key: str,
        for_update: bool = False,
    ) -> DocumentVersion | None:
        stmt = (
            select(DocumentVersion)
            .where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.dataset_id == dataset_id,
                DocumentVersion.document_id == document_id,
                DocumentVersion.business_key == business_key,
            )
            .order_by(DocumentVersion.version.desc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return session.scalar(stmt)

    @staticmethod
    def get_version(
        session: Session,
        *,
        tenant_id: str,
        dataset_id: str,
        version_id: str,
    ) -> DocumentVersion | None:
        return session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.id == version_id,
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.dataset_id == dataset_id,
            )
        )

    @staticmethod
    def list_versions(
        session: Session,
        *,
        tenant_id: str,
        dataset_id: str,
        document_id: str,
    ) -> list[DocumentVersion]:
        stmt = (
            select(DocumentVersion)
            .where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.dataset_id == dataset_id,
                DocumentVersion.document_id == document_id,
            )
            .order_by(DocumentVersion.version.desc())
        )
        return list(session.scalars(stmt).all())

    @staticmethod
    def version_summary(version: DocumentVersion) -> dict[str, Any]:
        return DocumentVersionCache.serialize_version(version)

    @staticmethod
    def list_version_summaries(
        session: Session,
        *,
        tenant_id: str,
        dataset_id: str,
        document_id: str,
    ) -> list[dict[str, Any]]:
        """Return the version list, reading through the cache with single-flight.

        The returned dicts carry the same metadata exposed by the list endpoint.
        Original text, vectors and segment details are intentionally not cached.
        """
        cached = DocumentVersionCache.get_version_list(
            tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id
        )
        if cached is not None:
            return cached

        lock = redis_client.lock(
            DocumentVersionCache.version_list_lock_key(
                tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id
            ),
            timeout=LOCK_TTL_SECONDS,
            blocking_timeout=LOCK_BLOCKING_TIMEOUT_SECONDS,
        )
        try:
            acquired = lock.acquire(blocking=True)
        except Exception:
            return DocumentVersionService._list_version_summaries_from_db(
                session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id
            )
        if not acquired:
            return DocumentVersionService._list_version_summaries_from_db(
                session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id
            )
        try:
            cached = DocumentVersionCache.get_version_list(
                tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id
            )
            if cached is not None:
                return cached
            summaries = DocumentVersionService._list_version_summaries_from_db(
                session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id
            )
            DocumentVersionCache.set_version_list(
                tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id, summaries=summaries
            )
            return summaries
        finally:
            lock.release()

    @staticmethod
    def _list_version_summaries_from_db(
        session: Session, *, tenant_id: str, dataset_id: str, document_id: str
    ) -> list[dict[str, Any]]:
        versions = DocumentVersionService.list_versions(
            session, tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id
        )
        return [DocumentVersionService.version_summary(version) for version in versions]

    @staticmethod
    def get_version_summary(
        session: Session,
        *,
        tenant_id: str,
        dataset_id: str,
        version_id: str,
    ) -> dict[str, Any] | None:
        """Return a single version summary, reading through the cache with single-flight."""
        cached: Any = DocumentVersionCache.get_version_summary(
            tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id
        )
        if cached == CACHED_NULL:
            return None
        if cached is not None:
            return cached

        lock = redis_client.lock(
            DocumentVersionCache.version_summary_lock_key(
                tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id
            ),
            timeout=LOCK_TTL_SECONDS,
            blocking_timeout=LOCK_BLOCKING_TIMEOUT_SECONDS,
        )
        try:
            acquired = lock.acquire(blocking=True)
        except Exception:
            return DocumentVersionService._get_version_summary_from_db(
                session, tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id
            )
        if not acquired:
            return DocumentVersionService._get_version_summary_from_db(
                session, tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id
            )
        try:
            cached = DocumentVersionCache.get_version_summary(
                tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id
            )
            if cached == CACHED_NULL:
                return None
            if cached is not None:
                return cached
            summary = DocumentVersionService._get_version_summary_from_db(
                session, tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id
            )
            DocumentVersionCache.set_version_summary(
                tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id, summary=summary
            )
            return summary
        finally:
            lock.release()

    @staticmethod
    def _get_version_summary_from_db(
        session: Session, *, tenant_id: str, dataset_id: str, version_id: str
    ) -> dict[str, Any] | None:
        version = DocumentVersionService.get_version(
            session, tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id
        )
        return DocumentVersionService.version_summary(version) if version else None

    @staticmethod
    def resolve_or_create_version(
        session: Session,
        *,
        document: Document,
        file_hash: str | None,
        file_id: str | None = None,
        file_name: str | None = None,
        file_size: int | None = None,
        word_count: int | None = None,
        content: str | None = None,
        operator_id: str,
    ) -> tuple[DocumentVersion, bool]:
        business_key = DocumentVersionService.get_business_key(document.name, document.doc_metadata)

        # Serialize version resolution per document. Concurrent uploads of the
        # same document must not both observe the same latest version and insert
        # duplicate rows. Locking the document row forces the second transaction
        # to wait until the first commit is visible before resolving a version.
        session.execute(select(Document.id).where(Document.id == document.id).with_for_update())

        # Identical content reuses an existing version of the same document,
        # regardless of whether it is currently the latest version.
        existing: DocumentVersion | None = None
        if file_hash:
            existing = session.scalar(
                select(DocumentVersion)
                .where(
                    DocumentVersion.tenant_id == document.tenant_id,
                    DocumentVersion.dataset_id == document.dataset_id,
                    DocumentVersion.document_id == document.id,
                    DocumentVersion.file_hash == file_hash,
                )
                .order_by(DocumentVersion.version.asc())
                .limit(1)
                .with_for_update()
            )
        if existing is not None:
            if existing.is_latest:
                document.current_version_id = existing.id
            else:
                DocumentVersionService._promote_version(session, document=document, version=existing)
            return existing, False

        latest = DocumentVersionService.get_latest_version(
            session,
            tenant_id=document.tenant_id,
            dataset_id=document.dataset_id,
            document_id=document.id,
            business_key=business_key,
            for_update=True,
        )

        version_number = (latest.version + 1) if latest is not None else 1

        # Previous version is archived and stops participating in default retrieval.
        if latest is not None:
            latest.is_latest = False
            latest.archived = True
            DocumentVersionService._disable_segments(session, version_id=latest.id)

        document_version = DocumentVersion(
            tenant_id=document.tenant_id,
            dataset_id=document.dataset_id,
            document_id=document.id,
            version=version_number,
            business_key=business_key,
            file_hash=file_hash or "",
            file_id=file_id,
            file_name=file_name,
            file_size=file_size,
            word_count=word_count,
            content=content,
            is_latest=True,
            archived=False,
            created_by=operator_id,
        )
        session.add(document_version)
        session.flush()
        document.current_version_id = document_version.id

        affected_version_ids = [document_version.id]
        if latest is not None:
            affected_version_ids.append(latest.id)
        DocumentVersionService._schedule_cache_invalidation(
            session,
            tenant_id=document.tenant_id,
            dataset_id=document.dataset_id,
            document_id=document.id,
            version_ids=affected_version_ids,
        )
        return document_version, True

    @staticmethod
    def _promote_version(session: Session, *, document: Document, version: DocumentVersion) -> None:
        """Promote an existing version to latest and archive all other versions."""
        others = session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.tenant_id == document.tenant_id,
                DocumentVersion.dataset_id == document.dataset_id,
                DocumentVersion.document_id == document.id,
                DocumentVersion.id != version.id,
            )
        ).all()
        for other in others:
            other.is_latest = False
            other.archived = True
            DocumentVersionService._disable_segments(session, version_id=other.id)
        version.is_latest = True
        version.archived = False
        DocumentVersionService._enable_segments(session, version_id=version.id)
        if version.word_count is not None:
            document.word_count = version.word_count
        session.add(version)
        document.current_version_id = version.id

        affected_version_ids = [version.id, *(other.id for other in others)]
        DocumentVersionService._schedule_cache_invalidation(
            session,
            tenant_id=document.tenant_id,
            dataset_id=document.dataset_id,
            document_id=document.id,
            version_ids=affected_version_ids,
        )

    @staticmethod
    def _disable_segments(session: Session, *, version_id: str) -> None:
        session.execute(
            update(DocumentSegment)
            .where(DocumentSegment.version_id == version_id)
            .values(enabled=False, disabled_at=naive_utc_now())
        )

    @staticmethod
    def _enable_segments(session: Session, *, version_id: str) -> None:
        session.execute(
            update(DocumentSegment)
            .where(DocumentSegment.version_id == version_id)
            .values(enabled=True, disabled_at=None)
        )

    @staticmethod
    def set_version_archived(
        session: Session,
        *,
        version: DocumentVersion,
        archived: bool,
    ) -> DocumentVersion:
        """Set the archive flag for a non-latest version.

        Archived versions are excluded from default retrieval while remaining
        readable for historical conversation tracing. The latest version cannot
        be archived so a document always keeps at least one retrievable version.
        """
        if version.is_latest and archived:
            raise ValueError("The latest version cannot be archived.")
        version.archived = archived
        if archived:
            DocumentVersionService._disable_segments(session, version_id=version.id)
        else:
            DocumentVersionService._enable_segments(session, version_id=version.id)
        session.add(version)

        DocumentVersionService._schedule_cache_invalidation(
            session,
            tenant_id=version.tenant_id,
            dataset_id=version.dataset_id,
            document_id=version.document_id,
            version_ids=[version.id],
        )
        return version

    @staticmethod
    def get_version_original_text(session: Session, *, tenant_id: str, version: DocumentVersion) -> str:
        """Return the original text for a version.

        Prefers the stored content snapshot; falls back to reconstructing the text
        from the version's segments ordered by position.
        """
        return DocumentVersionService.get_version_content(
            session, tenant_id=tenant_id, dataset_id=version.dataset_id, version_id=version.id
        )

    @staticmethod
    def get_version_content(session: Session, *, tenant_id: str, dataset_id: str, version_id: str) -> str:
        """Return the original text for a version without going through the cache.

        Original text may be large and may fall back to segment content, so it is
        intentionally never cached.
        """
        content = session.scalar(
            select(DocumentVersion.content).where(
                DocumentVersion.id == version_id,
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.dataset_id == dataset_id,
            )
        )
        if content:
            return content

        chunks = list(
            session.scalars(
                select(DocumentSegment.content)
                .where(
                    DocumentSegment.tenant_id == tenant_id,
                    DocumentSegment.version_id == version_id,
                )
                .order_by(DocumentSegment.position.asc())
            ).all()
        )
        return "\n".join(chunks) if chunks else ""

    @staticmethod
    def _schedule_cache_invalidation(
        session: Session,
        *,
        tenant_id: str,
        dataset_id: str,
        document_id: str,
        version_ids: list[str],
    ) -> None:
        """Delete cache keys after the caller's transaction commits.

        Invalidation must run after commit so concurrent readers cannot repopulate
        the cache from the pre-write state. The callback is registered with
        ``once=True`` and performs no I/O when the transaction rolls back.
        """

        def invalidate(_session: Session) -> None:
            DocumentVersionCache.delete_version_list(
                tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id
            )
            for version_id in version_ids:
                DocumentVersionCache.delete_version_summary(
                    tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id
                )

        event.listen(session, "after_commit", invalidate, once=True)
