"""Aggregator — re-exports every ORM model so a single
`import finmind.models` registers them all on `Base.metadata`.

When you add a new model file under this package, append the import
here and EVERY downstream consumer (alembic env, test conftest,
dataset_catalog seed, runtime queries) sees it. No more
"forgot to add to env.py" footguns.
"""
from finmind.models import backfill_progress  # noqa: F401
from finmind.models import dataset_source  # noqa: F401
from finmind.models import master  # noqa: F401
from finmind.models import technical  # noqa: F401
from finmind.models import chip  # noqa: F401
from finmind.models import fundamental  # noqa: F401
from finmind.models import derivative  # noqa: F401
from finmind.models import corporate  # noqa: F401
from finmind.models import intraday  # noqa: F401
from finmind.models import crypto  # noqa: F401
from finmind.models import billing  # noqa: F401
