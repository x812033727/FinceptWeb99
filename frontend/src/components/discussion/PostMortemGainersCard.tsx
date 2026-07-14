import { useTranslation } from "react-i18next";
import { useCollapsible } from "@/hooks/useCollapsible";
import { DataTable, type DataTableColumn } from "../ui/table";
import type { PostMortemResponse } from "./_helpers";

/**
 * Visual leaderboard for the post-mortem ground-truth views
 * (PR #273, evolved from PR #257).
 *
 * Two sections:
 *
 *   A. **Recommended D1-D5 self-eval** — for each recommended
 *      symbol, a row of D1..D5 cumulative-since-as_of % changes.
 *      Lets the operator see at a glance whether the picks
 *      delivered, not just whether they overlapped with random
 *      one-day winners. Cumulative cell coloured green/red.
 *
 *   B. **Daily top-N gainers** — per-day leaderboards across the
 *      same window. Operator scans the columns for symbols that
 *      repeat (high-conviction missed picks). Each day rendered
 *      as a compact column with rank · symbol · % cells.
 *
 * Backend writes the same data into the user_input turn's
 * markdown so the personas see it too — this card is operator
 * situational awareness.
 *
 * Persisted in localStorage by `rememberPostMortemResult`
 * (PR #268) so reload preserves the snapshot.
 */

function PerfCell({ pct }: { pct: number }) {
  const sign = pct >= 0 ? "+" : "";
  const cls =
    pct >= 0 ? "text-up" : "text-down";
  return (
    <span className={`font-mono tabular-nums ${cls}`}>
      {sign}
      {pct.toFixed(2)}%
    </span>
  );
}

export function PostMortemGainersCard({
  data,
}: {
  data: PostMortemResponse | null;
}) {
  const { t } = useTranslation();
  // PR-C: Section B (daily top-N gainers leaderboard) is now
  // collapsed by default — Section A (recommended self-eval) is the
  // headline view operators want to see, and the leaderboard adds
  // visual weight that competes with the transcript above. The
  // collapse-pref persists per-user via useCollapsible.
  const sectionB = useCollapsible(
    "discussion.postMortem.daily_winners",
    false,
  );
  if (!data) return null;

  // Derive the working shape from the new payload, falling back to
  // the back-compat single-day fields if the backend is older
  // (PR #270 and earlier).
  const tradingDays =
    data.trading_days && data.trading_days.length > 0
      ? data.trading_days
      : data.next_trading_day
      ? [data.next_trading_day]
      : [];
  const dailyBlocks =
    data.daily_top_gainers && data.daily_top_gainers.length > 0
      ? data.daily_top_gainers
      : tradingDays.length > 0
      ? [
          {
            trading_day: tradingDays[0],
            gainers: data.top_gainers ?? [],
          },
        ]
      : [];
  const recPerf = data.recommended_performance ?? [];

  if (tradingDays.length === 0 && recPerf.length === 0) return null;

  const lastDay = tradingDays[tradingDays.length - 1] ?? "";

  type RecPerfRow = (typeof recPerf)[number];
  const columns: DataTableColumn<RecPerfRow>[] = [
    {
      key: "symbol",
      header: t("discussion.post_mortem_symbol_col"),
      render: (row) => (
        <span className="font-mono font-bold text-foreground">{row.symbol}</span>
      ),
    },
    {
      key: "base",
      header: t("discussion.post_mortem_base_col"),
      numeric: true,
      render: (row) => (
        <span className="font-mono tabular-nums text-muted-foreground">
          {row.base_close.toLocaleString(undefined, {
            maximumFractionDigits: 2,
          })}
        </span>
      ),
    },
    ...tradingDays.map((d, i) => ({
      key: d,
      header: <span title={d}>D{i + 1}</span>,
      numeric: true,
      render: (row: RecPerfRow) => {
        const dp = row.days.find((x) => x.trading_day === d);
        return dp ? (
          <PerfCell pct={dp.change_pct} />
        ) : (
          <span className="text-muted-foreground/50 text-micro">—</span>
        );
      },
    })),
  ];

  return (
    <div className="bg-purple-950/15 border border-purple-800/40 rounded-lg p-3 mt-3 space-y-4">
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <h4 className="text-xs font-semibold text-purple-300">
          {t("discussion.post_mortem_window_title", {
            first: tradingDays[0] ?? "—",
            last: lastDay,
            count: tradingDays.length,
          })}
        </h4>
      </div>

      {/* Section A — recommended D1-D5 self-eval */}
      <div className="space-y-1.5">
        <h5 className="text-label font-semibold text-purple-200/90 uppercase tracking-wider">
          {t("discussion.post_mortem_self_eval_title")}
        </h5>
        {recPerf.length === 0 ? (
          <p className="text-meta text-muted-foreground">
            {t("discussion.post_mortem_self_eval_empty")}
          </p>
        ) : (
          <DataTable
            columns={columns}
            rows={recPerf}
            rowKey={(row) => row.symbol}
            mobileMode="scroll"
            aria-label={t("discussion.post_mortem_self_eval_title")}
          />
        )}
      </div>

      {/* Section B — daily top-N gainers. Collapsed by default since
          PR-C: Section A above answers the operator's primary
          question ("did the picks land?") and the leaderboard is a
          deeper-dive view that doesn't need to compete for vertical
          space on every load. */}
      <div className="space-y-1.5">
        <button
          type="button"
          onClick={sectionB.toggle}
          aria-expanded={sectionB.open}
          className="w-full flex items-center gap-1.5 text-left hover:opacity-80 transition-opacity"
        >
          <span className="text-micro text-muted-foreground w-2.5 inline-block">
            {sectionB.open ? "▼" : "▶"}
          </span>
          <h5 className="text-label font-semibold text-purple-200/90 uppercase tracking-wider">
            {t("discussion.post_mortem_daily_winners_title")}
          </h5>
          {!sectionB.open && dailyBlocks.length > 0 && (
            <span className="text-micro text-muted-foreground ml-1">
              ({dailyBlocks.length})
            </span>
          )}
        </button>
        {sectionB.open && (
          dailyBlocks.length === 0 ? (
            <p className="text-meta text-muted-foreground">
              {t("discussion.post_mortem_daily_winners_empty")}
            </p>
          ) : (
            <div className="grid gap-3"
              style={{
                gridTemplateColumns: `repeat(${dailyBlocks.length}, minmax(0, 1fr))`,
              }}
            >
              {dailyBlocks.map((block, i) => (
                <div key={block.trading_day} className="space-y-1">
                  <div className="text-micro text-muted-foreground/80">
                    D{i + 1}
                    <span className="font-mono">{block.trading_day}</span>
                  </div>
                  {block.gainers.length === 0 ? (
                    <p className="text-micro text-muted-foreground/60">
                      {t("discussion.gainers_no_data")}
                    </p>
                  ) : (
                    <ol className="space-y-0.5">
                      {block.gainers.map((g, j) => (
                        <li
                          key={g.symbol}
                          className="grid grid-cols-[1.2rem_minmax(0,1fr)_auto] items-center gap-1 text-meta"
                        >
                          <span className="text-muted-foreground/60 tabular-nums">
                            {j + 1}.
                          </span>
                          <span className="font-mono font-bold text-foreground truncate">
                            {g.symbol}
                          </span>
                          <PerfCell pct={g.change_pct} />
                        </li>
                      ))}
                    </ol>
                  )}
                </div>
              ))}
            </div>
          )
        )}
      </div>

      <p className="text-micro text-muted-foreground/70 leading-relaxed">
        {t("discussion.post_mortem_help_v2")}
      </p>
    </div>
  );
}
