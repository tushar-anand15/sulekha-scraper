"""SAKARMA Meeting Minutes Scraper.

Sister scraper to ``src/sulekha`` that captures public meeting records from
``meeting.lsgkerala.gov.in`` for every Kerala local body, year, main group,
and category exposed by the source. Three artifact types per Approved-Minutes
meeting (Minutes HTML, DR HTML, attachment PDFs) are persisted to a dedicated
GCS bucket; manifest, KPI snapshots, and reconciliation rows live in the
``sakarma`` Postgres schema.

See ``docs/brainstorms/2026-05-09-sakarma-requirements.md`` for the brainstorm
and ``docs/plans/2026-05-09-001-feat-sakarma-meeting-scraper-plan.md`` for the
implementation plan.
"""

__version__ = "0.1.0"
