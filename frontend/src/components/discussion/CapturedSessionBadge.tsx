import { useTranslation } from "react-i18next";
import type { CapturedSession } from "@/types/discussion";

/**
 * Inline badge that surfaces the trading session a discussion's
 * numeric blocks are anchored to. Mirrors the `captured_session`
 * block injected into the ctx + carried onto every conclusion at
 * synthesis time (PR for expert-quote freshness). Distinct from
 * `captured_at` (wall-clock NOW) — surfaces the actual session the
 * prices / technicals / chip rows describe so the user can never
 * confuse "the conclusion was written 04:00 Taipei Wed" with "the
 * conclusion talks about Wed's market" (it's actually anchored to
 * Tuesday's close in that scenario).
 *
 * Pure presentation: renders nothing when no session metadata is
 * present (old conclusions written before the freshness phase
 * shipped), so the card layout doesn't shift for archive rows.
 *
 * Phase colour-codes the badge:
 *   - `intraday` / `between_close_and_publish` — amber (warning the
 *     reader the session is open / the data is mid-publication).
 *   - `today_close_published` / `weekend_or_holiday` — neutral
 *     border (the session is fully settled).
 *   - `backtest` — purple matching the BacktestSweep family colour.
 *   - `pre_open_today` — sky-blue (data is "yesterday's close, used
 *     to brief today's open").
 */
export function CapturedSessionBadge({
  session,
}: {
  session?: import("@/types/discussion").CapturedSession;
}) {
  const { t } = useTranslation();
  if (!session) return null;
  const { session_date, decision_date, phase, is_intraday, hint_zh } = session;
  const tone = pickTone(phase);
  // Show phase label translated when we have one; otherwise fall
  // back to the raw key so a new backend phase still renders
  // sensibly without a coordinated translation bump.
  const phaseLabel = t(
    `discussion.captured_session_phase.${phase}`,
    { defaultValue: phase },
  );

  return (
    <div
      className={`mb-3 rounded border ${tone} px-3 py-2 text-[11px] leading-snug`}
      role="note"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 font-medium">
        <span>📅 {t("discussion.captured_session_label")}</span>
        {session_date && <span className="font-mono">{session_date}</span>}
        {decision_date && decision_date !== session_date && (
          <span className="font-mono opacity-90">
            → {t("discussion.captured_session_entry")} {decision_date}
          </span>
        )}
        <span className="text-[10px] uppercase opacity-80">{phaseLabel}</span>
        {is_intraday && (
          <span className="text-[10px] uppercase opacity-80">
            · {t("discussion.captured_session_intraday")}
          </span>
        )}
      </div>
      {hint_zh && (
        <p className="mt-1 text-[10.5px] opacity-90">{hint_zh}</p>
      )}
    </div>
  );
}

function pickTone(phase: string): string {
  switch (phase) {
    case "backtest":
      return "border-purple-800/50 bg-purple-950/30 text-purple-200";
    case "intraday":
    case "between_close_and_publish":
      return "border-amber-700/60 bg-amber-950/30 text-amber-200";
    case "pre_open_today":
      return "border-sky-800/50 bg-sky-950/30 text-sky-200";
    case "today_close_published":
    case "weekend_or_holiday":
      return "border-border bg-card/40 text-muted-foreground";
    case "crypto_24_7":
      return "border-emerald-800/50 bg-emerald-950/30 text-emerald-200";
    default:
      return "border-border bg-card/40 text-muted-foreground";
  }
}

/** Lighter inline variant for the per-round context card — fits in
 * a single row and skips the long hint so the row stays compact.
 * Use the full badge above on hero / conclusion surfaces where the
 * extra explanation is worth the vertical space.
 */
export function CapturedSessionInline({
  session,
}: {
  session?: CapturedSession;
}) {
  const { t } = useTranslation();
  if (!session) return null;
  const { session_date, decision_date, phase, is_intraday } = session;
  const tone = pickTone(phase);
  const phaseLabel = t(
    `discussion.captured_session_phase.${phase}`,
    { defaultValue: phase },
  );
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border ${tone} px-1.5 py-0.5 text-[10px] leading-tight`}
    >
      <span>📅</span>
      {session_date && <span className="font-mono">{session_date}</span>}
      {decision_date && decision_date !== session_date && (
        <span className="font-mono opacity-90">
          → {t("discussion.captured_session_entry")} {decision_date}
        </span>
      )}
      <span className="uppercase opacity-80">{phaseLabel}</span>
      {is_intraday && (
        <span className="uppercase opacity-80">
          · {t("discussion.captured_session_intraday")}
        </span>
      )}
    </span>
  );
}
