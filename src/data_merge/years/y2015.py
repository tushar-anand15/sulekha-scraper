"""2015 -- the first SEC-spine year.

Genuinely 2015-specific, beyond the ``YearSpec`` declared in ``spec.py``:

* the PDF report uses the OLD layout (name, sex, party, votes) -- passed to
  :func:`~data_merge.years.base.load_inputs` explicitly, never guessed from
  ``spec.year``, so that fact stays a property of the document rather than a
  year switch inside the shared assembly;
* there is no contesting-candidate feed (``spec.has_contest_feed`` is
  ``False``), so gender leans on the PDF's own Sex field once its orientation
  is measured -- see ``base.py``'s module docstring on ``spec.pdf_sex``.
"""

from __future__ import annotations

from pathlib import Path

from data_merge.config import Paths
from data_merge.parsers.pdf_candidates import Layout
from data_merge.spec import spec_for
from data_merge.years.base import BuildResult, SecSpineInputs, build, load_inputs

YEAR = 2015

PDF_LAYOUT = Layout.OLD


def load(paths: Paths, *, pdf_cache_dir: Path | None = None) -> SecSpineInputs:
    """Read 2015's caches and PDF into :class:`SecSpineInputs`."""
    return load_inputs(
        spec_for(YEAR), paths, pdf_layout=PDF_LAYOUT, pdf_cache_dir=pdf_cache_dir
    )


def build_year(paths: Paths, *, pdf_cache_dir: Path | None = None) -> BuildResult:
    """Read and assemble 2015 end to end."""
    return build(spec_for(YEAR), load(paths, pdf_cache_dir=pdf_cache_dir))
