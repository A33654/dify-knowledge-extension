"""Dataset annotation business logic for knowledge bases.

Owns the persistence rules for dataset annotations: every annotation is scoped
to a dataset inside the current workspace, and all reads and writes verify that
scope before touching the row. HTTP concerns are handled by the console
controller; export formatting is kept here so both console and future APIs can
reuse it.
"""

import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.exceptions import NotFound

from libs.pagination import PaginatedResult, paginate_query
from models.dataset import Dataset, DatasetAnnotation
from models.enums import FeedbackRating

logger = logging.getLogger(__name__)


class DatasetAnnotationService:
    @staticmethod
    def _require_dataset(session: Session, *, dataset_id: str, tenant_id: str) -> Dataset:
        """Return the workspace-owned dataset or raise NotFound.

        This is the workspace isolation boundary: a dataset from another tenant
        is indistinguishable from a missing dataset and therefore returns 404.
        """
        dataset = session.scalar(
            select(Dataset).where(Dataset.id == dataset_id, Dataset.tenant_id == tenant_id).limit(1)
        )
        if dataset is None:
            raise NotFound("Dataset not found")
        return dataset

    @classmethod
    def create_annotation(
        cls,
        *,
        session: Session,
        tenant_id: str,
        account_id: str,
        dataset_id: str,
        question: str,
        answer: str,
        tags: list[str],
        feedback_type: FeedbackRating | None,
    ) -> DatasetAnnotation:
        cls._require_dataset(session, dataset_id=dataset_id, tenant_id=tenant_id)
        annotation = DatasetAnnotation(
            tenant_id=tenant_id,
            dataset_id=dataset_id,
            question=question,
            answer=answer,
            tags=list(tags or []),
            feedback_type=feedback_type,
            created_by=account_id,
        )
        session.add(annotation)
        return annotation

    @classmethod
    def update_annotation(
        cls,
        *,
        session: Session,
        tenant_id: str,
        dataset_id: str,
        annotation_id: str,
        question: str | None = None,
        answer: str | None = None,
        tags: list[str] | None = None,
        feedback_type: FeedbackRating | None = None,
    ) -> DatasetAnnotation:
        cls._require_dataset(session, dataset_id=dataset_id, tenant_id=tenant_id)
        annotation = session.scalar(
            select(DatasetAnnotation)
            .where(
                DatasetAnnotation.id == annotation_id,
                DatasetAnnotation.dataset_id == dataset_id,
                DatasetAnnotation.tenant_id == tenant_id,
            )
            .limit(1)
        )
        if annotation is None:
            raise NotFound("Annotation not found")

        if question is not None:
            annotation.question = question
        if answer is not None:
            annotation.answer = answer
        if tags is not None:
            annotation.tags = list(tags)
        if feedback_type is not None:
            annotation.feedback_type = feedback_type

        return annotation

    @classmethod
    def delete_annotation(
        cls,
        *,
        session: Session,
        tenant_id: str,
        dataset_id: str,
        annotation_id: str,
    ) -> None:
        cls._require_dataset(session, dataset_id=dataset_id, tenant_id=tenant_id)
        annotation = session.scalar(
            select(DatasetAnnotation)
            .where(
                DatasetAnnotation.id == annotation_id,
                DatasetAnnotation.dataset_id == dataset_id,
                DatasetAnnotation.tenant_id == tenant_id,
            )
            .limit(1)
        )
        if annotation is None:
            raise NotFound("Annotation not found")
        session.delete(annotation)

    @classmethod
    def list_annotations(
        cls,
        *,
        session: Session,
        tenant_id: str,
        dataset_id: str,
        page: int,
        limit: int,
        tag: str | None = None,
        feedback_type: FeedbackRating | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> PaginatedResult[DatasetAnnotation]:
        cls._require_dataset(session, dataset_id=dataset_id, tenant_id=tenant_id)

        stmt = select(DatasetAnnotation).where(
            DatasetAnnotation.tenant_id == tenant_id,
            DatasetAnnotation.dataset_id == dataset_id,
        )
        if tag:
            stmt = stmt.where(DatasetAnnotation.tags.contains([tag]))
        if feedback_type is not None:
            stmt = stmt.where(DatasetAnnotation.feedback_type == feedback_type)
        if start_time is not None:
            stmt = stmt.where(DatasetAnnotation.created_at >= start_time)
        if end_time is not None:
            stmt = stmt.where(DatasetAnnotation.created_at <= end_time)

        stmt = stmt.order_by(DatasetAnnotation.created_at.desc())
        return paginate_query(stmt, session=session, page=page, per_page=limit)

    @classmethod
    def export_annotations(
        cls,
        *,
        session: Session,
        tenant_id: str,
        dataset_id: str,
        export_type: str,
    ) -> str:
        """Serialize annotations as SFT JSONL or an evaluation JSON document."""
        cls._require_dataset(session, dataset_id=dataset_id, tenant_id=tenant_id)
        annotations = session.scalars(
            select(DatasetAnnotation)
            .where(DatasetAnnotation.tenant_id == tenant_id, DatasetAnnotation.dataset_id == dataset_id)
            .order_by(DatasetAnnotation.created_at.desc())
        ).all()

        if export_type == "sft":
            lines = [
                json.dumps(
                    {
                        "messages": [
                            {"role": "user", "content": annotation.question},
                            {"role": "assistant", "content": annotation.answer},
                        ]
                    },
                    ensure_ascii=False,
                )
                for annotation in annotations
            ]
            return "\n".join(lines) + ("\n" if lines else "")

        data = [
            {
                "id": annotation.id,
                "question": annotation.question,
                "answer": annotation.answer,
                "tags": annotation.tags or [],
                "feedback_type": annotation.feedback_type.value if annotation.feedback_type else None,
                "created_by": annotation.created_by,
                "created_at": annotation.created_at.isoformat() if annotation.created_at else None,
            }
            for annotation in annotations
        ]
        return json.dumps({"dataset_id": dataset_id, "data": data}, ensure_ascii=False, indent=2)
