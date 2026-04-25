#!/usr/bin/env python3
"""
Multi-process pressure test for the TWSE Redis token bucket rate limiter.

Spawns N independent OS processes — each simulating a uvicorn worker /
k8s pod — that all hammer `acquire_token()` against the same Redis
instance. Counts how many tokens were granted in `--duration` seconds
and asserts the observed rate stays at or below the configured limit
plus a small burst tolerance.

Usage
-----
    # Defaults: 4 workers, 20s, expected rate 1/1.1 ≈ 0.91 req/s
    REDIS_URL=redis://localhost:6379 python scripts/pressure_test_twse.py

    # Tune
    python scripts/pressure_test_twse.py \
        --workers 8 --duration 30 --capacity 1 --rate 0.91

Exit code is non-zero if the global rate exceeds the limit, signalling
a regression in the limiter.
"""
from __future__ import annotations

import argparse
import asyncio
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

# Make `cache.redis_cache` importable when running the script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


def _worker(worker_id: int, duration: float, key: str, capacity: float, rate: float, redis_url: str) -> tuple[int, int, int]:
    """Body of one OS process. Returns (worker_id, granted, attempted)."""
    os.environ["REDIS_URL"] = redis_url
    # Pydantic Settings validates JWT_SECRET_KEY at import time even when we
    # only need the cache module.
    os.environ.setdefault("JWT_SECRET_KEY", "pressure-test-secret-key-32chars!!")

    async def _run() -> tuple[int, int]:
        from cache.redis_cache import RedisUnavailable, acquire_token

        granted = 0
        attempted = 0
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            attempted += 1
            try:
                if await acquire_token(key, capacity, rate):
                    granted += 1
            except RedisUnavailable as exc:
                print(f"[worker {worker_id}] Redis unavailable: {exc}", file=sys.stderr)
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(0.05)
        return granted, attempted

    granted, attempted = asyncio.run(_run())
    return worker_id, granted, attempted


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workers", type=int, default=4, help="Number of OS processes")
    p.add_argument("--duration", type=float, default=20.0, help="Seconds to run")
    p.add_argument("--capacity", type=float, default=1.0, help="Bucket capacity (tokens)")
    p.add_argument("--rate", type=float, default=1.0 / 1.1, help="Refill rate tokens/sec")
    p.add_argument("--key", default="pressure-test:twse", help="Redis bucket key")
    p.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379"))
    p.add_argument("--burst-tolerance", type=float, default=2.0,
                   help="Allowed extra tokens above rate*duration (burst + clock drift)")
    args = p.parse_args()

    print("=" * 64)
    print(f"  TWSE token bucket pressure test")
    print(f"  workers={args.workers}  duration={args.duration}s  rate={args.rate:.3f}/s")
    print(f"  redis={args.redis_url}  key={args.key}")
    print("=" * 64)

    # Reset bucket state so the test starts clean.
    import redis
    sync_redis = redis.from_url(args.redis_url)
    sync_redis.delete(args.key)

    started = time.monotonic()
    with mp.get_context("spawn").Pool(args.workers) as pool:
        results = pool.starmap(_worker, [
            (i, args.duration, args.key, args.capacity, args.rate, args.redis_url)
            for i in range(args.workers)
        ])
    elapsed = time.monotonic() - started

    total_granted = sum(g for _, g, _ in results)
    total_attempted = sum(a for _, _, a in results)
    expected = args.rate * args.duration + args.capacity + args.burst_tolerance
    observed_rate = total_granted / args.duration

    print()
    print(f"{'worker':>8} | {'granted':>8} | {'attempted':>10}")
    print(f"{'-' * 8:>8}-+-{'-' * 8:>8}-+-{'-' * 10:>10}")
    for wid, granted, attempted in sorted(results):
        print(f"{wid:>8} | {granted:>8} | {attempted:>10}")
    print()
    print(f"  total granted    : {total_granted}")
    print(f"  total attempted  : {total_attempted}")
    print(f"  elapsed          : {elapsed:.2f}s (target {args.duration}s)")
    print(f"  observed rate    : {observed_rate:.3f} req/s")
    print(f"  expected ceiling : {expected / args.duration:.3f} req/s "
          f"({expected:.1f} tokens over {args.duration}s)")

    if total_granted > expected:
        print(f"  RESULT: ❌ FAIL — rate limit breached "
              f"(granted {total_granted} > {expected:.1f})")
        return 1
    print(f"  RESULT: ✅ PASS — global rate respected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
