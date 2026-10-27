"""Redis cache for document version list and single-version summary.

Cache-aside with a single-flight read path and write-delete-only invalidation:

- Reads check Redis first. On a miss they query the database while holding a
  distributed lock so a cold key does not stampede the database, then write the
  value back with a TTL.
- Writes never update cached values. The write path deletes the affected keys
  after the database transaction commits; the TTL is only a safety net that
  evicts stale entries if a delete is missed. This trades a brief window of
  stale reads for a much simpler consistency model.
- Vectors and segment details are intentionally never cached; only the version
  metadata shown in list/detail responses is stored here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from extensions.ext_redis import redis_client, redis_fallback
from libs.helper import to_timestamp

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "document_version"
VERSION_LIST_TTL_SECONDS = 300
VERSION_SUMMARY_TTL_SECONDS = 300
VERSION_NULL_TTL_SECONDS = 60
LOCK_TTL_SECONDS = 10
LOCK_BLOCKING_TIMEOUT_SECONDS = 5

# Cached marker for a version that does not exist, so repeated 404 lookups do
# not hit the database every time. Kept distinct from a JSON ``null`` so callers
# can tell "cached as missing" apart from a cache miss.
CACHED_NULL = "__document_version_cached_null__"


class DocumentVersionCache:
    """Serialize and access cached document-version metadata in Redis."""

    @staticmethod
    def version_list_key(*, tenant_id: str, dataset_id: str, document_id: str) -> str:
        return f"{CACHE_KEY_PREFIX}:list:{tenant_id}:{dataset_id}:{document_id}"

    @staticmethod
    def version_summary_key(*, tenant_id: str, dataset_id: str, version_id: str) -> str:
        return f"{CACHE_KEY_PREFIX}:summary:{tenant_id}:{dataset_id}:{version_id}"

    @staticmethod
    def version_list_lock_key(*, tenant_id: str, dataset_id: str, document_id: str) -> str:
        return f"{CACHE_KEY_PREFIX}:lock:list:{tenant_id}:{dataset_id}:{document_id}"

    @staticmethod
    def version_summary_lock_key(*, tenant_id: str, dataset_id: str, version_id: str) -> str:
        return f"{CACHE_KEY_PREFIX}:lock:summary:{tenant_id}:{dataset_id}:{version_id}"

    @staticmethod
    def serialize_version(version: Any) -> dict[str, Any]:
        """Serialize version metadata shared by list and detail responses.

        ``document_id`` and ``dataset_id`` are included for the endpoint
        ownership check and for debugging; response models ignore extra fields.
        """
        return {
            "id": version.id,
            "document_id": version.document_id,
            "dataset_id": version.dataset_id,
            "version": version.version,
            "business_key": version.business_key,
            "file_hash": version.file_hash,
            "file_name": version.file_name,
            "file_size": version.file_size,
            "word_count": version.word_count,
            "is_latest": version.is_latest,
            "archived": version.archived,
            "created_by": version.created_by,
            "created_at": to_timestamp(version.created_at),
        }

    @staticmethod
    def _decode(raw: bytes | str | None) -> str | None:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return raw

    @staticmethod
    @redis_fallback(default_return=None)
    def get_version_list(*, tenant_id: str, dataset_id: str, document_id: str) -> list[dict[str, Any]] | None:
        key = DocumentVersionCache.version_list_key(tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id)
        raw = DocumentVersionCache._decode(redis_client.get(key))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Failed to decode cached document version list for document %s", document_id)
            return None
        return data if isinstance(data, list) else None

    @staticmethod
    @redis_fallback(default_return=None)
    def get_version_summary(*, tenant_id: str, dataset_id: str, version_id: str) -> dict[str, Any] | str | None:
        key = DocumentVersionCache.version_summary_key(
            tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id
        )
        raw = DocumentVersionCache._decode(redis_client.get(key))
        if raw is None:
            return None
        if raw == CACHED_NULL:
            return CACHED_NULL
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Failed to decode cached document version summary for version %s", version_id)
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    @redis_fallback(default_return=False)
    def set_version_list(*, tenant_id: str, dataset_id: str, document_id: str, summaries: list[dict[str, Any]]) -> bool:
        redis_client.setex(
            DocumentVersionCache.version_list_key(tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id),
            VERSION_LIST_TTL_SECONDS,
            json.dumps(summaries).encode("utf-8"),
        )
        return True

    @staticmethod
    @redis_fallback(default_return=False)
    def set_version_summary(
        *, tenant_id: str, dataset_id: str, version_id: str, summary: dict[str, Any] | None
    ) -> bool:
        if summary is None:
            value = CACHED_NULL.encode("utf-8")
            ttl = VERSION_NULL_TTL_SECONDS
        else:
            value = json.dumps(summary).encode("utf-8")
            ttl = VERSION_SUMMARY_TTL_SECONDS
        redis_client.setex(
            DocumentVersionCache.version_summary_key(tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id),
            ttl,
            value,
        )
        return True

    @staticmethod
    @redis_fallback(default_return=False)
    def delete_version_list(*, tenant_id: str, dataset_id: str, document_id: str) -> bool:
        key = DocumentVersionCache.version_list_key(tenant_id=tenant_id, dataset_id=dataset_id, document_id=document_id)
        redis_client.delete(key)
        return True

    @staticmethod
    @redis_fallback(default_return=False)
    def delete_version_summary(*, tenant_id: str, dataset_id: str, version_id: str) -> bool:
        key = DocumentVersionCache.version_summary_key(
            tenant_id=tenant_id, dataset_id=dataset_id, version_id=version_id
        )
        redis_client.delete(key)
        return True
