"""ORM model for `tw_company_info` (migration `0060`).

Deploy-survival layer for the TW symbol → 公司簡稱 / 產業 / 上市櫃 maps
that ``tw_market_service`` builds from a TWSE refresh. Without this
table the maps live only in process memory + a 7-day Redis snapshot;
on a fresh deploy with empty Redis AND a TWSE upstream that's blocking
the cloud-IP range, every pod cold-starts with ``_name_map == {}`` and
discussion chips render bare ticker codes until the next TWSE refresh
window opens.

Population: ``refresh_symbol_map()`` upserts every entry after a
successful TWSE fetch. Cold-start: ``load_symbol_map_from_db()`` reads
all rows before the upstream refresh, so a brand-new pod whose Redis
key is empty (cache eviction or first-deploy of this code) still
serves chip names from the last good DB snapshot.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwCompanyInfo(Base):
    __tablename__ = "tw_company_info"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    exchange: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="TWSE",
    )
    industry: Mapped[str | None] = mapped_column(Text, nullable=True)
    name_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
