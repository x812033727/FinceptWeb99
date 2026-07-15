"""Traceable Taiwan instrument classification and effective-dated trading rules."""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_company_info import TwCompanyInfo
from models.tw_security_master import TwSecurityMasterVersion
from services.tw_symbol_service import is_etf

ETF_CODE_SOURCE = "https://accessibility.twse.com.tw/downloads/zh/ETF/ETFcode.pdf"
ETF_OVERVIEW_SOURCE = "https://www.twse.com.tw/zh/products/securities/etf/overview/introduction.html"
TAX_SOURCE = (
    "https://www.etax.nat.gov.tw/etwmain/tax-info/understanding/"
    "tax-q-and-a/national/securities-transaction-tax/taxation-scope/7r3MjNB"
)
BOND_ETF_TAX_HOLIDAY_END = date(2026, 12, 31)


def infer_security_profile(
    symbol: str,
    *,
    as_of: date,
    name_zh: str | None = None,
    exchange: str = "TWSE",
) -> dict[str, Any]:
    """Infer only rules explicitly encoded by TWSE's published symbol taxonomy."""
    normalized = (symbol or "").strip().upper()
    suffix = normalized[-1:] if normalized[-1:].isalpha() else ""
    etf = is_etf(normalized)
    leveraged = etf and suffix in {"L", "M"}
    inverse = etf and suffix in {"R", "S"}
    bond = etf and suffix in {"B", "C", "D"}
    futures = etf and suffix in {"U", "V"}
    multi_asset = etf and suffix == "T"
    active = etf and suffix in {"A", "D"}
    foreign_currency = etf and suffix in {"C", "K", "M", "S", "V"}

    if not etf:
        instrument_type, asset_class = "stock", "equity"
    elif bond:
        instrument_type, asset_class = (
            "etf_bond_active" if active else "etf_bond",
            "fixed_income",
        )
    elif futures:
        instrument_type, asset_class = "etf_futures", "commodity"
    elif multi_asset:
        instrument_type, asset_class = "etf_multi_asset", "multi_asset"
    elif leveraged:
        instrument_type, asset_class = "etf_leveraged", "unknown"
    elif inverse:
        instrument_type, asset_class = "etf_inverse", "unknown"
    elif active:
        instrument_type, asset_class = "etf_equity_active", "equity"
    else:
        instrument_type, asset_class = "etf_equity", "equity"

    if not etf:
        sell_tax_bps, tax_rule = 30.0, "stock_0.3pct"
    elif bond and not leveraged and not inverse and as_of <= BOND_ETF_TAX_HOLIDAY_END:
        sell_tax_bps, tax_rule = 0.0, "bond_etf_suspended_through_2026"
    else:
        sell_tax_bps, tax_rule = 10.0, "beneficial_certificate_0.1pct"

    return {
        "symbol": normalized,
        "name_zh": name_zh,
        "exchange": exchange or "TWSE",
        "instrument_type": instrument_type,
        "asset_class": asset_class,
        "is_etf": etf,
        "is_bond_etf": bond,
        "is_leveraged": leveraged,
        "is_inverse": inverse,
        "is_foreign_currency": foreign_currency,
        "board_lot_size": 1000,
        "odd_lot_size": 1,
        "sell_tax_bps": sell_tax_bps,
        "tax_rule_code": tax_rule,
        "classification_source_url": ETF_CODE_SOURCE if etf else ETF_OVERVIEW_SOURCE,
        "tax_source_url": TAX_SOURCE,
        "confidence": "published_code_rule" if etf else "market_master",
        "is_manual_override": False,
        "override_reason": None,
        "overridden_by": None,
    }


def _serialize(row: TwSecurityMasterVersion) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "effective_from": row.effective_from,
        "effective_to": row.effective_to,
        "name_zh": row.name_zh,
        "exchange": row.exchange,
        "instrument_type": row.instrument_type,
        "asset_class": row.asset_class,
        "is_etf": row.is_etf,
        "is_bond_etf": row.is_bond_etf,
        "is_leveraged": row.is_leveraged,
        "is_inverse": row.is_inverse,
        "board_lot_size": row.board_lot_size,
        "odd_lot_size": row.odd_lot_size,
        "sell_tax_bps": float(row.sell_tax_bps),
        "tax_rule_code": row.tax_rule_code,
        "source": row.source,
        "classification_source_url": row.classification_source_url,
        "tax_source_url": row.tax_source_url,
        "confidence": row.confidence,
        "is_manual_override": row.is_manual_override,
        "override_reason": row.override_reason,
        "overridden_by": row.overridden_by,
        "captured_at": row.captured_at,
        "fallback": False,
    }


async def resolve_security_profiles(
    db: AsyncSession | None,
    symbols: list[str] | set[str],
    *,
    as_of: date,
) -> dict[str, dict[str, Any]]:
    normalized = sorted({str(symbol).strip().upper() for symbol in symbols if symbol})
    if not normalized:
        return {}
    rows = []
    if db is not None:
        rows = (await db.scalars(
            select(TwSecurityMasterVersion).where(
                TwSecurityMasterVersion.symbol.in_(normalized),
                TwSecurityMasterVersion.effective_from <= as_of,
                or_(
                    TwSecurityMasterVersion.effective_to.is_(None),
                    TwSecurityMasterVersion.effective_to >= as_of,
                ),
            ).order_by(
                TwSecurityMasterVersion.symbol,
                TwSecurityMasterVersion.is_manual_override.desc(),
                TwSecurityMasterVersion.effective_from.desc(),
                TwSecurityMasterVersion.captured_at.desc(),
            )
        )).all()
    resolved: dict[str, dict[str, Any]] = {}
    for row in rows:
        resolved.setdefault(row.symbol, _serialize(row))

    missing = [symbol for symbol in normalized if symbol not in resolved]
    companies: dict[str, TwCompanyInfo] = {}
    if missing and db is not None:
        company_rows = (await db.scalars(
            select(TwCompanyInfo).where(TwCompanyInfo.symbol.in_(missing))
        )).all()
        companies = {row.symbol: row for row in company_rows}
    for symbol in missing:
        company = companies.get(symbol)
        profile = infer_security_profile(
            symbol,
            as_of=as_of,
            name_zh=company.name_zh if company else None,
            exchange=company.exchange if company else "TWSE",
        )
        resolved[symbol] = {
            **profile,
            "effective_from": as_of,
            "effective_to": None,
            "source": "runtime_fallback",
            "captured_at": None,
            "fallback": True,
        }
    return resolved


async def sync_security_master(db: AsyncSession, *, as_of: date) -> dict[str, Any]:
    companies = (await db.scalars(select(TwCompanyInfo))).all()
    existing_rows = (await db.scalars(select(TwSecurityMasterVersion).where(
        TwSecurityMasterVersion.effective_from == as_of,
        TwSecurityMasterVersion.source == "twse_tpex_master",
    ))).all()
    existing_by_symbol = {row.symbol: row for row in existing_rows}
    created = updated = 0
    for company in companies:
        profile = infer_security_profile(
            company.symbol,
            as_of=as_of,
            name_zh=company.name_zh,
            exchange=company.exchange,
        )
        existing = existing_by_symbol.get(company.symbol)
        values = {key: value for key, value in profile.items() if key != "is_foreign_currency"}
        if existing is None:
            db.add(TwSecurityMasterVersion(
                **values,
                effective_from=as_of,
                effective_to=(BOND_ETF_TAX_HOLIDAY_END if profile["is_bond_etf"]
                              and profile["sell_tax_bps"] == 0 else None),
                source="twse_tpex_master",
            ))
            created += 1
        else:
            for key, value in values.items():
                setattr(existing, key, value)
            existing.effective_to = (
                BOND_ETF_TAX_HOLIDAY_END
                if profile["is_bond_etf"] and profile["sell_tax_bps"] == 0
                else None
            )
            updated += 1
    await db.commit()
    return {
        "as_of": as_of,
        "source_rows": len(companies),
        "created": created,
        "updated": updated,
        "source": "twse_tpex_master",
    }


async def upsert_manual_override(
    db: AsyncSession,
    *,
    symbol: str,
    effective_from: date,
    effective_to: date | None,
    values: dict[str, Any],
    reason: str,
    admin_id: str,
) -> dict[str, Any]:
    if effective_to is not None and effective_to < effective_from:
        raise ValueError("effective_to must be on or after effective_from")
    baseline = (await resolve_security_profiles(
        db, [symbol], as_of=effective_from,
    ))[symbol.strip().upper()]
    allowed = {
        "name_zh", "exchange", "instrument_type", "asset_class", "is_etf",
        "is_bond_etf", "is_leveraged", "is_inverse", "board_lot_size",
        "odd_lot_size", "sell_tax_bps", "tax_rule_code",
        "classification_source_url", "tax_source_url", "confidence",
    }
    merged = {key: baseline[key] for key in allowed}
    merged.update({key: value for key, value in values.items() if key in allowed and value is not None})
    normalized = symbol.strip().upper()
    row = await db.scalar(select(TwSecurityMasterVersion).where(
        TwSecurityMasterVersion.symbol == normalized,
        TwSecurityMasterVersion.effective_from == effective_from,
        TwSecurityMasterVersion.source == "manual_override",
    ))
    if row is None:
        row = TwSecurityMasterVersion(
            symbol=normalized,
            effective_from=effective_from,
            source="manual_override",
            **merged,
        )
        db.add(row)
    else:
        for key, value in merged.items():
            setattr(row, key, value)
    row.effective_to = effective_to
    row.is_manual_override = True
    row.override_reason = reason.strip()
    row.overridden_by = admin_id
    await db.commit()
    await db.refresh(row)
    return _serialize(row)
