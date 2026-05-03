"""Independent DeclarativeBase for the FinMind subsystem.

Mirrors the main app's `db.base.Base` naming convention (so future
extraction to a separate service produces identical constraint names)
but is a different `MetaData` instance — `Base.metadata.create_all()`
on this base only touches FinMind tables, never the main app's.
"""
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=convention)
