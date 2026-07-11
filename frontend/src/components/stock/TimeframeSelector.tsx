/**
 * A2 多週期切換 — timeframe control for the StockDetailPage chart card.
 *
 * Two groups on one row:
 *   分時  1m / 5m / 15m — served by the /intraday endpoint (snapshot
 *         aggregation, ~30-day coverage). Disabled with an explanatory
 *         tooltip when the symbol has no snapshot data.
 *   週期  日 / 週 / 月 — 日 is the raw daily history; 週/月 are aggregated
 *         client-side from the same daily bars (see lib/aggregateBars).
 */
import { useTranslation } from "react-i18next";
import { INTRADAY_TIMEFRAMES, type Timeframe } from "./_shared";

interface Props {
  value: Timeframe;
  onChange: (tf: Timeframe) => void;
  /** Whether the /intraday endpoint has bars for this symbol. False →
   *  the 1m/5m/15m buttons render disabled with the coverage tooltip. */
  intradayAvailable: boolean;
  /** Snapshot retention window used in the tooltip copy (default 30). */
  coverageDays?: number;
}

function TfButton({
  active, disabled, label, title, onClick,
}: {
  active: boolean;
  disabled?: boolean;
  label: string;
  title?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-pressed={active}
      className={`px-3 py-1.5 sm:px-2.5 sm:py-1 text-xs rounded transition-colors touch-manipulation min-h-[36px] sm:min-h-0 ${
        disabled
          ? "text-muted-foreground/40 cursor-not-allowed"
          : active
            ? "bg-primary text-primary-foreground"
            : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
}

export default function TimeframeSelector({
  value, onChange, intradayAvailable, coverageDays = 30,
}: Props) {
  const { t } = useTranslation();
  const dailyLabels: Record<"1d" | "1wk" | "1mo", string> = {
    "1d": t("stock.timeframe.day"),
    "1wk": t("stock.timeframe.week"),
    "1mo": t("stock.timeframe.month"),
  };
  return (
    <div className="flex items-center gap-1" role="group" aria-label={t("stock.timeframe.label")}>
      {INTRADAY_TIMEFRAMES.map((tf) => (
        <TfButton
          key={tf}
          active={value === tf}
          disabled={!intradayAvailable}
          label={tf}
          title={
            intradayAvailable
              ? t("stock.timeframe.intraday_limit", { days: coverageDays })
              : t("stock.timeframe.intraday_unavailable", { days: coverageDays })
          }
          onClick={() => onChange(tf)}
        />
      ))}
      <span className="w-px h-4 bg-border mx-1" aria-hidden />
      {(Object.keys(dailyLabels) as ("1d" | "1wk" | "1mo")[]).map((tf) => (
        <TfButton
          key={tf}
          active={value === tf}
          label={dailyLabels[tf]}
          onClick={() => onChange(tf)}
        />
      ))}
    </div>
  );
}
