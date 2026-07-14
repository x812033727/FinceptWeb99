/**
 * PR-5b: lesson library — full-page browser for the operator's
 * accumulated learning archive.
 *
 * Filter strip (tier / market / search) + sortable table (hit
 * rate / usage / recency / created) + pagination. The active vs
 * archived split is a tab so operators can audit either bucket
 * without leaving the page.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import type {
  LessonLibraryResponse,
  LessonLibraryRow,
} from "@/components/discussion/_helpers";
import { DataTable, type DataTableColumn } from "@/components/ui/table";


type SortKey = "hit_rate" | "usage" | "recent" | "created";
type Tab = "active" | "archived";


export default function LessonLibraryPage() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("active");
  const [tier, setTier] = useState<string>("");
  const [market, setMarket] = useState<string>("");
  const [search, setSearch] = useState<string>("");
  const [sort, setSort] = useState<SortKey>("hit_rate");
  const [offset, setOffset] = useState<number>(0);
  const limit = 50;

  const { data, isLoading } = useQuery({
    queryKey: ["lesson-library", tab, tier, market, search, sort, offset],
    queryFn: async () => {
      const params = new URLSearchParams();
      params.set("archived", tab === "archived" ? "true" : "false");
      params.set("sort", sort);
      params.set("limit", String(limit));
      params.set("offset", String(offset));
      if (tier) params.set("tier", tier);
      if (market) params.set("market", market);
      if (search) params.set("search", search);
      const { data } = await api.get<LessonLibraryResponse>(
        `/discussion/lessons/library?${params.toString()}`,
      );
      return data;
    },
    staleTime: 30_000,
  });

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const hasNext = offset + limit < total;
  const hasPrev = offset > 0;

  // Reset offset whenever a filter/tab changes — the previous
  // offset doesn't make sense in the new result set.
  function setFilter<T>(setter: (v: T) => void, value: T) {
    setter(value);
    setOffset(0);
  }

  const columns: DataTableColumn<LessonLibraryRow>[] = [
    {
      key: "market",
      header: "market",
      cellClassName: "align-top font-mono text-muted-foreground",
      render: (row) => row.market,
    },
    {
      key: "tier",
      header: "tier",
      cellClassName: "align-top",
      render: (row) => {
        if (!row.tier) return null;
        const tierTone =
          row.tier === "structural"
            ? "bg-amber-900/40 text-amber-200 border-amber-700/50"
            : row.tier === "semantic"
              ? "bg-emerald-900/40 text-emerald-200 border-emerald-700/50"
              : "bg-blue-900/40 text-blue-200 border-blue-700/50";
        return (
          <span className={`inline-block px-1.5 py-0.5 text-micro rounded border ${tierTone}`}>
            {row.tier}
          </span>
        );
      },
    },
    {
      key: "category",
      header: "category",
      cellClassName: "align-top text-micro text-muted-foreground",
      render: (row) => row.category,
    },
    {
      key: "lesson",
      header: t("lesson_library.col.lesson"),
      cellClassName: "align-top max-w-md",
      render: (row) => (
        <>
          <div className="break-words">{row.lesson_text}</div>
          {(row.related_symbols.length > 0 || row.missed_winners.length > 0) && (
            <div className="text-micro text-muted-foreground mt-0.5">
              {row.related_symbols.length > 0 && (
                <span>related: {row.related_symbols.join(", ")}</span>
              )}
              {row.missed_winners.length > 0 && (
                <span className="ml-2">missed: {row.missed_winners.join(", ")}</span>
              )}
            </div>
          )}
        </>
      ),
    },
    {
      key: "usage",
      header: t("lesson_library.col.usage"),
      numeric: true,
      cellClassName: "align-top",
      render: (row) => `${row.hit_count}/${row.usage_count}`,
    },
    {
      key: "rate",
      header: t("lesson_library.col.rate"),
      numeric: true,
      cellClassName: "align-top",
      render: (row) =>
        row.recent_hit_rate_10 !== null
          ? `${(row.recent_hit_rate_10 * 100).toFixed(0)}%`
          : "—",
    },
    {
      key: "last_used",
      header: t("lesson_library.col.last_used"),
      align: "right",
      cellClassName: "align-top text-muted-foreground",
      render: (row) =>
        row.last_used_at ? new Date(row.last_used_at).toLocaleDateString() : "—",
    },
  ];

  return (
    <div className="p-gutter sm:p-page space-y-stack max-w-6xl">
      <div className="flex items-baseline justify-between gap-2">
        <h1 className="text-title font-semibold">
          {t("lesson_library.title")}
        </h1>
        <span className="text-xs text-muted-foreground">
          {t("lesson_library.total", { count: total })}
        </span>
      </div>

      {/* Tabs: active vs archived */}
      <div className="flex gap-2 border-b border-border">
        {(["active", "archived"] as Tab[]).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setFilter(setTab, k)}
            className={`px-3 py-1.5 text-xs border-b-2 -mb-px ${
              tab === k
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            {t(`lesson_library.tab.${k}`)}
          </button>
        ))}
      </div>

      {/* Filter strip */}
      <div className="flex flex-wrap items-end gap-2">
        <FilterField label={t("lesson_library.filter.tier")}>
          <select
            value={tier}
            onChange={(e) => setFilter(setTier, e.target.value)}
            className="bg-background border border-border rounded px-2 py-1 text-xs"
          >
            <option value="">{t("lesson_library.filter.all")}</option>
            <option value="episodic">episodic</option>
            <option value="semantic">semantic</option>
            <option value="structural">structural</option>
          </select>
        </FilterField>
        <FilterField label={t("lesson_library.filter.market")}>
          <select
            value={market}
            onChange={(e) => setFilter(setMarket, e.target.value)}
            className="bg-background border border-border rounded px-2 py-1 text-xs"
          >
            <option value="">{t("lesson_library.filter.all")}</option>
            <option value="TW">TW</option>
            <option value="US">US</option>
            <option value="GLOBAL">GLOBAL</option>
          </select>
        </FilterField>
        <FilterField label={t("lesson_library.filter.sort")}>
          <select
            value={sort}
            onChange={(e) => setFilter(setSort, e.target.value as SortKey)}
            className="bg-background border border-border rounded px-2 py-1 text-xs"
          >
            <option value="hit_rate">{t("lesson_library.sort.hit_rate")}</option>
            <option value="usage">{t("lesson_library.sort.usage")}</option>
            <option value="recent">{t("lesson_library.sort.recent")}</option>
            <option value="created">{t("lesson_library.sort.created")}</option>
          </select>
        </FilterField>
        <FilterField label={t("lesson_library.filter.search")}>
          <input
            type="text"
            value={search}
            onChange={(e) => setFilter(setSearch, e.target.value)}
            placeholder={t("lesson_library.filter.search_placeholder")}
            className="bg-background border border-border rounded px-2 py-1 text-xs w-48"
          />
        </FilterField>
      </div>

      {/* Table */}
      {isLoading && (
        <div className="text-xs text-muted-foreground animate-pulse">
          {t("common.loading")}
        </div>
      )}
      {!isLoading && items.length === 0 && (
        <div className="text-xs text-muted-foreground italic">
          {t("lesson_library.empty")}
        </div>
      )}
      {items.length > 0 && (
        <DataTable
          columns={columns}
          rows={items}
          rowKey={(row) => row.id}
          mobileMode="scroll"
          aria-label={t("lesson_library.title")}
        />
      )}

      {/* Pagination */}
      {(hasPrev || hasNext) && (
        <div className="flex items-center justify-between text-xs">
          <button
            type="button"
            disabled={!hasPrev}
            onClick={() => setOffset(Math.max(0, offset - limit))}
            className="px-2 py-0.5 border border-border rounded
                       text-muted-foreground hover:text-foreground
                       disabled:opacity-30"
          >
            ← {t("lesson_library.prev")}
          </button>
          <span className="text-muted-foreground">
            {offset + 1}–{Math.min(offset + limit, total)} / {total}
          </span>
          <button
            type="button"
            disabled={!hasNext}
            onClick={() => setOffset(offset + limit)}
            className="px-2 py-0.5 border border-border rounded
                       text-muted-foreground hover:text-foreground
                       disabled:opacity-30"
          >
            {t("lesson_library.next")} →
          </button>
        </div>
      )}
    </div>
  );
}


function FilterField({
  label, children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-0.5">
      <span className="text-micro text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}
