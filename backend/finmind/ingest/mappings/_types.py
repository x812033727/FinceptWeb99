"""DatasetMapping dataclass + MappingNotFoundError + type-coercion
helpers shared by the row + batch transform modules and the registry.

The four `_to_*` helpers normalize FinMind's loose JSON shapes (empty
string sentinel for missing values, ISO date strings, mixed-type numbers) into
the typed columns the local tables expect. Lifted out of the original
flat `mappings.py` so the per-dataset transforms can import the
helpers without dragging in the full registry on every test load.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class CompareSpec:
    """Value-level comparison recipe for the cutover dry-run's
    ``--values`` mode. Describes how to join a self-crawl connector's
    raw rows against FinMind's raw rows and which columns must match
    within tolerance before a source flip is safe.

    All column names here are FinMind's *raw* response keys (the shape
    ``SourceClient.fetch`` returns), NOT the local-table columns —
    self-crawl handlers translate their upstream columns back to
    FinMind's key names precisely so this comparison, and the existing
    ``column_map``, both stay source-agnostic.

    ``value_cols`` entries are ``(column, kind, tol)`` where ``kind`` is:
      - ``"rel"``   relative error ``|a-b| / max(|a|,|b|) <= tol`` (prices)
      - ``"abs"``   absolute error ``|a-b| <= tol`` (share/lot counts
                    that differ only by rounding)
      - ``"exact"`` string/enum equality (``tol`` ignored)
    """

    key_cols: tuple[str, ...]
    value_cols: tuple[tuple[str, str, float], ...]


@dataclass(frozen=True)
class DatasetMapping:
    """One entry per FinMind dataset the ingest runner can route."""

    dataset_code: str
    local_table: str
    # FinMind column name → local column name. Columns absent here
    # are dropped silently (we don't store every FinMind field —
    # see ``raw`` JSONB columns in the fundamental tables for the
    # full-payload alternative).
    column_map: dict[str, str]
    # PK columns of the local table — used to build the ON CONFLICT
    # clause for idempotent UPSERT.
    pk_columns: tuple[str, ...]
    # Static fields injected into every row (e.g. `market='TWSE'` for
    # datasets that don't carry the market axis in their response).
    extra: dict[str, Any] = field(default_factory=dict)
    # Optional per-row callback that runs AFTER `column_map` and
    # `extra` apply — for unit conversions, derived columns, etc.
    row_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Optional whole-chunk transform — for wide-format datasets (e.g.
    # quarterly statements that return one FinMind row per line item)
    # that need to be pivoted into one local row per period. Mutually
    # exclusive with row_transform; runner prefers batch_transform when
    # both are set. Receives the raw FinMind payload (list[dict]) and
    # returns local-table rows (list[dict]) directly — column_map and
    # row_transform DO NOT apply on the batch path because the pivot
    # logic owns column resolution itself.
    batch_transform: (
        Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None
    ) = None
    # FinMind has a class of intraday-grain datasets (KBar, PriceTick,
    # BlockTradingDailyReport, GovernmentBankBuySell) that reject any
    # multi-day query with HTTP 400 *"the dataset … size is too large,
    # we only send one day data, so end_date parameter need be none"*.
    # When this flag is set, the runner iterates the requested
    # [range_start, range_end] day-by-day and concatenates the per-day
    # responses, omitting the `end_date` query param on each call.
    # The chunk in `backfill_progress` still spans the full range —
    # the day-level fan-out is internal to one chunk's fetch step.
    single_day: bool = False
    # Optional value-level comparison recipe for the cutover dry-run's
    # ``--values`` mode (see `CompareSpec`). When absent, the dry-run
    # falls back to its row-count-only check for this dataset.
    compare_spec: CompareSpec | None = None



class MappingNotFoundError(LookupError):
    """The runner was asked to ingest a dataset with no entry in
    MAPPINGS — distinguishes 'we haven't built this yet' from a
    transient FinMind failure so progress.py records 'skipped'
    rather than 'failed'."""



# ── Type coercion helpers ────────────────────────────────────────


def _to_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    return date.fromisoformat(str(v)[:10])



def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None



def _to_decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (TypeError, ValueError, InvalidOperation):
        return None



def _to_str(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return str(v)
