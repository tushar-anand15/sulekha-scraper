"""Storage backends for Sulekha service.

This module provides a unified interface for object storage with two backends:
- GCSStorage: Google Cloud Storage for production
- S3Storage: S3-compatible storage (Minio) for development/testing

Both backends support:
- Structured path organization by year/lb_type/district/local_body
- Content hash deduplication
- Signed URL generation for access
"""

import hashlib
import io
import re
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Optional

import structlog

from sulekha.config import settings

logger = structlog.get_logger(__name__)


def slugify(text: str, max_length: int = 50) -> str:
    """Convert text to a filesystem-safe slug.

    Args:
        text: Text to slugify
        max_length: Maximum length of the result

    Returns:
        Slugified text safe for use in file paths
    """
    # Replace spaces with underscores
    text = text.strip().replace(" ", "_")
    # Remove characters not safe for filenames
    text = re.sub(r'[<>:"/\\|?*]', "_", text)
    # Collapse multiple underscores
    text = re.sub(r"_+", "_", text)
    # Remove leading/trailing underscores
    text = text.strip("_")
    # Truncate
    if len(text) > max_length:
        text = text[:max_length].rstrip("_")
    return text or "unnamed"


def build_storage_path(
    year_label: str,
    lb_type_label: str,
    district_name: str,
    lb_name: str,
    project_no: str,
    original_filename: Optional[str] = None,
) -> str:
    """Build a structured storage path for a PDF.

    Path format: pdfs/{year}/{lb_type}/{district}/{lb_name}/{project_no}.pdf

    Args:
        year_label: Year label (e.g., "2024-2025")
        lb_type_label: LB type label (e.g., "District Panchayat")
        district_name: District name
        lb_name: Local body name
        project_no: Project number
        original_filename: Original filename (optional, for extension)

    Returns:
        Object storage path
    """
    year_slug = slugify(year_label, 20)
    lb_type_slug = slugify(lb_type_label, 30)
    district_slug = slugify(district_name, 40)
    lb_slug = slugify(lb_name, 60)
    project_slug = slugify(project_no, 30)

    # Determine extension
    ext = ".pdf"
    if original_filename:
        if original_filename.lower().endswith(".pdf"):
            ext = ".pdf"

    path = f"pdfs/{year_slug}/{lb_type_slug}/{district_slug}/{lb_slug}/{project_slug}{ext}"
    return path


def compute_hash(content: bytes) -> str:
    """Compute SHA256 hash of content.

    Args:
        content: Bytes to hash

    Returns:
        Hex string of SHA256 hash
    """
    return hashlib.sha256(content).hexdigest()


class BaseStorage(ABC):
    """Abstract base class for object storage backends."""

    def __init__(self, bucket_name: str):
        self.bucket_name = bucket_name

    @abstractmethod
    def upload(self, path: str, content: bytes, content_type: str = "application/pdf") -> None:
        """Upload content to storage."""
        pass

    @abstractmethod
    def download(self, path: str) -> Optional[bytes]:
        """Download content from storage."""
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check if an object exists."""
        pass

    @abstractmethod
    def delete(self, path: str) -> bool:
        """Delete an object."""
        pass

    @abstractmethod
    def list_objects(self, prefix: str = "", max_results: int = 1000) -> list[str]:
        """List objects with a prefix."""
        pass

    def upload_pdf(
        self,
        pdf_bytes: bytes,
        year_label: str,
        lb_type_label: str,
        district_name: str,
        lb_name: str,
        project_no: str,
        original_filename: Optional[str] = None,
        content_hash: Optional[str] = None,
    ) -> tuple[str, str, int]:
        """Upload a PDF with structured path.

        Args:
            pdf_bytes: PDF content
            year_label: Year label
            lb_type_label: LB type label
            district_name: District name
            lb_name: Local body name
            project_no: Project number
            original_filename: Original filename
            content_hash: Pre-computed content hash (computed if not provided)

        Returns:
            Tuple of (path, content_hash, file_size_bytes)
        """
        # Compute hash if not provided
        if content_hash is None:
            content_hash = compute_hash(pdf_bytes)

        # Build path
        path = build_storage_path(
            year_label=year_label,
            lb_type_label=lb_type_label,
            district_name=district_name,
            lb_name=lb_name,
            project_no=project_no,
            original_filename=original_filename,
        )

        # Upload
        self.upload(path, pdf_bytes, content_type="application/pdf")

        file_size = len(pdf_bytes)

        logger.info(
            "Uploaded PDF",
            bucket=self.bucket_name,
            path=path,
            size_bytes=file_size,
            content_hash=content_hash[:16] + "...",
        )

        return path, content_hash, file_size


class GCSStorage(BaseStorage):
    """Google Cloud Storage backend for production."""

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        project_id: Optional[str] = None,
    ):
        """Initialize GCS storage.

        Args:
            bucket_name: GCS bucket name (defaults to settings)
            project_id: GCP project ID (defaults to settings)
        """
        super().__init__(bucket_name or settings.gcs_bucket_name)
        self.project_id = project_id or settings.gcs_project_id or None

        self._client = None
        self._bucket = None

    @property
    def client(self):
        """Get or create the GCS client."""
        if self._client is None:
            from google.cloud import storage
            self._client = storage.Client(project=self.project_id)
            logger.debug("Created GCS client", project=self.project_id)
        return self._client

    @property
    def bucket(self):
        """Get or create the bucket reference."""
        if self._bucket is None:
            self._bucket = self.client.bucket(self.bucket_name)
            logger.debug("Got bucket reference", bucket=self.bucket_name)
        return self._bucket

    def upload(self, path: str, content: bytes, content_type: str = "application/pdf") -> None:
        """Upload content to GCS."""
        blob = self.bucket.blob(path)
        blob.upload_from_string(content, content_type=content_type)

    def download(self, path: str) -> Optional[bytes]:
        """Download content from GCS."""
        from google.cloud.exceptions import NotFound
        try:
            blob = self.bucket.blob(path)
            return blob.download_as_bytes()
        except NotFound:
            logger.warning("Object not found in GCS", path=path)
            return None

    def exists(self, path: str) -> bool:
        """Check if an object exists in GCS."""
        blob = self.bucket.blob(path)
        return blob.exists()

    def delete(self, path: str) -> bool:
        """Delete an object from GCS."""
        from google.cloud.exceptions import NotFound
        try:
            blob = self.bucket.blob(path)
            blob.delete()
            logger.info("Deleted object from GCS", path=path)
            return True
        except NotFound:
            logger.warning("Object not found for deletion", path=path)
            return False

    def list_objects(self, prefix: str = "pdfs/", max_results: int = 1000) -> list[str]:
        """List objects in GCS with a prefix."""
        blobs = self.client.list_blobs(
            self.bucket_name,
            prefix=prefix,
            max_results=max_results,
        )
        return [blob.name for blob in blobs]

    def get_signed_url(self, path: str, expiration_hours: int = 24) -> str:
        """Generate a signed URL for accessing an object."""
        blob = self.bucket.blob(path)
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=expiration_hours),
            method="GET",
        )
        return url


class S3Storage(BaseStorage):
    """S3-compatible storage backend for development (Minio)."""

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        secure: bool = False,
    ):
        """Initialize S3-compatible storage (Minio).

        Args:
            bucket_name: Bucket name (defaults to settings)
            endpoint_url: S3 endpoint URL (defaults to settings)
            access_key: Access key (defaults to settings)
            secret_key: Secret key (defaults to settings)
            secure: Use HTTPS (defaults to False for local dev)
        """
        super().__init__(bucket_name or settings.s3_bucket_name)
        self.endpoint_url = endpoint_url or settings.s3_endpoint_url
        self.access_key = access_key or settings.s3_access_key
        self.secret_key = secret_key or settings.s3_secret_key
        self.secure = secure

        self._client = None

    @property
    def client(self):
        """Get or create the Minio client."""
        if self._client is None:
            from minio import Minio
            # Parse endpoint URL to get host
            endpoint = self.endpoint_url
            if endpoint.startswith("http://"):
                endpoint = endpoint[7:]
            elif endpoint.startswith("https://"):
                endpoint = endpoint[8:]
                self.secure = True

            self._client = Minio(
                endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.secure,
            )
            logger.debug("Created Minio client", endpoint=endpoint)

            # Ensure bucket exists
            if not self._client.bucket_exists(self.bucket_name):
                self._client.make_bucket(self.bucket_name)
                logger.info("Created bucket", bucket=self.bucket_name)

        return self._client

    def upload(self, path: str, content: bytes, content_type: str = "application/pdf") -> None:
        """Upload content to S3/Minio."""
        self.client.put_object(
            self.bucket_name,
            path,
            io.BytesIO(content),
            length=len(content),
            content_type=content_type,
        )

    def download(self, path: str) -> Optional[bytes]:
        """Download content from S3/Minio."""
        from minio.error import S3Error
        try:
            response = self.client.get_object(self.bucket_name, path)
            return response.read()
        except S3Error as e:
            if e.code == "NoSuchKey":
                logger.warning("Object not found in S3", path=path)
                return None
            raise
        finally:
            if 'response' in locals():
                response.close()
                response.release_conn()

    def exists(self, path: str) -> bool:
        """Check if an object exists in S3/Minio."""
        from minio.error import S3Error
        try:
            self.client.stat_object(self.bucket_name, path)
            return True
        except S3Error:
            return False

    def delete(self, path: str) -> bool:
        """Delete an object from S3/Minio."""
        from minio.error import S3Error
        try:
            self.client.remove_object(self.bucket_name, path)
            logger.info("Deleted object from S3", path=path)
            return True
        except S3Error as e:
            if e.code == "NoSuchKey":
                logger.warning("Object not found for deletion", path=path)
                return False
            raise

    def list_objects(self, prefix: str = "pdfs/", max_results: int = 1000) -> list[str]:
        """List objects in S3/Minio with a prefix."""
        objects = self.client.list_objects(
            self.bucket_name,
            prefix=prefix,
        )
        result = []
        for obj in objects:
            result.append(obj.object_name)
            if len(result) >= max_results:
                break
        return result

    def get_presigned_url(self, path: str, expiration_hours: int = 24) -> str:
        """Generate a presigned URL for accessing an object."""
        return self.client.presigned_get_object(
            self.bucket_name,
            path,
            expires=timedelta(hours=expiration_hours),
        )


# Storage factory
_storage_instance: Optional[BaseStorage] = None


def get_storage() -> BaseStorage:
    """Get the storage instance based on settings.

    Returns GCSStorage for production, S3Storage for development.
    """
    global _storage_instance
    if _storage_instance is None:
        if settings.storage_backend == "s3":
            _storage_instance = S3Storage()
            logger.info("Using S3/Minio storage backend")
        else:
            _storage_instance = GCSStorage()
            logger.info("Using GCS storage backend")
    return _storage_instance


def reset_storage() -> None:
    """Reset the storage singleton (useful for tests)."""
    global _storage_instance
    _storage_instance = None


# Alias for backwards compatibility
GCS_Storage = GCSStorage


def get_gcs_storage() -> BaseStorage:
    """Get the global storage instance (deprecated, use get_storage)."""
    return get_storage()
