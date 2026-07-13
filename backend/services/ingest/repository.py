"""Central repository for the scheduled-fetch subsystem.

Phase 1 covers OHLCV reads/writes plus a Redis-backed health snapshot
the admin UI can poll. Schedulers write here; `tw_market_service` reads
here as the new tier between Redis and the upstream waterfall.

This module was split (R3-A1/G8) into the ``services.ingest.repo`` package
by domain. It is retained as a **facade** that re-exports every public
symbol so the ~46 existing importers keep working unchanged.
"""
import logging

from services.ingest.repo.infra import *  # noqa: F401,F403

# A handful of tests / callers import these private helpers directly by name.
# Star-import skips underscore names, so re-export them explicitly to keep the
# pre-split import surface intact.
from services.ingest.repo.infra import (  # noqa: F401
    _backoff_seconds_for,
    _classify_outcome,
)
from services.ingest.repo.news import *  # noqa: F401,F403
from services.ingest.repo.ohlcv import *  # noqa: F401,F403
from services.ingest.repo.quotes import *  # noqa: F401,F403
from services.ingest.repo.tw_chip import *  # noqa: F401,F403
from services.ingest.repo.tw_fundamentals import *  # noqa: F401,F403
from services.ingest.repo.tw_fundamentals import _is_bogus_growth_pair  # noqa: F401

# Keep the facade's own logger name identical to the pre-split module so any
# importer relying on `repository.log` sees the same channel it always did.
log = logging.getLogger(__name__)
