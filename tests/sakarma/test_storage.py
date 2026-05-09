"""Unit tests for SAKARMA storage path construction and upload helpers."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from sakarma.storage.gcs import (
    build_meeting_path,
    upload_document,
)


# ---------------------------------------------------------------------------
# build_meeting_path
# ---------------------------------------------------------------------------


def test_build_meeting_path_minutes_html() -> None:
    path = build_meeting_path(1, 5, 303, 2025, 12, 999, "minutes_html")
    assert path == "1/5/303/2025/12/999/minutes.html"


def test_build_meeting_path_dr_html() -> None:
    path = build_meeting_path(1, 5, 303, 2025, 12, 999, "dr_html")
    assert path == "1/5/303/2025/12/999/dr.html"


def test_build_meeting_path_attachment_pdf_with_original_filename() -> None:
    content_hash = "abc12345" + "f" * 56  # 64-char hex
    path = build_meeting_path(
        1,
        5,
        303,
        2025,
        12,
        999,
        "attachment_pdf",
        original_filename="prosecution details.pdf",
        content_hash=content_hash,
    )
    assert path.startswith("1/5/303/2025/12/999/attachments/abc12345__")
    assert path.endswith(".pdf")
    # Slugified form should not contain spaces and should preserve recognizable tokens
    assert "prosecution" in path
    assert "details" in path
    assert " " not in path


def test_build_meeting_path_attachment_pdf_without_original_filename() -> None:
    content_hash = "deadbeef" + "0" * 56
    path = build_meeting_path(
        1,
        5,
        303,
        2025,
        12,
        999,
        "attachment_pdf",
        original_filename=None,
        content_hash=content_hash,
    )
    assert path == "1/5/303/2025/12/999/attachments/attachment_deadbeef.pdf"


def test_build_meeting_path_attachment_pdf_requires_content_hash() -> None:
    with pytest.raises(ValueError, match="content_hash"):
        build_meeting_path(
            1, 5, 303, 2025, 12, 999, "attachment_pdf", original_filename="a.pdf"
        )


def test_build_meeting_path_unknown_artifact_type() -> None:
    with pytest.raises(ValueError, match="Unknown artifact_type"):
        build_meeting_path(1, 5, 303, 2025, 12, 999, "bogus")


def test_build_meeting_path_attachment_strips_double_pdf_extension() -> None:
    content_hash = "11112222" + "a" * 56
    path = build_meeting_path(
        1,
        5,
        303,
        2025,
        12,
        999,
        "attachment_pdf",
        original_filename="report.PDF",
        content_hash=content_hash,
    )
    # Only one .pdf extension at the end, regardless of input casing
    assert path.endswith(".pdf")
    assert not path.endswith(".PDF.pdf")
    assert not path.endswith(".pdf.pdf")


# ---------------------------------------------------------------------------
# upload_document
# ---------------------------------------------------------------------------


def _make_mock_storage() -> MagicMock:
    storage = MagicMock()
    storage.bucket_name = "sakarma-test-bucket"
    storage.upload = MagicMock(return_value=None)
    return storage


def test_upload_document_html_roundtrip_bytes() -> None:
    storage = _make_mock_storage()
    blob = b"<html><body>hello</body></html>"
    path = "1/5/303/2025/12/999/minutes.html"

    out_path, content_hash, size = upload_document(storage, blob, path)

    assert out_path == path
    assert size == len(blob)
    assert content_hash == hashlib.sha256(blob).hexdigest()

    storage.upload.assert_called_once()
    call_args = storage.upload.call_args
    # First positional: path; second positional: bytes
    assert call_args.args[0] == path
    assert call_args.args[1] == blob  # byte-for-byte
    assert call_args.kwargs.get("content_type") == "text/html; charset=utf-8"


def test_upload_document_infers_pdf_content_type() -> None:
    storage = _make_mock_storage()
    blob = b"%PDF-1.4 fake"
    path = "1/5/303/2025/12/999/attachments/abcd1234__doc.pdf"

    upload_document(storage, blob, path)

    call_args = storage.upload.call_args
    assert call_args.kwargs.get("content_type") == "application/pdf"


def test_upload_document_defaults_to_octet_stream_for_unknown_extension() -> None:
    storage = _make_mock_storage()
    blob = b"binary blob"
    path = "1/5/303/2025/12/999/extras/something.bin"

    upload_document(storage, blob, path)

    call_args = storage.upload.call_args
    assert call_args.kwargs.get("content_type") == "application/octet-stream"


def test_upload_document_explicit_content_type_overrides_inference() -> None:
    storage = _make_mock_storage()
    blob = b"<html></html>"
    path = "1/5/303/2025/12/999/minutes.html"

    upload_document(storage, blob, path, content_type="application/xhtml+xml")

    call_args = storage.upload.call_args
    assert call_args.kwargs.get("content_type") == "application/xhtml+xml"


def test_upload_document_preserves_malayalam_utf8_bytes() -> None:
    storage = _make_mock_storage()
    body = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        "<body><h1>ഭരണസമിതി യോഗം</h1></body></html>"
    )
    blob = body.encode("utf-8")
    path = "1/5/303/2025/12/999/minutes.html"

    out_path, content_hash, size = upload_document(storage, blob, path)

    # Bytes round-trip exactly
    uploaded_bytes = storage.upload.call_args.args[1]
    assert uploaded_bytes == blob
    assert uploaded_bytes.decode("utf-8") == body
    assert size == len(blob)
    assert content_hash == hashlib.sha256(blob).hexdigest()
    assert out_path == path


def test_upload_document_returns_correct_hash_and_size() -> None:
    storage = _make_mock_storage()
    blob = b"x" * 1234
    path = "1/5/303/2025/12/999/dr.html"

    out_path, content_hash, size = upload_document(storage, blob, path)

    assert out_path == path
    assert size == 1234
    assert content_hash == hashlib.sha256(blob).hexdigest()
    assert len(content_hash) == 64  # SHA-256 hex
