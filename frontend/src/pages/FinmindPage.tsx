import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import api, { errorDetail } from "@/lib/api";
import { DataTable, type DataTableColumn } from "@/components/ui/table";

/**
 * Public-facing landing page for the FinMind-clone subsystem
 * (`backend/finmind/`). First stage: read-only catalog browse +
 * static pricing tiers. Stripe Checkout / customer portal arrive in
 * a follow-up once the Stripe account is provisioned.
 *
 * Backed by `GET /api/finmind/catalog` which is intentionally
 * auth-free so prospective customers can browse before signing up.
 * The richer `/api/finmind/datasets` endpoint stays auth-gated for
 * existing key holders who need operational fields like last_ingest_at.
 */

interface CatalogItem {
  dataset_code: string;
  category: string;
  description_zh: string;
  ingest_freq: string;
  per_symbol: boolean;
  sponsor_tier: boolean;
  available: boolean;
}

interface PricingTier {
  code: "free" | "pro" | "enterprise";
  name: string;
  featureKeys: string[];
  highlighted?: boolean;
}

// Static, code-only tier definitions. All display strings (price, note,
// quotas, features, CTA) live in the i18n locale files under
// `finmind.pricing.<code>.*` so the page renders in the selected language.
const PRICING_TIERS: PricingTier[] = [
  {
    code: "free",
    name: "Free",
    featureKeys: ["f1", "f2", "f3", "f4"],
  },
  {
    code: "pro",
    name: "Pro",
    featureKeys: ["f1", "f2", "f3", "f4", "f5"],
    highlighted: true,
  },
  {
    code: "enterprise",
    name: "Enterprise",
    featureKeys: ["f1", "f2", "f3", "f4", "f5"],
  },
];

export default function FinmindPage() {
  const { t } = useTranslation();
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  const [showOnlyAvailable, setShowOnlyAvailable] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");

  const catalogQuery = useQuery<CatalogItem[]>({
    queryKey: ["finmind", "catalog"],
    queryFn: async () => {
      const r = await api.get<CatalogItem[]>("/finmind/catalog");
      return r.data;
    },
    staleTime: 5 * 60 * 1000,
  });

  const datasets = useMemo(
    () => catalogQuery.data ?? [],
    [catalogQuery.data],
  );
  const categories = useMemo(
    () => Array.from(new Set(datasets.map((d) => d.category))).sort(),
    [datasets],
  );

  const filtered = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return datasets.filter((d) => {
      if (categoryFilter !== "all" && d.category !== categoryFilter) return false;
      if (showOnlyAvailable && !d.available) return false;
      if (q) {
        const hay = `${d.dataset_code} ${d.description_zh}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [datasets, categoryFilter, showOnlyAvailable, searchQuery]);

  const availableCount = datasets.filter((d) => d.available).length;

  const columns: DataTableColumn<CatalogItem>[] = [
    {
      key: "dataset_code",
      header: t("finmind.catalog.col_dataset"),
      cellClassName: "font-mono",
      render: (d) => (
        <span className="font-mono">
          {d.dataset_code}
          {d.sponsor_tier && (
            <span
              className="ml-1 rounded bg-warning/15 px-1 text-[10px] text-warning"
              title={t("finmind.catalog.sponsor_title")}
            >
              sponsor
            </span>
          )}
        </span>
      ),
    },
    {
      key: "category",
      header: t("finmind.catalog.col_category"),
      cellClassName: "text-muted-foreground",
      render: (d) => t(`finmind.category.${d.category}`, { defaultValue: d.category }),
    },
    {
      key: "description_zh",
      header: t("finmind.catalog.col_description"),
      render: (d) => d.description_zh,
    },
    {
      key: "ingest_freq",
      header: t("finmind.catalog.col_freq"),
      cellClassName: "text-muted-foreground",
      render: (d) => t(`finmind.freq.${d.ingest_freq}`, { defaultValue: d.ingest_freq }),
    },
    {
      key: "per_symbol",
      header: t("finmind.catalog.col_per_symbol"),
      cellClassName: "text-muted-foreground",
      render: (d) => (d.per_symbol ? "✓" : "—"),
    },
    {
      key: "available",
      header: t("finmind.catalog.col_status"),
      render: (d) =>
        d.available ? (
          <span className="rounded bg-success/15 px-1.5 py-0.5 text-[10px] text-success">
            {t("finmind.catalog.status_available")}
          </span>
        ) : (
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {t("finmind.catalog.status_building")}
          </span>
        ),
    },
  ];

  return (
    <div className="space-y-8 p-4 lg:p-6">
      {/* Hero ─────────────────────────────────────────── */}
      <section className="rounded-lg border border-border bg-gradient-to-br from-primary/10 via-background to-background p-6 lg:p-8">
        <div className="max-w-3xl">
          <div className="mb-2 inline-block rounded bg-primary/15 px-2 py-0.5 text-xs font-medium text-primary">
            FinMind Mirror API
          </div>
          <h1 className="mb-3 text-2xl font-bold lg:text-3xl">
            {t("finmind.hero.title")}
          </h1>
          <p className="mb-4 text-sm leading-relaxed text-muted-foreground lg:text-base">
            {t("finmind.hero.desc_1")}
            <code className="rounded bg-muted px-1 text-xs">metadata</code>
            {t("finmind.hero.desc_2")}
          </p>
          <div className="flex flex-wrap gap-3 text-xs">
            <span className="rounded border border-border bg-background px-2 py-1">
              <span className="font-mono text-primary">{datasets.length}</span> {t("finmind.hero.stat_datasets")}
            </span>
            <span className="rounded border border-border bg-background px-2 py-1">
              <span className="font-mono text-success">{availableCount}</span> {t("finmind.hero.stat_available")}
            </span>
            <span className="rounded border border-border bg-background px-2 py-1">
              {t("finmind.hero.stat_compat")}
            </span>
            <span className="rounded border border-border bg-background px-2 py-1">
              {t("finmind.hero.stat_phase")}
            </span>
          </div>
        </div>
      </section>

      {/* Pricing ─────────────────────────────────────── */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">{t("finmind.pricing.heading")}</h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {PRICING_TIERS.map((tier) => (
            <div
              key={tier.code}
              className={`flex flex-col rounded-lg border p-5 ${
                tier.highlighted
                  ? "border-primary bg-primary/5 ring-2 ring-primary/20"
                  : "border-border bg-card"
              }`}
            >
              <div className="mb-1 flex items-center justify-between">
                <h3 className="text-base font-semibold">{tier.name}</h3>
                {tier.highlighted && (
                  <span className="rounded bg-primary/20 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                    {t("finmind.pricing.popular")}
                  </span>
                )}
              </div>
              <div className="mb-1 text-2xl font-bold">{t(`finmind.pricing.${tier.code}.price`)}</div>
              <div className="mb-4 text-xs text-muted-foreground">{t(`finmind.pricing.${tier.code}.note`)}</div>
              <div className="mb-4 space-y-1 text-xs">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">{t("finmind.pricing.api_calls")}</span>
                  <span className="font-mono">{t(`finmind.pricing.${tier.code}.calls`)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">{t("finmind.pricing.row_quota")}</span>
                  <span className="font-mono">{t(`finmind.pricing.${tier.code}.rows`)}</span>
                </div>
              </div>
              <ul className="mb-5 flex-1 space-y-1.5 text-sm">
                {tier.featureKeys.map((fk) => (
                  <li key={fk} className="flex gap-2">
                    <span className="text-success">✓</span>
                    <span>{t(`finmind.pricing.${tier.code}.${fk}`)}</span>
                  </li>
                ))}
              </ul>
              <button
                type="button"
                disabled
                className={`w-full rounded px-3 py-2 text-sm font-medium ${
                  tier.highlighted
                    ? "bg-primary text-primary-foreground opacity-60"
                    : "border border-border bg-background opacity-60"
                }`}
                title={t("finmind.pricing.checkout_soon_title")}
              >
                {t(`finmind.pricing.${tier.code}.cta`)}
              </button>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          {t("finmind.pricing.footnote")}
        </p>
      </section>

      {/* Catalog ─────────────────────────────────────── */}
      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">{t("finmind.catalog.heading")}</h2>
          <span className="text-xs text-muted-foreground">
            {filtered.length} / {datasets.length} {t("finmind.catalog.shown_suffix")}
          </span>
        </div>

        <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
          <input
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t("finmind.catalog.search_placeholder")}
            className="flex-1 min-w-[200px] rounded border border-border bg-background px-3 py-1.5"
          />
          <label className="flex items-center gap-2">
            {t("finmind.catalog.filter_category")}
            <select
              value={categoryFilter}
              onChange={(e) => setCategoryFilter(e.target.value)}
              className="rounded border border-border bg-background px-2 py-1"
            >
              <option value="all">{t("finmind.catalog.filter_all")}</option>
              {categories.map((c) => (
                <option key={c} value={c}>
                  {t(`finmind.category.${c}`, { defaultValue: c })}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={showOnlyAvailable}
              onChange={(e) => setShowOnlyAvailable(e.target.checked)}
            />
            {t("finmind.catalog.filter_available_only")}
          </label>
        </div>

        {catalogQuery.isLoading && (
          <div className="rounded border border-border bg-card p-6 text-center text-sm text-muted-foreground">
            {t("finmind.catalog.loading")}
          </div>
        )}
        {catalogQuery.isError && (
          <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm">
            {t("finmind.catalog.load_error", { detail: errorDetail(catalogQuery.error) })}
          </div>
        )}

        {catalogQuery.data && (
          <DataTable
            columns={columns}
            rows={filtered}
            rowKey={(d) => d.dataset_code}
            mobileMode="scroll"
            aria-label={t("finmind.catalog.aria_table")}
            className="rounded-lg border border-border bg-card"
            empty={
              <div className="rounded-lg border border-border bg-card px-3 py-6 text-center text-sm text-muted-foreground">
                {t("finmind.catalog.empty")}
              </div>
            }
          />
        )}
      </section>

      {/* API quickstart ─────────────────────────────── */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">{t("finmind.quickstart.heading")}</h2>
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="mb-3 text-sm text-muted-foreground">
            {t("finmind.quickstart.intro_1")}
            <code className="rounded bg-muted px-1">fck_live_</code>
            {t("finmind.quickstart.intro_2")}
          </p>
          <pre className="overflow-x-auto rounded bg-muted/50 p-3 text-xs leading-relaxed">
            <code>{`curl -H "X-Finmind-API-Key: fck_live_xxx" \\
  "https://fincept.example.com/api/finmind/data/TaiwanStockPrice?\\
data_id=2330&start_date=2024-01-01&end_date=2024-12-31&limit=10000"`}</code>
          </pre>
          <p className="mt-3 text-xs text-muted-foreground">
            {t("finmind.quickstart.resp_1")}
            <code className="rounded bg-muted px-1">{`{status, msg, data, metadata}`}</code>
            {t("finmind.quickstart.resp_2")}
          </p>
        </div>
      </section>
    </div>
  );
}
