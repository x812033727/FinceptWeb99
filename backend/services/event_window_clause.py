"""The event-window handling clause text (2026-08 miss review).

Background: the live auto-run rules never contained an event-window
rule, yet the panel invented one ("法說/除息落在 5 日窗內 → 方向訊號
失效") and propagated it across sessions — through August's earnings
and ex-dividend season it abstained 8 of 12 sessions straight through
a +6% TAIEX rebound. It was also applied inconsistently: 2026-08-06
price_signal vetoed 3605 (a self-described 教科書級突破, +22.2% over
the window) for an ex-dividend date, then recommended 3702 (−2.4%)
which had its own earnings call inside the same window. The panel
likewise cites risk-reward floors (3:1, 2:1 …) that appear nowhere in
the rules.

This clause doesn't ban event awareness — it pins events to the
position-sizing lever (the same shape as the macro-veto downgrade
clause) instead of the veto lever, and requires consistency.

Neutral service-module home for the constant, mirroring
`services.veto_clause`: the operator script and any future guard both
import from here.
"""
from __future__ import annotations

from datetime import UTC, datetime

# When `--apply` was actually run against the live config. Update this
# stamp if the clause is ever reverted and re-applied — see
# `services.veto_clause.VETO_DOWNGRADE_ADOPTED_AT` for the precedent.
EVENT_WINDOW_ADOPTED_AT = datetime(2026, 8, 14, 0, 0, 0, tzinfo=UTC)

EVENT_WINDOW_CLAUSE = (
    "\n\n【事件窗處理準則(適用所有策略場次)】持有窗(5 個交易日)內既知的"
    "公司事件(法說會、除息、除權)不得單獨作為否決個股或棄權的理由:"
    "(1) 除息/除權屬機械性、可預期事件,應以「按息值調整目標價與停損」處理,"
    "不得以除息為由否決;(2) 法說會屬不確定性事件,得作為部位上限減半、停損"
    "收緊的理由;僅當該股同時欠缺技術面或籌碼面確認時才可否決;(3) 同場多檔"
    "候選皆有事件窗時,本準則必須一致適用,不得選擇性引用事件窗否決其中一檔"
    "卻推薦另一檔;(4) 不得引用本規則未載明的固定風報比門檻(如 3:1)作為"
    "否決的唯一理由。"
)

__all__ = ["EVENT_WINDOW_ADOPTED_AT", "EVENT_WINDOW_CLAUSE"]
