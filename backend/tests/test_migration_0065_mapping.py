"""Data-mapping test for migration 0065 (price_alerts 規則引擎欄位).

Alembic migrations don't run in the SQLite test harness, so the
condition→condition_type backfill is exposed by the migration module
as `_mapping_statements()` (plain portable SQL) and exercised here
against an in-memory SQLite table shaped like pre-0065 price_alerts.
"""
import importlib.util
import sqlite3
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "db" / "migrations" / "versions" / "0065_alert_rule_engine.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0065", MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_condition_type_map_covers_both_legacy_values():
    mod = _load_migration()
    assert mod._CONDITION_TYPE_MAP == {
        "above": "price_above",
        "below": "price_below",
    }
    assert mod.revision == "0065"
    assert mod.down_revision == "0064"


def test_mapping_statements_backfill_existing_rows():
    """Existing above/below rows land on the right condition_type;
    rows already carrying a non-default type are left alone."""
    mod = _load_migration()
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE price_alerts ("
        " id INTEGER PRIMARY KEY,"
        " condition TEXT,"
        " condition_type TEXT NOT NULL DEFAULT 'price_above')"
    )
    con.executemany(
        "INSERT INTO price_alerts (id, condition) VALUES (?, ?)",
        [(1, "above"), (2, "below"), (3, "above"), (4, "below")],
    )

    for stmt in mod._mapping_statements():
        con.execute(stmt)

    rows = dict(con.execute("SELECT id, condition_type FROM price_alerts"))
    assert rows == {
        1: "price_above",
        2: "price_below",
        3: "price_above",
        4: "price_below",
    }


def test_mapping_statements_are_condition_scoped():
    """Each UPDATE is WHERE-scoped to one legacy value — a blanket
    UPDATE would clobber the other direction."""
    mod = _load_migration()
    stmts = mod._mapping_statements()
    assert len(stmts) == 2
    assert all("WHERE condition = " in s for s in stmts)
