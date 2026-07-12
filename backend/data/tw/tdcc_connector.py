"""TDCC (臺灣集中保管結算所) open-data connector.

Currently exposes the 集保戶股權分散表 (share-ownership distribution by
holding bracket) — FinMind's `TaiwanStockHoldingSharesPer`. TDCC
publishes ONE open-data CSV covering every listed stock for the latest
weekly snapshot, so this is a market-wide fetch: the whole file in one
request, no per-symbol fan-out.

TDCC only serves the most recent week via open data (no historical
archive), so the date range args are advisory — the connector always
returns whatever week the file currently holds. The row's own 資料日期
is authoritative.

Emits FinMind's raw column names so the existing
`TaiwanStockHoldingSharesPer` DatasetMapping projects them onto
tw_holdings_aggregates unchanged.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

# 集保戶股權分散表 open-data endpoint (id=1-5). UTF-8 with BOM.
_HOLDING_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=30.0)
_USER_AGENT = "Mozilla/5.0 (Linux; Server) FinceptWeb/0.5 (tdcc-ingest)"


def _parse_roc_or_greg_day(raw: str) -> str | None:
    """TDCC 資料日期 is Gregorian YYYYMMDD (e.g. 20260703). Returns an
    ISO date string, or None when unparseable."""
    raw = (raw or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _parse_holding_csv(body: str) -> list[dict[str, Any]]:
    """Parse the TDCC 股權分散 CSV into FinMind `TaiwanStockHoldingSharesPer`
    raw-shaped rows: {date, stock_id, HoldingSharesLevel, people, unit,
    percent}. Header (BOM-stripped):
        資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%
    """
    if not body or not body.strip():
        return []
    # Strip UTF-8 BOM the endpoint prepends so the first header matches.
    reader = csv.reader(io.StringIO(body.lstrip("﻿")))
    rows = list(reader)
    if len(rows) < 2:
        return []
    header = [h.strip() for h in rows[0]]
    idx = {name: i for i, name in enumerate(header)}
    required = ("資料日期", "證券代號", "持股分級", "人數", "股數", "占集保庫存數比例%")
    if any(c not in idx for c in required):
        log.warning("tdcc.holding.unrecognized_csv_header", extra={"header": header})
        return []

    out: list[dict[str, Any]] = []
    for line in rows[1:]:
        if len(line) < len(header):
            continue
        d = _parse_roc_or_greg_day(line[idx["資料日期"]])
        stock_id = (line[idx["證券代號"]] or "").strip()
        if d is None or not stock_id:
            continue
        out.append({
            "date": d,
            "stock_id": stock_id,
            "HoldingSharesLevel": (line[idx["持股分級"]] or "").strip(),
            "people": (line[idx["人數"]] or "").strip(),
            "unit": (line[idx["股數"]] or "").strip(),
            "percent": (line[idx["占集保庫存數比例%"]] or "").strip(),
        })
    return out


async def get_holding_shares_per(
    start: date | None = None, end: date | None = None,
) -> list[dict[str, Any]]:
    """集保戶股權分散表 — every listed stock's 15-bracket holder/share
    distribution for the latest weekly snapshot. `start`/`end` are
    advisory (TDCC serves only the current week). Empty list on
    connector failure or empty body."""
    headers = {"User-Agent": _USER_AGENT}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            r = await client.get(_HOLDING_URL, headers=headers)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("tdcc.holding.fetch_failed", extra={"error": str(exc)})
            raise
    return _parse_holding_csv(r.text)


__all__ = ["get_holding_shares_per"]
