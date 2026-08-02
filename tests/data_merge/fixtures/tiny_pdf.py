"""Build a minimal, valid multi-page PDF from raw syntax.

Written by hand rather than checked in as a binary, and rather than pulling in
a PDF writer: the source-layer tests only need to prove that both engines read
text out of a real file, and a generator keeps the fixture inspectable.
"""

from __future__ import annotations


def build_pdf(pages: list[list[str]]) -> bytes:
    """A PDF where each entry in ``pages`` is that page's lines of text."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

    page_ids: list[int] = []
    content_ids: list[int] = []
    for lines in pages:
        drawn = b"BT /F1 10 Tf 12 TL 40 740 Td\n"
        for line in lines:
            drawn += b"(" + _escape(line) + b") Tj T*\n"
        drawn += b"ET"
        content_ids.append(add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(drawn), drawn)))
        page_ids.append(0)  # placeholder, filled once the Pages object has an id

    pages_id = len(objects) + len(pages) + 1
    for index, content_id in enumerate(content_ids):
        page_ids[index] = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font, content_id)
        )

    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog,
        xref_at,
    )
    return bytes(out)


def _escape(text: str) -> bytes:
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("latin-1", errors="replace")
