"""2020 -- the second SEC-spine year.

Genuinely 2020-specific, beyond the ``YearSpec`` declared in ``spec.py``:

* the PDF report uses the NEW layout (party, name, sex, age, address, votes)
  -- passed to :func:`~data_merge.years.base.load_inputs` explicitly, the
  same reasoning as 2015's ``Layout.OLD``;
* ``spec.pdf_sex`` is ``IGNORE``: the PDF's Sex column is inverted at source,
  so gender comes from the contesting-candidate feed instead
  (``spec.has_contest_feed``). The reservation-alignment check still uses the
  PDF, oriented -- ``base.py`` applies that independently of ``pdf_sex``,
  because R6 needs it regardless of which source gender itself prefers.
"""

from __future__ import annotations

from pathlib import Path

from data_merge.config import Paths
from data_merge.parsers.pdf_candidates import Layout
from data_merge.spec import spec_for
from data_merge.years.base import BuildResult, SecSpineInputs, build, load_inputs

YEAR = 2020

PDF_LAYOUT = Layout.NEW


def load(paths: Paths, *, pdf_cache_dir: Path | None = None) -> SecSpineInputs:
    """Read 2020's caches and PDF into :class:`SecSpineInputs`."""
    return load_inputs(
        spec_for(YEAR), paths, pdf_layout=PDF_LAYOUT, pdf_cache_dir=pdf_cache_dir
    )


def build_year(paths: Paths, *, pdf_cache_dir: Path | None = None) -> BuildResult:
    """Read and assemble 2020 end to end."""
    return build(spec_for(YEAR), load(paths, pdf_cache_dir=pdf_cache_dir))
