"""Create the strategy-template rows for the daily roundtable strategies.

The health / maturity / calibration stack keys everything on a
`discussion_strategy_templates` row. The daily roundtable never had
one -- it keys off the three strings in
`discussion_auto_run_configs.strategy_run_counts` -- so
`monitor_strategy_health` walked an empty table every morning and wrote
zero rows while reporting healthy. `strategy_health_metrics` had 0 rows
for the life of the deployment.

This seeds one row per strategy, tagged with `auto_run_strategy`, owned
by whoever owns the enabled auto-run config. `strategy_health_service.
_discussion_scope` reads that tag and samples the auto-run discussions
directly instead of going through backtest sweeps.

Idempotent: re-running matches on `(owner_id, auto_run_strategy)` and
updates the descriptive fields rather than inserting a duplicate, so it
is safe to run after every deploy.

    python -m scripts.seed_daily_strategy_templates
    python -m scripts.seed_daily_strategy_templates --dry-run
"""
import argparse
import asyncio
import logging
import sys

from sqlalchemy import select

from db.session import AsyncSessionLocal
from models.discussion_auto_run_config import DiscussionAutoRunConfig
from models.discussion_strategy_template import DiscussionStrategyTemplate

log = logging.getLogger(__name__)

# Display names mirror `services.daily_stock_strategies.LABELS`; kept
# as a literal here so seeding a template can't fail on an import
# cycle through the strategies module.
_STRATEGIES: dict[str, str] = {
    "general": "每日綜合",
    "chip_quality": "籌碼＋基本面",
    "price_signal": "量價訊號",
}


async def seed(*, dry_run: bool = False) -> int:
    """Returns the number of rows created or updated."""
    async with AsyncSessionLocal() as db:
        cfg = await db.scalar(
            select(DiscussionAutoRunConfig)
            .where(DiscussionAutoRunConfig.enabled.is_(True))
            .limit(1)
        )
        if cfg is None:
            # Not an error worth raising -- a deployment with auto-run
            # switched off legitimately has nothing to seed -- but it
            # must not read as "seeded successfully" either.
            log.warning("seed_daily_strategy_templates.no_enabled_config")
            return 0

        touched = 0
        for key, label in _STRATEGIES.items():
            existing = await db.scalar(
                select(DiscussionStrategyTemplate).where(
                    DiscussionStrategyTemplate.owner_id == cfg.user_id,
                    DiscussionStrategyTemplate.auto_run_strategy == key,
                )
            )
            if existing is not None:
                existing.name = label
                existing.market = cfg.market
                existing.topic = cfg.topic
                existing.rules = cfg.rules
                existing.persona_ids = list(cfg.persona_ids or [])
                touched += 1
                log.info(
                    "seed_daily_strategy_templates.updated",
                    extra={"strategy": key, "id": str(existing.id)},
                )
                continue

            row = DiscussionStrategyTemplate(
                owner_id=cfg.user_id,
                auto_run_strategy=key,
                name=label,
                description=(
                    "每日圓桌自動執行策略，健康度指標取樣自 auto_run 討論。"
                ),
                topic=cfg.topic,
                rules=cfg.rules,
                market=cfg.market,
                persona_ids=list(cfg.persona_ids or []),
            )
            db.add(row)
            touched += 1
            log.info(
                "seed_daily_strategy_templates.created",
                extra={"strategy": key},
            )

        if dry_run:
            await db.rollback()
        else:
            await db.commit()
        return touched


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    touched = asyncio.run(seed(dry_run=args.dry_run))
    if not touched:
        print(
            "[!] no enabled auto-run config found — nothing seeded",
            file=sys.stderr,
        )
        return 1
    print(
        f"\n[ok] {touched} daily strategy template(s) "
        f"{'would be ' if args.dry_run else ''}seeded."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
