from datetime import date

import pytest

from models.tw_company_info import TwCompanyInfo
from services.tw_security_master_service import (
    infer_security_profile,
    resolve_security_profiles,
    sync_security_master,
    upsert_manual_override,
)


def test_published_etf_code_taxonomy_covers_current_suffixes():
    equity = infer_security_profile("009801", as_of=date(2026, 7, 15))
    bond = infer_security_profile("00980B", as_of=date(2026, 7, 15))
    active_bond = infer_security_profile("00980D", as_of=date(2026, 7, 15))
    leveraged = infer_security_profile("00980L", as_of=date(2026, 7, 15))
    inverse_fx = infer_security_profile("00980S", as_of=date(2026, 7, 15))
    futures = infer_security_profile("00980U", as_of=date(2026, 7, 15))

    assert equity["instrument_type"] == "etf_equity"
    assert bond["instrument_type"] == "etf_bond"
    assert bond["sell_tax_bps"] == 0
    assert active_bond["instrument_type"] == "etf_bond_active"
    assert leveraged["is_leveraged"] is True
    assert inverse_fx["is_inverse"] is True
    assert inverse_fx["is_foreign_currency"] is True
    assert futures["instrument_type"] == "etf_futures"


def test_bond_etf_tax_sunset_is_point_in_time():
    before = infer_security_profile("00980B", as_of=date(2026, 12, 31))
    after = infer_security_profile("00980B", as_of=date(2027, 1, 1))
    assert before["sell_tax_bps"] == 0
    assert before["tax_rule_code"] == "bond_etf_suspended_through_2026"
    assert after["sell_tax_bps"] == 10
    assert after["tax_rule_code"] == "beneficial_certificate_0.1pct"


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_resolver_prefers_manual_override(db_session):
    db_session.add_all([
        TwCompanyInfo(symbol="2330", exchange="TWSE", name_zh="台積電"),
        TwCompanyInfo(symbol="00980B", exchange="TWSE", name_zh="測試債券ETF"),
    ])
    await db_session.commit()

    first = await sync_security_master(db_session, as_of=date(2026, 7, 15))
    second = await sync_security_master(db_session, as_of=date(2026, 7, 15))
    assert first["created"] == 2
    assert second["created"] == 0
    assert second["updated"] == 2

    resolved = await resolve_security_profiles(
        db_session, {"2330", "00980B"}, as_of=date(2026, 7, 15),
    )
    assert resolved["2330"]["sell_tax_bps"] == 30
    assert resolved["00980B"]["sell_tax_bps"] == 0
    assert resolved["00980B"]["effective_to"] == date(2026, 12, 31)

    override = await upsert_manual_override(
        db_session,
        symbol="00980B",
        effective_from=date(2026, 7, 15),
        effective_to=date(2026, 8, 1),
        values={"sell_tax_bps": 7.5, "tax_rule_code": "broker_confirmed"},
        reason="券商確認特殊商品分類",
        admin_id="admin-1",
    )
    assert override["is_manual_override"] is True
    resolved = await resolve_security_profiles(
        db_session, {"00980B"}, as_of=date(2026, 7, 16),
    )
    assert resolved["00980B"]["sell_tax_bps"] == 7.5
    assert resolved["00980B"]["source"] == "manual_override"
    assert resolved["00980B"]["overridden_by"] == "admin-1"


@pytest.mark.asyncio
async def test_missing_master_row_is_explicit_fallback(db_session):
    resolved = await resolve_security_profiles(
        db_session, ["00632R"], as_of=date(2026, 7, 15),
    )
    assert resolved["00632R"]["fallback"] is True
    assert resolved["00632R"]["source"] == "runtime_fallback"
    assert resolved["00632R"]["is_inverse"] is True
