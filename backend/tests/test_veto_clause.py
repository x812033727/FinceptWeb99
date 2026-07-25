"""The macro-veto downgrade clause text (spec Part 1 governance).

Pins the clause against the spec's four scoping phrases so a future
edit to the wording can't accidentally drop the part that makes it
strategy-scoped and position/stop-loss-qualified rather than a bare
veto override.
"""
from __future__ import annotations

from services.veto_clause import VETO_DOWNGRADE_CLAUSE


def test_veto_downgrade_clause_contains_spec_scoping_phrases():
    for phrase in (
        "僅適用於量價訊號策略場次",
        "部位上限減半",
        "停損位收緊",
        "總經逆風環境",
    ):
        assert phrase in VETO_DOWNGRADE_CLAUSE
