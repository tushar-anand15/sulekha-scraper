-- Master schema for Gram Sambandh.
--
-- Every fact table carries lb_key, the surrogate from core.local_body, so a
-- page for one body can query finances, meetings and elections without knowing
-- that the three upstream systems identify bodies three different ways.
--
-- Sources stay in src_* schemas and are never edited. These tables are derived
-- and can be dropped and rebuilt from them at any time.
--
-- The build does not run this file against core/finance/meetings/elections. It
-- creates a parallel set -- core_build, finance_build and so on -- rewrites
-- every schema qualifier below to point at them, and renames them into place
-- only once every table exists (see src/master/swap.py). The names here are the
-- live ones so the file reads the way the database looks, and so `psql -f` still
-- does the obvious thing; the four schemas are created by the caller.

-- ---------------------------------------------------------------- dimensions

-- Kerala's financial year runs April to March. One rule, one place: a meeting
-- on 12 March 2025 belongs to 2024-25, and must line up with the finance rows
-- for that body and year on the same page.
CREATE OR REPLACE FUNCTION core.fin_year(d date) RETURNS text AS $$
  SELECT CASE WHEN extract(month FROM d) >= 4
              THEN extract(year FROM d)::int || '-' || (extract(year FROM d)::int + 1)
              ELSE (extract(year FROM d)::int - 1) || '-' || extract(year FROM d)::int
         END;
$$ LANGUAGE sql IMMUTABLE STRICT;

DROP TABLE IF EXISTS core.financial_year CASCADE;
CREATE TABLE core.financial_year (
  year_label   text primary key,
  start_date   date not null,
  end_date     date not null,
  is_complete  boolean not null
);
INSERT INTO core.financial_year
SELECT y || '-' || (y + 1),
       make_date(y, 4, 1),
       make_date(y + 1, 3, 31),
       -- 2025-26 was still running when the data was scraped; anything that
       -- ends in the future must never be compared with a closed year without
       -- saying so.
       make_date(y + 1, 3, 31) < date '2026-03-31'
FROM generate_series(2012, 2025) AS y;

-- ------------------------------------------------------------------ finances

DROP TABLE IF EXISTS finance.project CASCADE;
CREATE TABLE finance.project AS
SELECT p.id                    AS project_id,
       y.lb_key,
       d.year_label,
       p.project_no,
       p.project_name,
       p.formulation::numeric  AS formulation,
       p.expense::numeric      AS expense,
       pdf.gcs_path            AS pdf_gcs_path,
       (pdf.id IS NOT NULL)    AS has_pdf
FROM src_sulekha.projects p
JOIN src_sulekha.local_bodies lb ON lb.id = p.local_body_id
JOIN src_sulekha.districts   d  ON d.id  = lb.district_id
JOIN core.lb_sulekha_year    y  ON y.sulekha_lb_id = lb.id AND y.year_label = d.year_label
LEFT JOIN src_sulekha.pdfs   pdf ON pdf.project_id = p.id;

ALTER TABLE finance.project ADD PRIMARY KEY (project_id);
CREATE INDEX ON finance.project (lb_key, year_label);
CREATE INDEX ON finance.project (year_label);

-- ------------------------------------------------------------------ meetings

DROP TABLE IF EXISTS meetings.meeting CASCADE;
CREATE TABLE meetings.meeting AS
SELECT m.id AS meeting_id,
       b.lb_key,
       m.meeting_date,
       core.fin_year(m.meeting_date) AS year_label,
       m.meeting_no_label,
       m.meeting_type,
       m.meeting_nature,
       m.meeting_venue,
       m.category
FROM sakarma.meeting_manifest m
JOIN core.local_body b ON b.sakarma_lb_id = m.lb_id;

ALTER TABLE meetings.meeting ADD PRIMARY KEY (meeting_id);
CREATE INDEX ON meetings.meeting (lb_key, year_label);
CREATE INDEX ON meetings.meeting (meeting_date);

DROP TABLE IF EXISTS meetings.artifact CASCADE;
CREATE TABLE meetings.artifact AS
SELECT a.id AS artifact_id,
       a.meeting_manifest_id AS meeting_id,
       a.artifact_type::text AS artifact_type,
       a.decision_index,
       a.original_filename,
       a.gcs_path,
       a.byte_size
FROM sakarma.meeting_artifact a
JOIN meetings.meeting m ON m.meeting_id = a.meeting_manifest_id;

ALTER TABLE meetings.artifact ADD PRIMARY KEY (artifact_id);
CREATE INDEX ON meetings.artifact (meeting_id, artifact_type);

-- ----------------------------------------------------------------- elections

DROP TABLE IF EXISTS elections.candidate CASCADE;
CREATE TABLE elections.candidate AS
SELECT b.lb_key, c.* FROM src_elections.candidates c
JOIN core.local_body b ON b.lb_code = c.lb_code;
CREATE INDEX ON elections.candidate (lb_key, cycle);

DROP TABLE IF EXISTS elections.ward CASCADE;
CREATE TABLE elections.ward AS
SELECT b.lb_key, w.* FROM src_elections.wards w
JOIN core.local_body b ON b.lb_code = w.lb_code;
CREATE INDEX ON elections.ward (lb_key, cycle);

DROP TABLE IF EXISTS elections.body_result CASCADE;
CREATE TABLE elections.body_result AS
SELECT b.lb_key, l.* FROM src_elections.local_bodies l
JOIN core.local_body b ON b.lb_code = l.lb_code;
CREATE INDEX ON elections.body_result (lb_key, cycle);

-- ------------------------------------------------------------------- rollups
-- Everything the dropdowns, the map colouring and the body pages read. These
-- exist so no page-load ever scans 3.6M project rows or 443k meetings.

DROP TABLE IF EXISTS finance.lb_year_summary CASCADE;
CREATE TABLE finance.lb_year_summary AS
WITH per AS (
  SELECT lb_key, year_label,
         count(*)                        AS projects,
         sum(formulation)                AS formulation,
         sum(expense)                    AS expense,
         count(*) FILTER (WHERE has_pdf) AS projects_with_pdf
  FROM finance.project GROUP BY 1, 2
)
SELECT p.*,
       CASE WHEN p.formulation > 0 THEN round(p.expense / p.formulation * 100, 1) END AS expense_pct
FROM per p;
ALTER TABLE finance.lb_year_summary ADD PRIMARY KEY (lb_key, year_label);

-- Carry-forward is name recurrence, not a flag the source provides. It is
-- recorded here as a measurable candidate, and must be validated against a
-- hand-checked district before any page shows it as fact -- generic recurring
-- names ("ദൈനംദിന ചെലവുകൾ", daily expenses) will over-match.
-- Distinct names per body-year, so a body that lists the same project name
-- twice in one year counts once on each side of the comparison.
DROP TABLE IF EXISTS finance._project_name CASCADE;
CREATE TABLE finance._project_name AS
SELECT DISTINCT lb_key, year_label,
       (split_part(year_label, '-', 1)::int - 1) || '-' || split_part(year_label, '-', 1) AS prev_label,
       lower(btrim(project_name)) AS pname
FROM finance.project;
CREATE INDEX ON finance._project_name (lb_key, year_label, pname);

DROP TABLE IF EXISTS finance.lb_year_continuity CASCADE;
CREATE TABLE finance.lb_year_continuity AS
SELECT c.lb_key, c.year_label,
       count(*)                    AS distinct_projects,
       count(p.pname)              AS also_in_prev_year,
       count(*) - count(p.pname)   AS first_seen_this_year
FROM finance._project_name c
LEFT JOIN finance._project_name p
       ON p.lb_key = c.lb_key AND p.pname = c.pname AND p.year_label = c.prev_label
GROUP BY 1, 2;
ALTER TABLE finance.lb_year_continuity ADD PRIMARY KEY (lb_key, year_label);

DROP TABLE IF EXISTS meetings.lb_year_summary CASCADE;
CREATE TABLE meetings.lb_year_summary AS
SELECT lb_key, year_label,
       count(*)                                                        AS meetings,
       count(*) FILTER (WHERE meeting_type LIKE 'ഭരണസമിതി%')             AS governing_body,
       count(*) FILTER (WHERE meeting_type NOT LIKE 'ഭരണസമിതി%')         AS standing_committee,
       count(*) FILTER (WHERE meeting_nature = 'സാധാരണ യോഗം')            AS ordinary,
       count(*) FILTER (WHERE meeting_nature <> 'സാധാരണ യോഗം')           AS special,
       min(meeting_date) AS first_meeting,
       max(meeting_date) AS last_meeting
FROM meetings.meeting GROUP BY 1, 2;
ALTER TABLE meetings.lb_year_summary ADD PRIMARY KEY (lb_key, year_label);

-- One row per body: what the dropdown and the map need in a single read.
DROP TABLE IF EXISTS core.lb_coverage CASCADE;
CREATE TABLE core.lb_coverage AS
SELECT b.lb_key, b.lb_code, b.district_name, b.lb_type, b.lb_name_en, b.lb_name_ml,
       b.first_cycle, b.last_cycle,
       -- Carried rather than joined for. Every body lookup and the selector need
       -- it -- Mattannur has finances and meetings and no election result, and a
       -- page that cannot tell that from "no data" renders as broken. An empty
       -- first_cycle is evidence for the same thing, but it is evidence, not the
       -- fact: this is the column the SEC's registry actually settled.
       b.in_elections,
       (b.sakarma_lb_id IS NOT NULL)  AS has_meetings,
       (b.ksmart_lb_code IS NOT NULL) AS has_geometry,
       f.years_with_finance, f.projects_total, f.formulation_total, f.expense_total,
       m.years_with_meetings, m.meetings_total
FROM core.local_body b
LEFT JOIN (
  SELECT lb_key, count(*) AS years_with_finance, sum(projects) AS projects_total,
         sum(formulation) AS formulation_total, sum(expense) AS expense_total
  FROM finance.lb_year_summary GROUP BY 1
) f ON f.lb_key = b.lb_key
LEFT JOIN (
  SELECT lb_key, count(*) AS years_with_meetings, sum(meetings) AS meetings_total
  FROM meetings.lb_year_summary GROUP BY 1
) m ON m.lb_key = b.lb_key;
ALTER TABLE core.lb_coverage ADD PRIMARY KEY (lb_key);
CREATE INDEX ON core.lb_coverage (district_name, lb_type);

-- ------------------------------------------------------------------ provenance
-- R9: every figure on the site traces to a named dataset and a build date. The
-- API used to take that date from a constant it could be handed by an
-- environment variable, which meant the site could state a date the data did
-- not have. This is the date the data has, written by the run that made it.
--
-- One row, not a history. The derived schemas are rebuilt wholesale and swapped
-- in, so a row describing an earlier build would be swapped out along with the
-- tables it described -- a history table here could only ever hold the present.
-- What was true of an earlier build lives in data/final/master/manifest.json,
-- which is a file, and in git.
--
-- The row is inserted by the build, not here: the timestamp, the fingerprint and
-- the counts are facts about one run.
DROP TABLE IF EXISTS core.build_manifest CASCADE;
CREATE TABLE core.build_manifest (
  dataset        text        primary key,
  built_at       timestamptz not null,
  master_version text        not null,
  -- Ties the row to data/final/master/manifest.json, which carries the gates,
  -- the coverage and a hash of every input file.
  fingerprint    text        not null,
  -- The dumps that were restored before the build ran. The restore is a manual
  -- step outside this module, so these are declared by whoever ran it --
  -- `master build --source-dump …` -- and default to the pair the runbook names.
  source_dumps   text[]      not null,
  -- The headline counts, read from the tables this same run built.
  bodies         int         not null,
  projects       int         not null,
  meetings       int         not null,
  candidates     int         not null
);
