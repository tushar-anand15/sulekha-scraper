#!/usr/bin/env bash
#
# Takes the release dump of the derived schemas, on the build host, for
# sambandh-app/deploy/load-master-db.sh to load onto production.
#
# Run after a passing `master build`. This is the build-host half of the
# procedure in sulekha/docs/deployment_runbook.md, "Getting the derived schemas
# onto production"; the production half is the load script named above.
#
# Two things about the shape.
#
# The schemas are renamed to `*_build` for the duration of the dump so that the
# archive carries the staging names. The far side restores into those scratch
# names while the site is still serving the previous release, and then swaps
# them into place with a catalogue rename that commits in milliseconds. If the
# dump carried the live names it would have to be restored directly over the
# schemas the site is reading, which is minutes of exclusive lock on a live
# database. The rename here is metadata only and takes no measurable time.
#
# `finance._project_name` is excluded. It is 1,296 MB, it is the intermediate
# the master runbook already says can be dropped, and production never reads it
# — the application's schema-qualified names are a closed set of fourteen and
# that is not one of them. Excluding it and the source schemas is the whole
# difference between shipping 5.2 GB and shipping 1.5 GB to a small VM.
#
# The renames are undone by a trap, so an interrupted or failed dump still
# leaves the build host with its schemas under the names everything else
# expects. That matters more than it looks: leaving `finance_build` behind
# would make the next `master build` fail in a way that reads like a build bug.

set -euo pipefail

CONTAINER="${MASTER_CONTAINER:-sambandh-master}"
BUCKET="${MASTER_BUCKET:-gs://your-db-backups-bucket}"
PROJECT="${GCP_PROJECT:-your-gcp-project}"
SCHEMAS=(core finance meetings elections)

STAMP="$(date -u +%Y%m%d)"
NAME="master_derived_${STAMP}.dump"
OUT="${PWD}/${NAME}"

log() { printf '\n[dump] %s\n' "$*"; }
psql_() { docker exec -i "$CONTAINER" psql -U sambandh -d sambandh "$@"; }

# --- preflight ------------------------------------------------------------

docker inspect "$CONTAINER" >/dev/null 2>&1 || {
  echo "[dump] FATAL: no container named ${CONTAINER}." >&2; exit 1; }

# A build that half-failed can leave these behind, and renaming onto an
# existing name fails partway through the loop — with some schemas renamed and
# some not, which is the messiest state this script could produce.
LEFTOVER="$(psql_ -tAc "SELECT string_agg(nspname,', ') FROM pg_namespace WHERE nspname LIKE '%\_build'" | tr -d '[:space:]')"
if [[ -n "$LEFTOVER" ]]; then
  echo "[dump] FATAL: staging schemas already exist: ${LEFTOVER}" >&2
  echo "A previous build or dump did not finish. Resolve before dumping." >&2
  exit 1
fi

for s in "${SCHEMAS[@]}"; do
  psql_ -tAc "SELECT 1 FROM pg_namespace WHERE nspname='${s}'" | grep -q 1 || {
    echo "[dump] FATAL: schema '${s}' does not exist. Run a master build first." >&2
    exit 1
  }
done

log "Build manifest of what is about to be dumped"
psql_ -c "SELECT dataset, built_at, bodies, projects, meetings, candidates FROM core.build_manifest"

# --- rename, dump, rename back --------------------------------------------

rename_to_build() {
  for s in "${SCHEMAS[@]}"; do
    psql_ -q -v ON_ERROR_STOP=1 -c "ALTER SCHEMA ${s} RENAME TO ${s}_build"
  done
}

# Runs on every exit path, including a failed dump and a Ctrl-C, so the build
# host is never left with its schemas under the staging names.
restore_names() {
  local rc=$?
  for s in "${SCHEMAS[@]}"; do
    if psql_ -tAc "SELECT 1 FROM pg_namespace WHERE nspname='${s}_build'" 2>/dev/null | grep -q 1; then
      psql_ -q -c "ALTER SCHEMA ${s}_build RENAME TO ${s}" >/dev/null 2>&1 \
        || echo "[dump] WARNING: could not rename ${s}_build back to ${s}." >&2
    fi
  done
  return $rc
}
trap restore_names EXIT

log "Renaming to staging names (metadata only)"
rename_to_build

log "Dumping — this is the slow part"
DUMP_ARGS=(-U sambandh -d sambandh -Fc -Z3 --exclude-table='finance_build._project_name')
for s in "${SCHEMAS[@]}"; do DUMP_ARGS+=(-n "${s}_build"); done
docker exec "$CONTAINER" pg_dump "${DUMP_ARGS[@]}" > "$OUT"

# The trap will put the names back; do it now so the verification below and
# anything else on this machine sees the normal names as soon as possible.
log "Restoring the live names"
restore_names
trap - EXIT

# --- verify ---------------------------------------------------------------

log "Verifying the archive"
SIZE="$(du -h "$OUT" | cut -f1)"
TOC="$(docker exec -i "$CONTAINER" pg_restore --list < "$OUT")" || {
  echo "[dump] FATAL: the archive is not readable." >&2; exit 1; }

for s in "${SCHEMAS[@]}"; do
  n="$(grep -c " ${s}_build " <<<"$TOC" || true)"
  [[ "$n" -gt 0 ]] || { echo "[dump] FATAL: nothing from ${s}_build in the archive." >&2; exit 1; }
  printf '  %-18s %s objects\n' "${s}_build" "$n"
done

# The load script refuses an archive that mentions public; catch it here, on
# the machine where it can still be re-taken cheaply, rather than on the VM.
if grep -qE ' (TABLE|TABLE DATA|SEQUENCE|VIEW|MATERIALIZED VIEW) public ' <<<"$TOC"; then
  echo "[dump] FATAL: the archive contains public objects." >&2; exit 1
fi
grep -q '_project_name' <<<"$TOC" \
  && { echo "[dump] FATAL: _project_name was not excluded." >&2; exit 1; } || true

log "Archive is ${SIZE}, scoped to the four staging schemas, no public objects"

# --- upload ---------------------------------------------------------------

log "Uploading to ${BUCKET}/master/${NAME}"
gcloud storage cp "$OUT" "${BUCKET}/master/${NAME}" --project "$PROJECT"

cat <<DONE

[dump] Done.

  local   ${OUT}  (${SIZE})
  remote  ${BUCKET}/master/${NAME}

Load it onto production with, on the VM:

    /opt/app/load-master-db.sh ${BUCKET}/master/${NAME}
DONE
