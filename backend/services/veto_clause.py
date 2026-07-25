"""The macro-veto downgrade clause text (spec Part 1 governance).

Neutral home for the clause constant: `scripts/apply_veto_downgrade.py`
(the operator tool that applies/reverts it against
`DiscussionAutoRunConfig.rules`) and `tasks/monitor_strategy_health.py`
(the daily guard that watches for revert conditions and cross-strategy
leakage) both need it, and a `tasks/*` module importing from
`scripts/*` would be backwards — scripts are operator entry points that
depend on the app, not the other way around. Pulling the constant out
of the script and into a service module lets both sides import it
without either depending on the other.
"""
from __future__ import annotations

VETO_DOWNGRADE_CLAUSE = (
    "\n\n【僅適用於量價訊號策略場次】總經逆風(外資台指期淨空、三大法人連續"
    "賣超、台VIX 偏高等系統性風險)不得作為否決個股的唯一理由。當候選同時"
    "滿足技術面與籌碼面進場條件時仍應給出推薦,但總經逆風時必須:(1) 建議"
    "部位上限減半;(2) 停損位收緊並明確標出;(3) 在 risks 首條標注「總經"
    "逆風環境」。僅當個股本身不符技術或籌碼條件、或風報比不足時才棄權。"
)

__all__ = ["VETO_DOWNGRADE_CLAUSE"]
