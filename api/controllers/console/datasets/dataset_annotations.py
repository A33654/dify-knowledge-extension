from datetime import UTC, datetime
from functools import wraps
from typing import Literal
from uuid import UUID

from flask import Response
from flask_restx import Resource
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from werkzeug.exceptions import Forbidden

from configs import dify_config
from controllers.common.rbac import DatasetId, RBACCheck
from controllers.common.schema import query_params_from_model, register_response_schema_models, register_schema_models
from controllers.common.session import with_session
from controllers.console import console_ns
from controllers.console.wraps import (
    RBACPermission,
    account_initialization_required,
    model_validate,
    rbac_permission_required,
    setup_required,
    with_current_tenant_id,
    with_current_user,
)
from enums.account import TenantAccountRole
from fields.dataset_annotation_fields import DatasetAnnotationListResponse, DatasetAnnotationResponse
from libs.helper import dump_response
from libs.login import login_required
from models import Account
from models.enums import FeedbackRating
from services.knowledge.annotation_service import DatasetAnnotationService

_ANNOTATION_ADMIN_ROLES = frozenset(
    {TenantAccountRole.OWNER, TenantAccountRole.ADMIN, TenantAccountRole.DATASET_OPERATOR}
)


def _require_dataset_annotation_admin(view):
    """Allow only knowledge-base admins when RBAC is disabled.

    RBAC-enabled deployments are handled by ``rbac_permission_required`` with a
    dataset-scoped permission check, where roles are replaced by fine-grained
    permissions.
    """

    @wraps(view)
    def decorated(self, current_user: Account, *args, **kwargs):
        if not dify_config.RBAC_ENABLED and current_user.role not in _ANNOTATION_ADMIN_ROLES:
            raise Forbidden()
        return view(self, current_user, *args, **kwargs)

    return decorated


class DatasetAnnotationCreatePayload(BaseModel):
    question: str = Field(..., min_length=1, max_length=100000)
    answer: str = Field(..., min_length=1, max_length=100000)
    tags: list[str] = Field(default_factory=list)
    feedback_type: Literal["like", "dislike"] | None = None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str]) -> list[str]:
        return _normalize_tags(tags)


class DatasetAnnotationUpdatePayload(BaseModel):
    question: str | None = Field(None, min_length=1, max_length=100000)
    answer: str | None = Field(None, min_length=1, max_length=100000)
    tags: list[str] | None = None
    feedback_type: Literal["like", "dislike"] | None = None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str] | None) -> list[str] | None:
        if tags is None:
            return None
        return _normalize_tags(tags)


class DatasetAnnotationListQuery(BaseModel):
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=20, ge=1, le=100)
    tag: str | None = Field(default=None, max_length=64)
    feedback_type: Literal["like", "dislike"] | None = None
    start_time: int | None = Field(default=None, ge=0)
    end_time: int | None = Field(default=None, ge=0)


class DatasetAnnotationExportQuery(BaseModel):
    type: Literal["sft", "eval"] = "sft"


def _normalize_tags(tags: list[str]) -> list[str]:
    cleaned: list[str] = []
    for tag in tags:
        normalized = tag.strip()
        if normalized and normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned


def _to_naive_utc(timestamp: int | None) -> datetime | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).replace(tzinfo=None)


register_schema_models(
    console_ns,
    DatasetAnnotationCreatePayload,
    DatasetAnnotationUpdatePayload,
    DatasetAnnotationListQuery,
    DatasetAnnotationExportQuery,
)
register_response_schema_models(console_ns, DatasetAnnotationResponse, DatasetAnnotationListResponse)


@console_ns.route("/datasets/<uuid:dataset_id>/annotations")
class DatasetAnnotationListCreateApi(Resource):
    @console_ns.doc("list_dataset_annotations")
    @console_ns.doc(description="List annotations for a dataset")
    @console_ns.doc(params={"dataset_id": "Dataset ID", **query_params_from_model(DatasetAnnotationListQuery)})
    @console_ns.response(200, "Annotations retrieved", console_ns.models[DatasetAnnotationListResponse.__name__])
    @console_ns.response(403, "Insufficient permissions")
    @setup_required
    @login_required
    @account_initialization_required
    @with_current_tenant_id
    @with_current_user
    @_require_dataset_annotation_admin
    @rbac_permission_required(RBACCheck(RBACPermission.DATASET_EDIT, DatasetId()))
    @with_session(write=False)
    @model_validate(DatasetAnnotationListQuery)
    def get(
        self,
        req_data: DatasetAnnotationListQuery,
        session: Session,
        current_user: Account,
        current_tenant_id: str,
        dataset_id: UUID,
    ):
        result = DatasetAnnotationService.list_annotations(
            session=session,
            tenant_id=current_tenant_id,
            dataset_id=str(dataset_id),
            page=req_data.page,
            limit=req_data.limit,
            tag=req_data.tag,
            feedback_type=FeedbackRating(req_data.feedback_type) if req_data.feedback_type else None,
            start_time=_to_naive_utc(req_data.start_time),
            end_time=_to_naive_utc(req_data.end_time),
        )
        return dump_response(
            DatasetAnnotationListResponse,
            {
                "data": result.items,
                "has_more": result.has_next,
                "limit": result.per_page,
                "total": result.total,
                "page": result.page,
            },
        ), 200

    @console_ns.doc("create_dataset_annotation")
    @console_ns.doc(description="Create an annotation for a dataset")
    @console_ns.doc(params={"dataset_id": "Dataset ID"})
    @console_ns.expect(console_ns.models[DatasetAnnotationCreatePayload.__name__])
    @console_ns.response(201, "Annotation created", console_ns.models[DatasetAnnotationResponse.__name__])
    @console_ns.response(403, "Insufficient permissions")
    @setup_required
    @login_required
    @account_initialization_required
    @with_current_tenant_id
    @with_current_user
    @_require_dataset_annotation_admin
    @rbac_permission_required(RBACCheck(RBACPermission.DATASET_EDIT, DatasetId()))
    @with_session
    @model_validate(DatasetAnnotationCreatePayload)
    def post(
        self,
        req_data: DatasetAnnotationCreatePayload,
        session: Session,
        current_user: Account,
        current_tenant_id: str,
        dataset_id: UUID,
    ):
        annotation = DatasetAnnotationService.create_annotation(
            session=session,
            tenant_id=current_tenant_id,
            account_id=str(current_user.id),
            dataset_id=str(dataset_id),
            question=req_data.question,
            answer=req_data.answer,
            tags=req_data.tags,
            feedback_type=FeedbackRating(req_data.feedback_type) if req_data.feedback_type else None,
        )
        return dump_response(DatasetAnnotationResponse, annotation), 201


@console_ns.route("/datasets/<uuid:dataset_id>/annotations/export")
class DatasetAnnotationExportApi(Resource):
    @console_ns.doc("export_dataset_annotations")
    @console_ns.doc(description="Export dataset annotations as SFT JSONL or evaluation JSON")
    @console_ns.doc(params={"dataset_id": "Dataset ID", **query_params_from_model(DatasetAnnotationExportQuery)})
    @console_ns.response(200, "Annotations exported")
    @console_ns.response(403, "Insufficient permissions")
    @setup_required
    @login_required
    @account_initialization_required
    @with_current_tenant_id
    @with_current_user
    @_require_dataset_annotation_admin
    @rbac_permission_required(RBACCheck(RBACPermission.DATASET_EDIT, DatasetId()))
    @with_session(write=False)
    @model_validate(DatasetAnnotationExportQuery)
    def get(
        self,
        req_data: DatasetAnnotationExportQuery,
        session: Session,
        current_user: Account,
        current_tenant_id: str,
        dataset_id: UUID,
    ):
        content = DatasetAnnotationService.export_annotations(
            session=session,
            tenant_id=current_tenant_id,
            dataset_id=str(dataset_id),
            export_type=req_data.type,
        )
        if req_data.type == "sft":
            content_type = "application/x-ndjson"
            extension = "jsonl"
        else:
            content_type = "application/json"
            extension = "json"
        filename = f"dataset_{dataset_id}_annotations.{extension}"
        return Response(
            content,
            content_type=f"{content_type}; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


@console_ns.route("/datasets/<uuid:dataset_id>/annotations/<uuid:annotation_id>")
class DatasetAnnotationDetailApi(Resource):
    @console_ns.doc("update_dataset_annotation")
    @console_ns.doc(description="Update a dataset annotation")
    @console_ns.doc(params={"dataset_id": "Dataset ID", "annotation_id": "Annotation ID"})
    @console_ns.expect(console_ns.models[DatasetAnnotationUpdatePayload.__name__])
    @console_ns.response(200, "Annotation updated", console_ns.models[DatasetAnnotationResponse.__name__])
    @console_ns.response(403, "Insufficient permissions")
    @setup_required
    @login_required
    @account_initialization_required
    @with_current_tenant_id
    @with_current_user
    @_require_dataset_annotation_admin
    @rbac_permission_required(RBACCheck(RBACPermission.DATASET_EDIT, DatasetId()))
    @with_session
    @model_validate(DatasetAnnotationUpdatePayload)
    def patch(
        self,
        req_data: DatasetAnnotationUpdatePayload,
        session: Session,
        current_user: Account,
        current_tenant_id: str,
        dataset_id: UUID,
        annotation_id: UUID,
    ):
        annotation = DatasetAnnotationService.update_annotation(
            session=session,
            tenant_id=current_tenant_id,
            dataset_id=str(dataset_id),
            annotation_id=str(annotation_id),
            question=req_data.question,
            answer=req_data.answer,
            tags=req_data.tags,
            feedback_type=FeedbackRating(req_data.feedback_type) if req_data.feedback_type else None,
        )
        return dump_response(DatasetAnnotationResponse, annotation), 200

    @console_ns.doc("delete_dataset_annotation")
    @console_ns.doc(description="Delete a dataset annotation")
    @console_ns.doc(params={"dataset_id": "Dataset ID", "annotation_id": "Annotation ID"})
    @console_ns.response(204, "Annotation deleted")
    @console_ns.response(403, "Insufficient permissions")
    @setup_required
    @login_required
    @account_initialization_required
    @with_current_tenant_id
    @with_current_user
    @_require_dataset_annotation_admin
    @rbac_permission_required(RBACCheck(RBACPermission.DATASET_EDIT, DatasetId()))
    @with_session
    def delete(
        self,
        session: Session,
        current_user: Account,
        current_tenant_id: str,
        dataset_id: UUID,
        annotation_id: UUID,
    ):
        DatasetAnnotationService.delete_annotation(
            session=session,
            tenant_id=current_tenant_id,
            dataset_id=str(dataset_id),
            annotation_id=str(annotation_id),
        )
        return "", 204
