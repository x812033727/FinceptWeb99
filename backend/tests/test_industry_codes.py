"""Tests for data.tw.industry_codes resolver."""
from data.tw.industry_codes import INDUSTRY_CODES, resolve_industry_label


def test_resolves_known_two_digit_codes_to_chinese_name():
    """The user-reported symptoms came from rows with codes like
    `"01"` (台泥, 1101) and `"27"` (全新, 2455). These must
    translate to canonical Chinese names."""
    assert resolve_industry_label("01") == "水泥工業"
    assert resolve_industry_label("24") == "半導體業"
    assert resolve_industry_label("27") == "通信網路業"
    assert resolve_industry_label("28") == "電子零組件業"


def test_zero_pads_single_digit_codes():
    """TWSE writes codes zero-padded ("01", "02") but defensive
    upstream parsers may strip the leading zero. Either form must
    resolve to the same Chinese name."""
    assert resolve_industry_label("1") == "水泥工業"
    assert resolve_industry_label("01") == "水泥工業"
    assert resolve_industry_label("9") == "造紙工業"


def test_strips_surrounding_whitespace():
    """Real upstream payloads sometimes carry stray whitespace from
    HTML scraping fallbacks."""
    assert resolve_industry_label(" 27 ") == "通信網路業"
    assert resolve_industry_label("\t01\n") == "水泥工業"


def test_passes_through_chinese_names_unchanged():
    """TPEx still returns Chinese names directly, and the in-memory
    `_industry_map` may already hold resolved values when this is
    called for a second time. Idempotency is the contract — calling
    `resolve(resolve(x))` must equal `resolve(x)`."""
    assert resolve_industry_label("通信網路業") == "通信網路業"
    assert resolve_industry_label("半導體業") == "半導體業"
    # Idempotent
    assert resolve_industry_label(resolve_industry_label("27")) == "通信網路業"


def test_unknown_numeric_code_falls_through_to_original():
    """A future TWSE schema addition (new industry code) shouldn't
    blank the field — surfacing the raw code is better than null
    so operators see *something* and can update the mapping."""
    assert resolve_industry_label("99") == "99"   # not in our map
    assert resolve_industry_label("999") == "999"


def test_none_and_empty_string_pass_through():
    """Defensive: caller may forward `None` from a raw upstream row
    where the field was missing. Returning None preserves the
    "no data" signal rather than silently coercing to empty string."""
    assert resolve_industry_label(None) is None
    assert resolve_industry_label("") == ""


def test_industry_codes_table_covers_all_user_visible_sectors():
    """Sanity-check the table size — at least the major sectors that
    the discussion context routinely surfaces (水泥, 半導體, 電子零組件,
    通信網路, 金融保險, 食品) must be present. Catches accidental
    deletion / typo of a code key during future edits."""
    expected = {
        "01": "水泥工業",
        "02": "食品工業",
        "17": "金融保險業",
        "24": "半導體業",
        "27": "通信網路業",
        "28": "電子零組件業",
    }
    for code, name in expected.items():
        assert INDUSTRY_CODES.get(code) == name


def test_get_industry_resolves_codes_from_stale_in_memory_map():
    """Integration: a process whose `_industry_map` was populated
    BEFORE the resolver landed (i.e. still holds raw codes like
    `"27"`) must still surface the Chinese name via `get_industry()`.
    Otherwise users would have to wait for the daily refresh cron
    before the discussion context cleans up."""
    from services import tw_market_service

    # Simulate stale state: code stored verbatim in the in-memory map.
    original = tw_market_service._industry_map.copy()
    try:
        tw_market_service._industry_map["2455"] = "27"
        assert tw_market_service.get_industry("2455") == "通信網路業"
    finally:
        tw_market_service._industry_map.clear()
        tw_market_service._industry_map.update(original)
