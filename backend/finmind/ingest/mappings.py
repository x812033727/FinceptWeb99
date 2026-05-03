"""FinMind row → local table mappings.

FinMind's column names are dataset-specific and don't match our
local schema (e.g. `Trading_Volume` → `volume`, `MarginPurchaseBuy`
→ `margin_purchase`). Each registered dataset has a mapping here;
the runner uses the mapping to:

  1. Project FinMind's response dict onto our local-table columns.
  2. Coerce types (date strings → date, "" → None, etc.).
  3. Inject static fields (e.g. `market='TWSE'`).

Adding a new dataset is one entry here + (optionally) a custom
`row_transform` callback for non-trivial unit conversions.

Datasets without a mapping fall through with a `MappingNotFoundError`
that the runner records as `skipped` in `backfill_progress` — Phase 1
schema work continues independently of which datasets the runner
actually knows how to ingest."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


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


# ── Mappings ─────────────────────────────────────────────────────
#
# Phase 2 ships mappings for the headline 5 datasets — adding more is
# an append-only operation. The pattern is mechanical: copy an entry,
# swap dataset_code / local_table / column_map.


def _row_ohlcv(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "open": _to_decimal(row.get("open")),
        "high": _to_decimal(row.get("high")),
        "low": _to_decimal(row.get("low")),
        "close": _to_decimal(row.get("close")),
        "volume": _to_int(row.get("volume")),
        "source": row.get("source", "finmind"),
    }


def _row_margin(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "margin_purchase": _to_int(row.get("margin_purchase")),
        "margin_sale": _to_int(row.get("margin_sale")),
        "margin_balance": _to_int(row.get("margin_balance")),
        "short_sale": _to_int(row.get("short_sale")),
        "short_cover": _to_int(row.get("short_cover")),
        "short_balance": _to_int(row.get("short_balance")),
        "source": row.get("source", "finmind"),
    }


def _row_institutional(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "foreign_buy": _to_int(row.get("foreign_buy")),
        "foreign_sell": _to_int(row.get("foreign_sell")),
        "sitc_buy": _to_int(row.get("sitc_buy")),
        "sitc_sell": _to_int(row.get("sitc_sell")),
        "dealer_buy": _to_int(row.get("dealer_buy")),
        "dealer_sell": _to_int(row.get("dealer_sell")),
        "source": row.get("source", "finmind"),
    }


def _row_revenue(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "revenue": _to_decimal(row.get("revenue")),
        "revenue_yoy": _to_decimal(row.get("revenue_yoy")),
        "revenue_mom": _to_decimal(row.get("revenue_mom")),
        "source": row.get("source", "finmind"),
    }


def _row_total_margin(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "ts": _to_date(row.get("ts")),
        "margin_balance": _to_int(row.get("margin_balance")),
        "margin_purchase": _to_int(row.get("margin_purchase")),
        "margin_sale": _to_int(row.get("margin_sale")),
        "short_balance": _to_int(row.get("short_balance")),
        "short_sale": _to_int(row.get("short_sale")),
        "short_cover": _to_int(row.get("short_cover")),
        "source": row.get("source", "finmind"),
    }


MAPPINGS: dict[str, DatasetMapping] = {
    "TaiwanStockPrice": DatasetMapping(
        dataset_code="TaiwanStockPrice",
        local_table="ohlcv_daily",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "open": "open",
            "max": "high",
            "min": "low",
            "close": "close",
            "Trading_Volume": "volume",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_ohlcv,
    ),
    "TaiwanStockMarginPurchaseShortSale": DatasetMapping(
        dataset_code="TaiwanStockMarginPurchaseShortSale",
        local_table="tw_margin_daily",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "MarginPurchaseBuy": "margin_purchase",
            "MarginPurchaseSell": "margin_sale",
            "MarginPurchaseTodayBalance": "margin_balance",
            "ShortSaleBuy": "short_cover",
            "ShortSaleSell": "short_sale",
            "ShortSaleTodayBalance": "short_balance",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_margin,
    ),
    "TaiwanStockInstitutionalInvestorsBuySell": DatasetMapping(
        dataset_code="TaiwanStockInstitutionalInvestorsBuySell",
        local_table="tw_institutional_daily",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "Foreign_Investor_Buy": "foreign_buy",
            "Foreign_Investor_Sell": "foreign_sell",
            "Investment_Trust_Buy": "sitc_buy",
            "Investment_Trust_Sell": "sitc_sell",
            "Dealer_Buy": "dealer_buy",
            "Dealer_Sell": "dealer_sell",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_institutional,
    ),
    "TaiwanStockMonthRevenue": DatasetMapping(
        dataset_code="TaiwanStockMonthRevenue",
        local_table="tw_revenue_monthly",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "revenue": "revenue",
            "revenue_year": "revenue_yoy",
            "revenue_month": "revenue_mom",
        },
        pk_columns=("symbol", "ts"),
        extra={"source": "finmind"},
        row_transform=_row_revenue,
    ),
    "TaiwanStockTotalMarginPurchaseShortSale": DatasetMapping(
        dataset_code="TaiwanStockTotalMarginPurchaseShortSale",
        local_table="tw_total_margin_daily",
        column_map={
            "date": "ts",
            "MarginPurchaseTodayBalance": "margin_balance",
            "MarginPurchaseBuy": "margin_purchase",
            "MarginPurchaseSell": "margin_sale",
            "ShortSaleTodayBalance": "short_balance",
            "ShortSaleSell": "short_sale",
            "ShortSaleBuy": "short_cover",
        },
        pk_columns=("market", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_total_margin,
    ),
}


def find_mapping(dataset_code: str) -> DatasetMapping:
    """Resolve dataset_code → DatasetMapping. Raises
    MappingNotFoundError when the runner doesn't know how to
    transform this dataset's payload yet."""
    m = MAPPINGS.get(dataset_code)
    if m is None:
        raise MappingNotFoundError(
            f"no ingest mapping for {dataset_code} — append one to "
            f"finmind/ingest/mappings.py to enable backfill"
        )
    return m


def transform_row(
    finmind_row: dict[str, Any], mapping: DatasetMapping
) -> dict[str, Any]:
    """FinMind row dict → local-table row dict.

    Three steps:
      1. Rename columns per `column_map` (drop unknown columns).
      2. Inject `extra` static fields.
      3. Run `row_transform` if present (type coercion + derived cols).
    """
    renamed: dict[str, Any] = {}
    for fm_col, local_col in mapping.column_map.items():
        if fm_col in finmind_row:
            renamed[local_col] = finmind_row[fm_col]
    renamed.update(mapping.extra)
    if mapping.row_transform is not None:
        renamed = mapping.row_transform(renamed)
    return renamed
