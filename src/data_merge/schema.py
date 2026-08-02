"""The canonical candidate schema.

Thirty-one columns, in one order, shared by all four election cycles. A year
that has no source for a field emits an empty string, never a placeholder and
never ``None`` -- that is what makes the four files safe to stack with a
``year`` column added.

Nothing outside this module may define column order. Ported verbatim from
``harmonise.py``'s ``SCHEMA`` so the rebuilt pipeline stays byte-comparable
against the files it must reproduce.
"""

from __future__ import annotations

from typing import Final

CandidateRow = dict[str, str]
"""A candidate row. Every value is a string; absence is the empty string.

Deliberately a plain dict rather than a dataclass: rows flow through CSV at
both ends, and a dict round-trips without the ``None``/``NaN`` coercion that
motivated this rebuild.
"""

SCHEMA: Final[tuple[str, ...]] = (
    "district_code",
    "district_name",
    "lb_type",
    "lb_code",
    "lb_name",
    "lb_name_mal",
    "ward_code",
    "ward_no",
    "ward_name",
    "ward_name_mal",
    "party_name",
    "party_group",
    "party_front",
    "party_group_source",
    "candidate_code",
    "candidate_title",
    "candidate_gender",
    "gender_source",
    "candidate_age",
    "candidate_name",
    "candidate_name_eng",
    "status",
    "total_votes",
    "invalid_votes",
    "ward_reservation",
    "candidate_role",
    "ward_winner_party",
    "ward_winner_party_group",
    "lb_ruling_front",
    "lb_control_type",
    "lb_head_party_group",
)

SCHEMA_SET: Final[frozenset[str]] = frozenset(SCHEMA)

# Columns a year may legitimately leave empty throughout. Everything else is
# expected to carry a value on every row, and validate/ asserts as much.
OPTIONAL_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "lb_name_mal",
        "ward_name_mal",
        "candidate_age",
        "candidate_name_eng",
        "invalid_votes",
        "candidate_code",
        "candidate_title",
        "ward_reservation",
        "candidate_role",
    }
)


class SchemaError(ValueError):
    """A row or file does not match the canonical schema."""


def blank_row() -> CandidateRow:
    """A row with every canonical column present and empty."""
    return dict.fromkeys(SCHEMA, "")


def conform(row: dict[str, object]) -> CandidateRow:
    """Coerce a partial row to the canonical schema.

    Missing columns fill with the empty string. ``None`` becomes the empty
    string rather than the literal ``"None"``. Extra keys raise immediately:
    an unexpected key means a producer and this schema disagree, and that
    disagreement should surface here rather than as a missing column three
    stages later.
    """
    extra = set(row) - SCHEMA_SET
    if extra:
        raise SchemaError(f"row carries columns outside the schema: {sorted(extra)}")
    out = blank_row()
    for key, value in row.items():
        out[key] = "" if value is None else str(value)
    return out


def check_columns(columns: list[str], *, origin: str) -> None:
    """Assert an ordered column list is exactly the canonical schema.

    ``origin`` names the file or producer so a mismatch is actionable.
    """
    if tuple(columns) == SCHEMA:
        return
    missing = [c for c in SCHEMA if c not in columns]
    unexpected = [c for c in columns if c not in SCHEMA_SET]
    if missing or unexpected:
        raise SchemaError(
            f"{origin}: column set differs from schema "
            f"(missing={missing}, unexpected={unexpected})"
        )
    raise SchemaError(
        f"{origin}: columns are the right set but the wrong order; "
        f"expected {list(SCHEMA)}, got {columns}"
    )
