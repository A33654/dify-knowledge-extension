import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.dataset import Dataset, Document, DocumentSegment, DocumentVersion
from models.enums import DataSourceType, DocumentCreatedFrom
from services.knowledge.document_version_service import DocumentVersionService


def _make_document(session: Session, *, tenant_id: str, dataset_id: str) -> Document:
    document = Document(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        dataset_id=dataset_id,
        position=1,
        data_source_type=DataSourceType.UPLOAD_FILE,
        batch="batch",
        name="notes.txt",
        created_from=DocumentCreatedFrom.WEB,
        created_by=str(uuid.uuid4()),
    )
    session.add(document)
    session.flush()
    return document


class TestDocumentVersionService:
    def test_resolve_creates_and_reuses_identical_hash(self, sqlite_session: Session) -> None:
        tenant_id = str(uuid.uuid4())
        dataset_id = str(uuid.uuid4())
        operator_id = str(uuid.uuid4())
        sqlite_session.add(
            Dataset(
                id=dataset_id, tenant_id=tenant_id, name="ds", indexing_technique="high_quality", created_by=operator_id
            )
        )
        document = _make_document(sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id)

        version, created = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        assert created is True
        assert version.version == 1
        assert version.is_latest is True

        reused, created = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        assert created is False
        assert reused.id == version.id

        versions = sqlite_session.scalars(select(DocumentVersion).order_by(DocumentVersion.version)).all()
        assert len(versions) == 1

    def test_resolve_archives_previous_and_promotes_old_content(self, sqlite_session: Session) -> None:
        tenant_id = str(uuid.uuid4())
        dataset_id = str(uuid.uuid4())
        operator_id = str(uuid.uuid4())
        sqlite_session.add(
            Dataset(
                id=dataset_id, tenant_id=tenant_id, name="ds", indexing_technique="high_quality", created_by=operator_id
            )
        )
        document = _make_document(sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id)

        first, _ = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        second, _ = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-b", operator_id=operator_id
        )
        assert second.version == 2
        assert second.is_latest is True

        sqlite_session.flush()
        sqlite_session.refresh(first)
        assert first.is_latest is False
        assert first.archived is True

        promoted, created = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        assert created is False
        assert promoted.id == first.id
        assert promoted.is_latest is True
        assert promoted.archived is False

        sqlite_session.refresh(second)
        assert second.is_latest is False
        assert second.archived is True

    def test_latest_version_cannot_be_archived(self, sqlite_session: Session) -> None:
        tenant_id = str(uuid.uuid4())
        dataset_id = str(uuid.uuid4())
        operator_id = str(uuid.uuid4())
        sqlite_session.add(
            Dataset(
                id=dataset_id, tenant_id=tenant_id, name="ds", indexing_technique="high_quality", created_by=operator_id
            )
        )
        document = _make_document(sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id)
        version, _ = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )

        with pytest.raises(ValueError, match="latest version cannot be archived"):
            DocumentVersionService.set_version_archived(sqlite_session, version=version, archived=True)

    def test_archive_disables_and_enables_segments(self, sqlite_session: Session) -> None:
        tenant_id = str(uuid.uuid4())
        dataset_id = str(uuid.uuid4())
        operator_id = str(uuid.uuid4())
        sqlite_session.add(
            Dataset(
                id=dataset_id, tenant_id=tenant_id, name="ds", indexing_technique="high_quality", created_by=operator_id
            )
        )
        document = _make_document(sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id)
        first, _ = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        second, _ = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-b", operator_id=operator_id
        )
        segment = DocumentSegment(
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            document_id=document.id,
            version_id=first.id,
            position=1,
            content="old",
            word_count=1,
            tokens=1,
            created_by=operator_id,
        )
        sqlite_session.add(segment)
        sqlite_session.commit()

        DocumentVersionService.set_version_archived(sqlite_session, version=first, archived=True)
        sqlite_session.commit()
        sqlite_session.refresh(segment)
        assert segment.enabled is False

        DocumentVersionService.set_version_archived(sqlite_session, version=first, archived=False)
        sqlite_session.commit()
        sqlite_session.refresh(segment)
        assert segment.enabled is True

    def test_version_reads_are_scoped_by_dataset(self, sqlite_session: Session) -> None:
        tenant_id = str(uuid.uuid4())
        operator_id = str(uuid.uuid4())
        dataset_a = str(uuid.uuid4())
        dataset_b = str(uuid.uuid4())
        sqlite_session.add_all(
            [
                Dataset(
                    id=dataset_a,
                    tenant_id=tenant_id,
                    name="a",
                    indexing_technique="high_quality",
                    created_by=operator_id,
                ),
                Dataset(
                    id=dataset_b,
                    tenant_id=tenant_id,
                    name="b",
                    indexing_technique="high_quality",
                    created_by=operator_id,
                ),
            ]
        )
        document = _make_document(sqlite_session, tenant_id=tenant_id, dataset_id=dataset_a)
        version, _ = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )

        assert (
            DocumentVersionService.get_version(
                sqlite_session, tenant_id=tenant_id, dataset_id=dataset_a, version_id=version.id
            )
            is not None
        )
        assert (
            DocumentVersionService.get_version(
                sqlite_session, tenant_id=tenant_id, dataset_id=dataset_b, version_id=version.id
            )
            is None
        )
        assert DocumentVersionService.list_versions(
            sqlite_session, tenant_id=tenant_id, dataset_id=dataset_a, document_id=document.id
        ) == [version]
        assert (
            DocumentVersionService.list_versions(
                sqlite_session, tenant_id=tenant_id, dataset_id=dataset_b, document_id=document.id
            )
            == []
        )

    def test_original_text_falls_back_to_segments(self, sqlite_session: Session) -> None:
        tenant_id = str(uuid.uuid4())
        dataset_id = str(uuid.uuid4())
        operator_id = str(uuid.uuid4())
        sqlite_session.add(
            Dataset(
                id=dataset_id, tenant_id=tenant_id, name="ds", indexing_technique="high_quality", created_by=operator_id
            )
        )
        document = _make_document(sqlite_session, tenant_id=tenant_id, dataset_id=dataset_id)
        version, _ = DocumentVersionService.resolve_or_create_version(
            sqlite_session, document=document, file_hash="hash-a", operator_id=operator_id
        )
        sqlite_session.add_all(
            [
                DocumentSegment(
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    document_id=document.id,
                    version_id=version.id,
                    position=1,
                    content="line-one",
                    word_count=1,
                    tokens=1,
                    created_by=operator_id,
                ),
                DocumentSegment(
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    document_id=document.id,
                    version_id=version.id,
                    position=2,
                    content="line-two",
                    word_count=1,
                    tokens=1,
                    created_by=operator_id,
                ),
            ]
        )
        sqlite_session.commit()

        assert (
            DocumentVersionService.get_version_original_text(sqlite_session, tenant_id=tenant_id, version=version)
            == "line-one\nline-two"
        )
