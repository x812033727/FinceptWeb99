#!/usr/bin/env python3
"""
End-to-end smoke test for the expert-discussion subsystem.

Walks a deployed backend through the full happy path:
  1. POST /api/auth/login           → get access token
  2. POST /api/discussion/sessions  → create a draft discussion
  3. POST /api/discussion/sessions/{id}/round → consume SSE stream
  4. GET  /api/discussion/sessions/{id}        → verify turns persisted
                                                  + status reset to draft
  5. POST /api/discussion/sessions/{id}/conclude → structured conclusion
  6. DELETE /api/discussion/sessions/{id}        → cleanup

Run after each deploy that touches `services/discussion_service.py`,
`api/discussion/router.py`, or the LLM router. Failure exits non-zero
with a clear message so it can drop into a Kubernetes post-deploy hook
or CI smoke job.

Usage
-----
    BACKEND_URL=https://fincept.example.com \
    SMOKE_EMAIL=admin@example.com \
    SMOKE_PASSWORD=... \
    python scripts/smoke_test_discussion.py

    # Optional knobs
    --personas buffett,lynch       # roster (default: market_analyst,trading_coach)
    --timeout 180                  # HTTP read timeout in seconds
    --keep                         # don't delete the discussion at the end

Notes
-----
- The script uses real LLM calls — make sure the deployment has at least
  one provider key configured. It does NOT mock or stub anything.
- A 5-persona round can take ~60-120 s end-to-end, so HTTP timeout is
  generous by default.
- The `error` SSE events are tolerated (LLM provider hiccups happen);
  the script only fails if NO turn_end events arrive at all.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx


# Default topic mentions a 4-digit TW code so `extract_focus_symbols`
# fires on the smoke run and the per-symbol news sentiment branch of
# `gather_market_context` actually gets exercised. If that branch
# regresses (e.g. a `services.news_sentiment_service` rename), the
# smoke test will fail post-deploy instead of hiding the breakage.
DEFAULT_TOPIC = "Smoke test: 找 2330 短線買點 (default — feel free to override)"
DEFAULT_RULES = "Each expert ≤ 80 chars. Cite at least one number."
DEFAULT_PERSONAS = ["market_analyst", "trading_coach"]


class SmokeError(RuntimeError):
    """Raised when an assertion fails. Caught at top level for clean exit."""


def _login(client: httpx.Client, email: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    if r.status_code != 200:
        raise SmokeError(f"login failed: {r.status_code} {r.text[:200]}")
    token = r.json().get("access_token")
    if not token:
        raise SmokeError(f"login response missing access_token: {r.text[:200]}")
    return token


def _create_discussion(
    client: httpx.Client, headers: dict, topic: str, rules: str, personas: list[str],
    *,
    as_of_date: str | None = None,
) -> dict:
    payload: dict = {"topic": topic, "rules": rules, "persona_ids": personas}
    # PR-3: backtest-mode smoke. Setting as_of_date pins the ctx
    # waterfall to the historical date so the smoke run exercises
    # the same code path the sweep worker does — catches regressions
    # in `gather_market_context`'s as_of clamp + the verifier's
    # window math.
    if as_of_date:
        payload["as_of_date"] = as_of_date
    r = client.post(
        "/api/discussion/sessions",
        headers=headers,
        json=payload,
    )
    if r.status_code != 201:
        raise SmokeError(f"create failed: {r.status_code} {r.text[:200]}")
    body = r.json()
    if body.get("status") != "draft" or body.get("current_round") != 0:
        raise SmokeError(f"new discussion has unexpected initial state: {body}")
    return body


def _stream_round(
    client: httpx.Client, headers: dict, discussion_id: str, expected_turns: int,
) -> dict:
    """Consume the SSE round and return a summary of the events seen."""
    seen: dict[str, int] = {}
    turn_ends: list[dict] = []
    started = time.monotonic()

    with client.stream(
        "POST",
        f"/api/discussion/sessions/{discussion_id}/round",
        headers=headers,
    ) as resp:
        if resp.status_code != 200:
            raise SmokeError(f"round failed: {resp.status_code} {resp.read()[:200]!r}")

        buf = ""
        for chunk in resp.iter_text():
            buf += chunk
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                line = frame.strip()
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    seen["DONE"] = seen.get("DONE", 0) + 1
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                etype = obj.get("type", "?")
                seen[etype] = seen.get(etype, 0) + 1
                if etype == "turn_end":
                    turn_ends.append(obj)

    elapsed = time.monotonic() - started

    if seen.get("DONE", 0) != 1:
        raise SmokeError(f"missing terminal [DONE] frame; events: {seen}")
    if seen.get("round_start", 0) != 1:
        raise SmokeError(f"expected exactly 1 round_start; got {seen}")
    if seen.get("round_end", 0) != 1:
        raise SmokeError(f"expected exactly 1 round_end; got {seen}")
    if not turn_ends:
        raise SmokeError(f"NO turn_end events arrived; events: {seen}")
    if len(turn_ends) != expected_turns:
        raise SmokeError(
            f"expected {expected_turns} turn_end events, saw {len(turn_ends)}: {seen}"
        )
    for te in turn_ends:
        if te.get("stance") not in ("agree", "dissent", "supplement"):
            raise SmokeError(f"turn_end has unexpected stance: {te}")

    return {"events": seen, "turn_ends": turn_ends, "elapsed_s": round(elapsed, 1)}


def _verify_post_round(
    client: httpx.Client, headers: dict, discussion_id: str, expected_turns: int,
) -> None:
    r = client.get(f"/api/discussion/sessions/{discussion_id}", headers=headers)
    if r.status_code != 200:
        raise SmokeError(f"get session after round failed: {r.status_code}")
    body = r.json()
    # Status MUST be back to "draft" — verifies the run_round try/finally
    # path resets state cleanly. RUNNING here = the deadlock fix in PR #91
    # regressed.
    if body.get("status") != "draft":
        raise SmokeError(
            f"discussion stuck in status={body.get('status')!r} after round; "
            "status-reset try/finally regression?"
        )
    if body.get("current_round") != 1:
        raise SmokeError(f"current_round expected 1, got {body.get('current_round')}")
    turns = body.get("turns") or []
    if len(turns) != expected_turns:
        raise SmokeError(f"expected {expected_turns} turns persisted, got {len(turns)}")


def _conclude(client: httpx.Client, headers: dict, discussion_id: str) -> dict:
    r = client.post(
        f"/api/discussion/sessions/{discussion_id}/conclude", headers=headers,
    )
    if r.status_code != 200:
        raise SmokeError(f"conclude failed: {r.status_code} {r.text[:200]}")
    body = r.json()
    conclusion = body.get("conclusion") or {}
    required = {"recommended_symbols", "reasoning", "risks", "time_horizon", "consensus_score"}
    missing = required - set(conclusion.keys())
    if missing:
        raise SmokeError(f"conclusion missing fields: {missing}")
    return conclusion


def _delete(client: httpx.Client, headers: dict, discussion_id: str) -> None:
    r = client.delete(
        f"/api/discussion/sessions/{discussion_id}", headers=headers,
    )
    if r.status_code != 204:
        raise SmokeError(f"delete failed: {r.status_code}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--backend-url",
        default=os.environ.get("BACKEND_URL", "http://localhost:8000"),
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("SMOKE_EMAIL", os.environ.get("ADMIN_EMAIL", "")),
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("SMOKE_PASSWORD", os.environ.get("ADMIN_PASSWORD", "")),
    )
    parser.add_argument(
        "--personas",
        default=",".join(DEFAULT_PERSONAS),
        help="comma-separated persona IDs (≥ 2)",
    )
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--rules", default=DEFAULT_RULES)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--keep", action="store_true",
                        help="don't delete the discussion at the end")
    # PR-3: backtest-mode smoke. Pins the ctx waterfall to a
    # historical date and asserts that the conclusion picks up
    # `quality_signals` (PR-1) + (when the strategy has a
    # calibration curve) `calibrated_confidence`.
    parser.add_argument(
        "--mode", choices=["live", "backtest"], default="live",
        help="live = today; backtest = pin ctx to --anchor-date",
    )
    parser.add_argument(
        "--anchor-date", default=None,
        help="YYYY-MM-DD; required when --mode=backtest",
    )
    args = parser.parse_args()

    if not args.email or not args.password:
        print("ERROR: set SMOKE_EMAIL + SMOKE_PASSWORD (or ADMIN_EMAIL/ADMIN_PASSWORD)",
              file=sys.stderr)
        return 2

    personas = [p.strip() for p in args.personas.split(",") if p.strip()]
    if len(personas) < 2:
        print("ERROR: need at least 2 personas", file=sys.stderr)
        return 2
    if args.mode == "backtest" and not args.anchor_date:
        print("ERROR: --mode=backtest requires --anchor-date YYYY-MM-DD",
              file=sys.stderr)
        return 2

    print(f"→ smoke-testing {args.backend_url}")
    print(f"  personas: {personas}")
    print(f"  mode: {args.mode}"
          + (f" (anchor={args.anchor_date})" if args.mode == "backtest" else ""))

    timeout = httpx.Timeout(args.timeout, connect=10.0)
    discussion_id: str | None = None
    token: str | None = None
    try:
        with httpx.Client(base_url=args.backend_url, timeout=timeout) as client:
            print("[1/5] login…", end=" ", flush=True)
            token = _login(client, args.email, args.password)
            headers = {"Authorization": f"Bearer {token}"}
            print("OK")

            print("[2/5] create discussion…", end=" ", flush=True)
            row = _create_discussion(
                client, headers, args.topic, args.rules, personas,
                as_of_date=args.anchor_date if args.mode == "backtest" else None,
            )
            discussion_id = row["id"]
            print(f"OK (id={discussion_id[:8]}…)")

            print(f"[3/5] streaming round (≤{int(args.timeout)}s)…", end=" ", flush=True)
            summary = _stream_round(client, headers, discussion_id, len(personas))
            print(f"OK ({summary['elapsed_s']}s, events={summary['events']})")

            print("[4/5] verify post-round state…", end=" ", flush=True)
            _verify_post_round(client, headers, discussion_id, len(personas))
            print("OK (status=draft, turns persisted)")

            print("[5/5] synthesize conclusion…", end=" ", flush=True)
            conclusion = _conclude(client, headers, discussion_id)
            recommended = conclusion.get("recommended_symbols") or []
            print(f"OK (recommended={recommended}, consensus={conclusion.get('consensus_score')})")

            # PR-1: every fresh conclusion should carry quality_signals
            # (or a `_skipped` marker on parse error). Missing the
            # block entirely means the synth post-parse hook
            # regressed silently.
            qsig = conclusion.get("quality_signals")
            if qsig is None and not conclusion.get("_parse_error"):
                raise SmokeError(
                    "conclusion missing `quality_signals` block — "
                    "PR-1 hook may have regressed"
                )
            if qsig and not qsig.get("_skipped"):
                halls = qsig.get("hallucination_warnings", [])
                ocf = qsig.get("confidence_stats", {}).get("over_confident")
                contradiction = qsig.get("consensus_contradiction")
                print(f"    quality_signals: hallucinations={len(halls)}, "
                      f"over_confident={ocf}, contradiction={contradiction}")

            # PR-3: if any recommendation already carries
            # `calibrated_confidence`, the strategy has a deployed
            # calibration curve — surface it for visibility.
            recs = conclusion.get("recommendations") or []
            calibrated = [
                r for r in recs
                if isinstance(r, dict) and r.get("calibrated_confidence") is not None
            ]
            if calibrated:
                print(f"    calibration: {len(calibrated)}/{len(recs)} "
                      f"recommendation(s) carry calibrated_confidence")

            if not args.keep:
                _delete(client, headers, discussion_id)
                print("    cleaned up.")
            else:
                print(f"    kept discussion {discussion_id} (--keep).")

    except SmokeError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        if discussion_id and token and not args.keep:
            try:
                with httpx.Client(base_url=args.backend_url, timeout=10.0) as cleanup:
                    cleanup.delete(
                        f"/api/discussion/sessions/{discussion_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except Exception:
                pass
        return 1
    except httpx.HTTPError as exc:
        print(f"\nFAIL (HTTP): {exc}", file=sys.stderr)
        return 1

    print("\nPASS — all 5 stages green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
