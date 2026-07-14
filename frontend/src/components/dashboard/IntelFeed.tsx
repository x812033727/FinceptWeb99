import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { ReactNode } from "react";
import api from "@/lib/api";

// Pure-move out of DashboardPage (W-final G8): the right-column intel feed
// (news / announcements / overseas indicators) is self-contained (only api +
// react-query), so extracting it drops the page under the 500-line threshold.
// Behaviour identical.

interface NewsItem {
  title: string;
  publisher: string;
  link: string;
  published_at: string;
  // Sentiment fields are populated only by the TW news endpoint
  // (`/tw/news/recent`). US news is read-through; sentiment scoring
  // only runs on rows that the TW ingest task wrote into the DB.
  sentiment_score?: number | null;
  sentiment_label?: "bullish" | "bearish" | "neutral" | null;
}

// Colors only — labels come from i18n (`dashboard.sentiment.*`). All
// three tones are semantic tokens so the badge tracks both the light
// theme and the [data-market-colors] convention flip.
const SENTIMENT_BADGE: Record<
  NonNullable<NewsItem["sentiment_label"]>,
  { labelKey: string; cls: string }
> = {
  bullish: { labelKey: "dashboard.sentiment.bullish", cls: "bg-up/10 text-up border-up/30" },
  bearish: { labelKey: "dashboard.sentiment.bearish", cls: "bg-down/10 text-down border-down/30" },
  neutral: { labelKey: "dashboard.sentiment.neutral", cls: "bg-flat/10 text-flat border-flat/30" },
};

function NewsList({ items }: { items: NewsItem[] }) {
  const { t, i18n } = useTranslation();
  return (
    <div className="divide-y divide-border/50">
      {items.slice(0, 5).map((item, i) => {
        const badge = item.sentiment_label
          ? SENTIMENT_BADGE[item.sentiment_label]
          : null;
        return (
          <a
            key={i}
            href={item.link}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-start gap-3 px-4 py-3 hover:bg-accent/5 transition-colors"
          >
            <div className="flex-1 min-w-0">
              <p className="text-sm text-foreground leading-snug line-clamp-2">{item.title}</p>
              <div className="mt-1 flex items-center gap-1.5 flex-wrap text-xs text-muted-foreground">
                <span>{item.publisher}</span>
                <span>·</span>
                <span>
                  {new Date(item.published_at).toLocaleDateString(i18n.language, {
                    month: "short", day: "numeric",
                  })}
                </span>
                {badge && (
                  <span
                    className={`px-1.5 py-0.5 rounded border text-micro ${badge.cls}`}
                  >
                    {t(badge.labelKey)}
                  </span>
                )}
              </div>
            </div>
          </a>
        );
      })}
    </div>
  );
}

function RecentNewsFeed({ market }: { market: "TW" | "GLOBAL" }) {
  const { t } = useTranslation();
  const { data: items = [], isLoading } = useQuery<NewsItem[]>({
    queryKey: ["news", market, "recent"],
    // 5-min stale matches the ingest cadence (hourly) — UI doesn't
    // need to refetch faster than the data can change.
    queryFn: () =>
      api
        .get(market === "TW" ? "/tw/news/recent?limit=20" : "/global/news/recent?limit=20")
        .then((r) => r.data),
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000, // news tier
  });

  if (isLoading) {
    return (
      <div className="px-4 py-3 text-xs text-muted-foreground animate-pulse">
        {t("dashboard.loading_news")}
      </div>
    );
  }

  return !items.length ? (
    <div className="px-4 py-6 text-xs text-muted-foreground text-center">
      {t("dashboard.no_news")}
    </div>
  ) : (
    <NewsList items={items} />
  );
}

interface OverseasIndex {
  symbol: string;
  name: string;
  close: number;
  prev_close: number;
  change_pct: number;
}

interface OverseasResponse {
  as_of: string | null;
  indices: OverseasIndex[];
}

/**
 * Overnight US/global index snapshot (PR #270). Same data the
 * discussion context block consumes (PR #269) — exposed on the
 * dashboard so operators can see SOX / NDX / SPX / DJI / VIX
 * direction at a glance without opening a discussion. Renders
 * compact one-line rows with coloured % change so the typical
 * "did SOX gap down?" check takes a single glance.
 */
function OverseasIndicators() {
  const { t } = useTranslation();
  const { data, isLoading } = useQuery<OverseasResponse>({
    queryKey: ["overseas-indicators"],
    queryFn: () => api.get("/global/overseas-indicators").then((r) => r.data),
    // Same TTL as the backend cache so the UI doesn't poll faster
    // than the data refreshes upstream.
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000, // news tier
  });

  if (isLoading) {
    return (
      <div className="px-4 py-3 text-xs text-muted-foreground animate-pulse">
        {t("dashboard.loading_overseas") ?? "Loading..."}
      </div>
    );
  }

  const rows = data?.indices ?? [];
  if (rows.length === 0) {
    return (
      <div className="px-4 py-6 text-xs text-muted-foreground text-center">
        {t("dashboard.overseas_indicators_empty")}
      </div>
    );
  }
  return (
    <ul className="divide-y divide-border/50">
      {rows.map((idx) => {
        const sign = idx.change_pct >= 0 ? "+" : "";
        const cls = idx.change_pct >= 0 ? "text-up" : "text-down";
        return (
          <li
            key={idx.symbol}
            className="px-4 py-2 grid grid-cols-[minmax(0,1fr)_auto_auto] items-baseline gap-3 text-sm"
          >
            <span className="text-foreground truncate" title={idx.symbol}>
              {idx.name}
            </span>
            <span className="font-mono tabular-nums text-foreground">
              {idx.close.toLocaleString(undefined, {
                maximumFractionDigits: 2,
              })}
            </span>
            <span
              className={`font-mono tabular-nums text-right w-20 ${cls}`}
            >
              {sign}
              {idx.change_pct.toFixed(2)}%
            </span>
          </li>
        );
      })}
    </ul>
  );
}

// ── Corporate announcements (PR-D4) ───────────────────────────────

interface AnnouncementItem {
  symbol: string;
  announced_at: string;
  category: string;
  title: string;
  body: string | null;
  source_url: string | null;
  sentiment_score?: number | null;
  sentiment_label?: "bullish" | "bearish" | "neutral" | null;
}

function AnnouncementsList({ items }: { items: AnnouncementItem[] }) {
  const { t, i18n } = useTranslation();
  return (
    <div className="divide-y divide-border/50">
      {items.slice(0, 5).map((item, i) => {
        const badge = item.sentiment_label
          ? SENTIMENT_BADGE[item.sentiment_label]
          : null;
        const inner = (
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap mb-0.5 text-xs">
              <span className="font-mono tabular-nums text-primary">
                {item.symbol}
              </span>
              <span className="px-1.5 py-0.5 rounded bg-muted/30 text-muted-foreground border border-border text-micro">
                {item.category}
              </span>
            </div>
            <p className="text-sm text-foreground leading-snug line-clamp-2">
              {item.title}
            </p>
            <div className="mt-1 flex items-center gap-1.5 flex-wrap text-xs text-muted-foreground">
              <span>
                {new Date(item.announced_at).toLocaleDateString(i18n.language, {
                  month: "short", day: "numeric",
                })}
              </span>
              {badge && (
                <span
                  className={`px-1.5 py-0.5 rounded border text-micro ${badge.cls}`}
                >
                  {t(badge.labelKey)}
                </span>
              )}
            </div>
          </div>
        );
        // External link only when source_url populated. SEC EDGAR
        // always provides one; MOPS sometimes doesn't (the connector
        // tolerates None). Wrap-as-anchor only when there's somewhere
        // to go; otherwise render a non-interactive row so a stray
        // click doesn't open about:blank.
        return item.source_url ? (
          <a
            key={i}
            href={item.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-start gap-3 px-4 py-3 hover:bg-accent/5 transition-colors"
          >
            {inner}
          </a>
        ) : (
          <div
            key={i}
            className="flex items-start gap-3 px-4 py-3"
          >
            {inner}
          </div>
        );
      })}
    </div>
  );
}

function RecentAnnouncements({ market }: { market: "TW" | "US" }) {
  const { t } = useTranslation();
  const { data: items = [], isLoading } = useQuery<AnnouncementItem[]>({
    queryKey: ["announcements", market, "recent"],
    // 5-min stale matches the ingest cadence (TW 30 min / US hourly).
    queryFn: () =>
      api
        .get(`/announcements/recent?market=${market}&limit=20`)
        .then((r) => r.data),
    staleTime: 5 * 60_000,
    gcTime: 30 * 60_000, // news tier
  });

  if (isLoading) {
    return (
      <div className="px-4 py-3 text-xs text-muted-foreground animate-pulse">
        {t("dashboard.loading_announcements")}
      </div>
    );
  }

  return !items.length ? (
    <div className="px-4 py-6 text-xs text-muted-foreground text-center">
      {t("dashboard.no_announcements")}
    </div>
  ) : (
    <AnnouncementsList items={items} />
  );
}

// ── Intel feed (right column) ──────────────────────────────────────

/**
 * One sub-group inside the unified intel-feed card. Replaces the five
 * separate section-header + card-header pairs the right column used to
 * stack — a single slim uppercase strip per source keeps the terminal
 * density while making the whole column read as one feed.
 */
function FeedSection({
  titleKey,
  children,
}: {
  titleKey: string;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <section>
      <h3 className="px-4 py-2 text-label uppercase text-muted-foreground font-medium bg-surface-2 border-b border-border-subtle">
        {t(titleKey)}
      </h3>
      {children}
    </section>
  );
}

export function IntelFeed() {
  const { t } = useTranslation();
  return (
    <div>
      <h2 className="text-label text-muted-foreground uppercase mb-2">
        {t("dashboard.intel_feed")}
      </h2>
      <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden divide-y divide-border">
        <FeedSection titleKey="dashboard.overseas_indicators_title">
          <OverseasIndicators />
        </FeedSection>
        <FeedSection titleKey="dashboard.tw_market_news">
          <RecentNewsFeed market="TW" />
        </FeedSection>
        <FeedSection titleKey="dashboard.global_market_news">
          <RecentNewsFeed market="GLOBAL" />
        </FeedSection>
        <FeedSection titleKey="dashboard.tw_announcements">
          <RecentAnnouncements market="TW" />
        </FeedSection>
        <FeedSection titleKey="dashboard.us_announcements">
          <RecentAnnouncements market="US" />
        </FeedSection>
      </div>
    </div>
  );
}
