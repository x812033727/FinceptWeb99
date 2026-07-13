"""Register every ORM model as soon as the ``models`` package (or any
``models.<submodule>``) is first imported.

Models otherwise load lazily, which makes cross-model ForeignKeys
fragile: a string ``ForeignKey("other_table.id")`` is resolved against
the shared ``Base.metadata`` only at flush time (table sorting), so if a
worker registers table A before the model owning table B is imported,
every ``session.flush()`` touching A raises ``NoReferencedTableError``
(seen in production as ``discussions.sweep_id -> backtest_sweeps`` and
``backtest_sweeps.strategy_id -> discussion_strategy_templates``).

Importing all model modules here — for their registration side effect —
makes the metadata complete the moment anything under ``models`` is
imported (which the app does at startup), removing the import-order
fragility for the whole class. String FKs stay lazily resolved, so the
import order within this list does not matter.
"""
from importlib import import_module as _import_module
from pkgutil import iter_modules as _iter_modules

# Import every sibling module (skip private/dunder). Discovered
# dynamically so a newly added model is picked up without editing a list.
for _mod in _iter_modules(__path__):
    if not _mod.name.startswith("_"):
        _import_module(f"{__name__}.{_mod.name}")

del _import_module, _iter_modules, _mod
