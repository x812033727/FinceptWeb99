"""TWSE / TPEx 產業別 code → Chinese-name resolver.

Why
---
TWSE's `t187ap03_L` (上市公司基本資料) endpoint used to return the
Chinese industry name directly in its `產業別` field, but at some
point started returning the standardised industry CODE instead (e.g.
`"01"` for 水泥工業, `"27"` for 通信網路業). Our `_industry_map` in
`tw_market_service` was storing whatever the upstream gave, so the
discussion context block + per-stock detail page started showing
opaque numeric codes that the LLM personas can't reason about.

This module provides one helper — `resolve_industry_label` — that
translates codes to names while passing through values that already
look like Chinese names (TPEx + cached older rows often do). Codes
not in the map fall through to the original value so a TWSE schema
addition (new industry code) doesn't blank the field.

Source
------
The 32-entry mapping below is the standardised TWSE 上市公司 industry
classification. Codes are stable — TWSE has only added new ones over
the years, never renumbered existing ones. The codes for 水泥工業
(01), 半導體業 (24), 通信網路業 (27), etc. are referenced in
financial regulations and have been unchanged for ~20 years.
"""
from __future__ import annotations

# TWSE 上市公司 產業別代碼 → 中文名稱
# https://mops.twse.com.tw/server-java/t51sb01 (lookup table)
INDUSTRY_CODES: dict[str, str] = {
    "01": "水泥工業",
    "02": "食品工業",
    "03": "塑膠工業",
    "04": "紡織纖維",
    "05": "電機機械",
    "06": "電器電纜",
    "08": "玻璃陶瓷",
    "09": "造紙工業",
    "10": "鋼鐵工業",
    "11": "橡膠工業",
    "12": "汽車工業",
    "14": "建材營造業",
    "15": "航運業",
    "16": "觀光餐旅",
    "17": "金融保險業",
    "18": "貿易百貨",
    "19": "綜合",
    "20": "其他",
    "21": "化學工業",
    "22": "生技醫療業",
    "23": "油電燃氣業",
    "24": "半導體業",
    "25": "電腦及週邊設備業",
    "26": "光電業",
    "27": "通信網路業",
    "28": "電子零組件業",
    "29": "電子通路業",
    "30": "資訊服務業",
    "31": "其他電子業",
    "32": "文化創意業",
    "33": "農業科技業",
    "34": "電子商務",
    "36": "存託憑證",
    "80": "管理股票",
    "91": "存託憑證",
}


def resolve_industry_label(raw: str | None) -> str | None:
    """Translate a TWSE / TPEx 產業別 value to a Chinese name.

    Behaviour:
      - `None` / empty string → returns the input unchanged.
      - Pure-numeric string like `"27"` or `" 01 "` → looks up the
        canonical name (`"通信網路業"` / `"水泥工業"`).
      - Numeric not in the map (e.g. a newly-added code TWSE adds
        after this module last shipped) → returns the original value
        so the field at least surfaces *something* to operators.
      - Non-numeric string (Chinese name from TPEx, or already-resolved
        cached value) → returns it unchanged.

    Idempotent: `resolve(resolve(x)) == resolve(x)` for all inputs,
    so it's safe to call defensively at any layer.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return s
    # Treat as code only when the entire string is digits — protects
    # against false matches on Chinese names that happen to start
    # with a digit (none exist today, but the rule is cheap insurance).
    if s.isdigit():
        # Pad single-digit input to two chars: TWSE codes are written
        # zero-padded ("01", "02", ...) so "1" should also resolve.
        key = s.zfill(2)
        return INDUSTRY_CODES.get(key, s)
    return s
