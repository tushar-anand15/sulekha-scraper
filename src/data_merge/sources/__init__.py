"""Read-only access to the raw sources. No parsing, no network.

Every module here answers "what bytes are on disk?" and nothing more.
Interpreting those bytes is ``parsers``' job.
"""

from data_merge.sources.cache import CachedResponse, CacheError, ResponseCache
from data_merge.sources.files import HtmlDirectory, read_html
from data_merge.sources.pdf import PdfText, have_pdftotext

__all__ = [
    "CacheError",
    "CachedResponse",
    "HtmlDirectory",
    "PdfText",
    "ResponseCache",
    "have_pdftotext",
    "read_html",
]
