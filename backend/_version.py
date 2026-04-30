"""Single source of truth for the Fincept Web app version.

scripts/sync-version.py mirrors this into frontend/package.json. CI verifies
the two are in sync. Bump here, then run:

    python scripts/sync-version.py
"""

__version__ = "0.4.3"
