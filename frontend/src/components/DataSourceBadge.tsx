import { useTranslation } from "react-i18next";

/**
 * Small chip rendered next to a price / symbol when the row was served
 * by a downgraded path. Steady-state sources (polygon / yfinance / twse
 * / kraken) render no chip — surfacing them would just be noise.
 *
 * Surfaces only:
 *   stooq        → US Stooq fallback (Yahoo blocked)
 *   finmind      → TW EOD fallback (TWSE realtime down)
 *   unavailable  → all upstreams blocked, value is a placeholder
 */
export function DataSourceBadge({ source }: { source: string | undefined | null }) {
  const { t } = useTranslation();

  if (source === "finmind") {
    return (
      <span
        title={t("market.source_badge.finmind_tooltip")}
        className="ml-1.5 text-micro uppercase font-medium px-1 py-px rounded bg-warning/10 text-warning border border-warning/30"
      >
        {t("market.source_badge.finmind_label")}
      </span>
    );
  }
  if (source === "stooq") {
    return (
      <span
        title={t("market.source_badge.stooq_tooltip")}
        className="ml-1.5 text-micro uppercase font-medium px-1 py-px rounded bg-warning/10 text-warning border border-warning/30"
      >
        {t("market.source_badge.stooq_label")}
      </span>
    );
  }
  if (source === "unavailable") {
    return (
      <span
        title={t("market.source_badge.unavailable_tooltip")}
        className="ml-1.5 text-micro uppercase font-medium px-1 py-px rounded bg-danger/10 text-danger border border-danger/30"
      >
        {t("market.source_badge.unavailable_label")}
      </span>
    );
  }
  return null;
}
