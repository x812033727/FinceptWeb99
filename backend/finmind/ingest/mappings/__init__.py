"""FinMind ingest mappings — split into transform + registry submodules.

Backward-compat shim for the original flat `mappings.py`. The 17
symbols imported externally (DatasetMapping, MappingNotFoundError,
MAPPINGS, find_mapping, transform_row, plus a handful of `_row_*` /
`_batch_*` / `_pivot_*` private helpers reached by tests) are
re-exported here so existing imports continue to resolve.

New code should import from the concrete submodule
(`mappings._registry`, `mappings._row_transforms`, etc.) — that's
where the symbol actually lives. The shim exists so the 8 importer
files (runner + 7 test files) don't all need editing in one PR.
"""
from finmind.ingest.mappings._batch_transforms import (
    _batch_cb_daily,
    _batch_futures_dealer_volume,
    _batch_futures_oi_largetraders,
    _batch_govt_bank_flow,
    _batch_option_inst_regular,
    _batch_stock_tick,
    _normalize_period,
    _pivot_balance_sheet,
    _pivot_cash_flow,
    _pivot_income_statement,
    _pivot_total_institutional,
)
from finmind.ingest.mappings._registry import MAPPINGS, find_mapping, transform_row
from finmind.ingest.mappings._row_transforms import _row_stock_info_with_warrant
from finmind.ingest.mappings._types import DatasetMapping, MappingNotFoundError

__all__ = [
    "DatasetMapping",
    "MAPPINGS",
    "MappingNotFoundError",
    "_batch_cb_daily",
    "_batch_futures_dealer_volume",
    "_batch_futures_oi_largetraders",
    "_batch_govt_bank_flow",
    "_batch_option_inst_regular",
    "_batch_stock_tick",
    "_normalize_period",
    "_pivot_balance_sheet",
    "_pivot_cash_flow",
    "_pivot_income_statement",
    "_pivot_total_institutional",
    "_row_stock_info_with_warrant",
    "find_mapping",
    "transform_row",
]
