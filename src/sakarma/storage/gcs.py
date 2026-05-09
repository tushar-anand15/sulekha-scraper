"""SAKARMA storage backend.

Builds meeting-scoped object paths and uploads HTML/PDF artifacts to a
dedicated SAKARMA bucket. Reuses the domain-agnostic primitives from
``sulekha.storage.gcs`` (``BaseStorage``, ``GCSStorage``, ``S3Storage``,
``slugify``, ``compute_hash``); this cross-tenant import is acceptable for
v1 per the plan's Risks & Dependencies note. A future v3 step may extract
these into a shared ``common.storage`` package.

Path layout (bucket-relative, no leading prefix; the storage backend owns
the bucket name):

    {district_id}/{lb_type_id}/{lb_id}/{year}/{main_group_value_id}/{meeting_manifest_id}/minutes.html
    {district_id}/{lb_type_id}/{lb_id}/{year}/{main_group_value_id}/{meeting_manifest_id}/dr.html
    {district_id}/{lb_type_id}/{lb_id}/{year}/{main_group_value_id}/{meeting_manifest_id}/attachments/{sha8}__{slug}.pdf
"""

from __future__ import annotations

from typing import Optional

import structlog

from sakarma.config import settings
from sulekha.storage.gcs import (
    BaseStorage,
    GCSStorage,
    S3Storage,
    compute_hash,
    slugify,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Path construction
# ---------------------------------------------------------------------------

ARTIFACT_MINUTES_HTML = "minutes_html"
ARTIFACT_DR_HTML = "dr_html"
ARTIFACT_ATTACHMENT_PDF = "attachment_pdf"


def build_meeting_path(
    district_id: int,
    lb_type_id: int,
    lb_id: int,
    year: int,
    main_group_value_id: int,
    meeting_manifest_id: int,
    artifact_type: str,
    original_filename: Optional[str] = None,
    content_hash: Optional[str] = None,
) -> str:
    """Build a bucket-relative GCS object name for a SAKARMA meeting artifact.

    Args:
        district_id: District ID.
        lb_type_id: Local body type ID.
        lb_id: Local body ID.
        year: Meeting year.
        main_group_value_id: Main group value ID (committee variant).
        meeting_manifest_id: Internal meeting manifest row ID.
        artifact_type: One of ``"minutes_html"``, ``"dr_html"``, ``"attachment_pdf"``.
        original_filename: Original filename (required for attachments to
            produce a recognizable slug; otherwise a hash-derived fallback
            is used).
        content_hash: SHA-256 hex of the bytes; only the first 8 chars are
            embedded in attachment paths. Required for ``attachment_pdf``.

    Returns:
        Object storage path (no bucket prefix).
    """
    base = (
        f"{district_id}/{lb_type_id}/{lb_id}/{year}/"
        f"{main_group_value_id}/{meeting_manifest_id}"
    )

    if artifact_type == ARTIFACT_MINUTES_HTML:
        return f"{base}/minutes.html"

    if artifact_type == ARTIFACT_DR_HTML:
        return f"{base}/dr.html"

    if artifact_type == ARTIFACT_ATTACHMENT_PDF:
        if not content_hash:
            raise ValueError(
                "content_hash is required for artifact_type='attachment_pdf'"
            )
        sha8 = content_hash[:8]
        if original_filename:
            # Strip trailing .pdf (case-insensitive) before slugifying so the
            # extension we append is canonical and lowercase.
            stem = original_filename
            if stem.lower().endswith(".pdf"):
                stem = stem[:-4]
            slug = slugify(stem, max_length=80)
            # slugify uses underscores; convert spaces-collapsed underscores
            # to hyphens for nicer URL-style filenames.
            slug = slug.replace("_", "-")
            filename = f"{sha8}__{slug}.pdf"
        else:
            filename = f"attachment_{sha8}.pdf"
        return f"{base}/attachments/{filename}"

    raise ValueError(f"Unknown artifact_type: {artifact_type!r}")


# ---------------------------------------------------------------------------
# Content-type inference
# ---------------------------------------------------------------------------

_EXT_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".pdf": "application/pdf",
}


def _infer_content_type(path: str) -> str:
    """Infer Content-Type from path extension."""
    lowered = path.lower()
    for ext, ct in _EXT_CONTENT_TYPES.items():
        if lowered.endswith(ext):
            return ct
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def upload_document(
    storage: BaseStorage,
    blob_bytes: bytes,
    path: str,
    content_type: Optional[str] = None,
    original_filename: Optional[str] = None,
) -> tuple[str, str, int]:
    """Upload a meeting artifact (HTML or PDF) to SAKARMA storage.

    Bytes are written verbatim — no encoding/decoding occurs here. For HTML
    the caller is responsible for ensuring the bytes are already encoded
    (typically UTF-8) and for embedding the right ``<meta charset>`` if any.

    Args:
        storage: ``BaseStorage`` instance (GCS or S3/Minio).
        blob_bytes: Raw bytes to upload.
        path: Bucket-relative object path (e.g. from ``build_meeting_path``).
        content_type: Explicit Content-Type. If ``None``, inferred from
            ``path`` extension.
        original_filename: Reserved for future logging/metadata; unused here
            because ``path`` already encodes any filename slugging.

    Returns:
        Tuple ``(path, content_hash_hex, byte_size)``.
    """
    del original_filename  # accepted for API symmetry; not currently used

    if content_type is None:
        content_type = _infer_content_type(path)

    content_hash = compute_hash(blob_bytes)
    byte_size = len(blob_bytes)

    storage.upload(path, blob_bytes, content_type=content_type)

    logger.info(
        "Uploaded SAKARMA document",
        bucket=storage.bucket_name,
        path=path,
        size_bytes=byte_size,
        content_type=content_type,
        content_hash=content_hash[:16] + "...",
    )

    return path, content_hash, byte_size


# ---------------------------------------------------------------------------
# Storage factory (singleton, mirrors sulekha)
# ---------------------------------------------------------------------------

_storage_instance: Optional[BaseStorage] = None


def get_storage() -> BaseStorage:
    """Return the SAKARMA storage singleton, constructed lazily from settings."""
    global _storage_instance
    if _storage_instance is None:
        if settings.storage_backend == "s3":
            _storage_instance = S3Storage(
                bucket_name=settings.s3_bucket_name,
                endpoint_url=settings.s3_endpoint_url,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
            )
            logger.info(
                "Using S3/Minio storage backend for SAKARMA",
                bucket=settings.s3_bucket_name,
            )
        else:
            _storage_instance = GCSStorage(
                bucket_name=settings.gcs_bucket_name,
                project_id=settings.gcs_project_id or None,
            )
            logger.info(
                "Using GCS storage backend for SAKARMA",
                bucket=settings.gcs_bucket_name,
            )
    return _storage_instance


def reset_storage() -> None:
    """Reset the storage singleton (test helper)."""
    global _storage_instance
    _storage_instance = None
