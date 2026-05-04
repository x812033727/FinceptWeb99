import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import api, { errorDetail } from "@/lib/api";

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
  priceMonthly: string;
  priceNote: string;
  quotaCalls: string;
  quotaRows: string;
  features: string[];
  ctaLabel: string;
  highlighted?: boolean;
}

const PRICING_TIERS: PricingTier[] = [
  {
    code: "free",
    name: "Free",
    priceMonthly: "NT$0",
    priceNote: "永久免費",
    quotaCalls: "100 次 / 日",
    quotaRows: "10,000 列 / 日",
    features: [
      "完整資料集目錄瀏覽",
      "每日 100 次 API 呼叫",
      "FinMind 鏡像回應格式",
      "社群 Email 支援",
    ],
    ctaLabel: "聯絡管理員索取金鑰",
  },
  {
    code: "pro",
    name: "Pro",
    priceMonthly: "NT$1,490",
    priceNote: "每月,可取消",
    quotaCalls: "10,000 次 / 日",
    quotaRows: "1,000,000 列 / 日",
    features: [
      "Free 全部功能",
      "每日 10,000 次 API 呼叫",
      "CSV / NDJSON 批次匯出",
      "歷史回補(2017+)",
      "Email 24h 內回覆",
    ],
    ctaLabel: "即將開放 Stripe 結帳",
    highlighted: true,
  },
  {
    code: "enterprise",
    name: "Enterprise",
    priceMonthly: "客製",
    priceNote: "聯絡業務團隊",
    quotaCalls: "無上限",
    quotaRows: "無上限",
    features: [
      "Pro 全部功能",
      "客製資料集 / 排程",
      "私有部署 (on-prem)",
      "SLA 99.9%",
      "專屬技術窗口",
    ],
    ctaLabel: "聯絡業務",
  },
];

const CATEGORY_LABELS: Record<string, string> = {
  technical: "技術 / 價量",
  chip: "籌碼",
  fundamental: "基本面 / 財報",
  derivative: "衍生性商品",
  corporate: "公司行動",
  intraday: "盤中 / 即時",
  news: "新聞",
  master: "基本資訊",
};

const FREQ_LABELS: Record<string, string> = {
  daily: "每日",
  monthly: "每月",
  quarterly: "每季",
  weekly: "每週",
  realtime: "即時",
  on_demand: "依需求",
};

export default function FinmindPage() {
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

  return (
    <div className="space-y-8 p-4 lg:p-6">
      {/* Hero ─────────────────────────────────────────── */}
      <section className="rounded-lg border border-border bg-gradient-to-br from-primary/10 via-background to-background p-6 lg:p-8">
        <div className="max-w-3xl">
          <div className="mb-2 inline-block rounded bg-primary/15 px-2 py-0.5 text-xs font-medium text-primary">
            FinMind Mirror API
          </div>
          <h1 className="mb-3 text-2xl font-bold lg:text-3xl">
            台股完整資料,FinMind 相容介面
          </h1>
          <p className="mb-4 text-sm leading-relaxed text-muted-foreground lg:text-base">
            80 個資料集鏡像 FinMind 公開 API 的回應格式 — 從 FinMind
            遷移無需改寫呼叫端。每筆回應額外帶 <code className="rounded bg-muted px-1 text-xs">metadata</code>
            欄位讓你掌握資料新鮮度;CSV / NDJSON 批次匯出可串流至 100 萬列。
          </p>
          <div className="flex flex-wrap gap-3 text-xs">
            <span className="rounded border border-border bg-background px-2 py-1">
              <span className="font-mono text-primary">{datasets.length}</span> 個資料集
            </span>
            <span className="rounded border border-border bg-background px-2 py-1">
              <span className="font-mono text-green-600">{availableCount}</span> 個目前可查
            </span>
            <span className="rounded border border-border bg-background px-2 py-1">
              FinMind 回應格式相容
            </span>
            <span className="rounded border border-border bg-background px-2 py-1">
              Phase A → Phase B 可切換
            </span>
          </div>
        </div>
      </section>

      {/* Pricing ─────────────────────────────────────── */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">訂閱方案</h2>
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
                    熱門
                  </span>
                )}
              </div>
              <div className="mb-1 text-2xl font-bold">{tier.priceMonthly}</div>
              <div className="mb-4 text-xs text-muted-foreground">{tier.priceNote}</div>
              <div className="mb-4 space-y-1 text-xs">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">API 呼叫</span>
                  <span className="font-mono">{tier.quotaCalls}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">列數配額</span>
                  <span className="font-mono">{tier.quotaRows}</span>
                </div>
              </div>
              <ul className="mb-5 flex-1 space-y-1.5 text-sm">
                {tier.features.map((f) => (
                  <li key={f} className="flex gap-2">
                    <span className="text-green-600">✓</span>
                    <span>{f}</span>
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
                title="Stripe Checkout 整合即將上線"
              >
                {tier.ctaLabel}
              </button>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          ※ 訂閱與自助結帳目前尚未開放,如需試用請聯絡管理員開立 API
          金鑰。完整定價條款以正式合約為準。
        </p>
      </section>

      {/* Catalog ─────────────────────────────────────── */}
      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">資料集目錄</h2>
          <span className="text-xs text-muted-foreground">
            {filtered.length} / {datasets.length} 顯示中
          </span>
        </div>

        <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
          <input
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="搜尋資料集名稱或描述…"
            className="flex-1 min-w-[200px] rounded border border-border bg-background px-3 py-1.5"
          />
          <label className="flex items-center gap-2">
            分類
            <select
              value={categoryFilter}
              onChange={(e) => setCategoryFilter(e.target.value)}
              className="rounded border border-border bg-background px-2 py-1"
            >
              <option value="all">全部</option>
              {categories.map((c) => (
                <option key={c} value={c}>
                  {CATEGORY_LABELS[c] ?? c}
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
            僅顯示可查
          </label>
        </div>

        {catalogQuery.isLoading && (
          <div className="rounded border border-border bg-card p-6 text-center text-sm text-muted-foreground">
            載入資料集目錄…
          </div>
        )}
        {catalogQuery.isError && (
          <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm">
            無法載入目錄: {errorDetail(catalogQuery.error)}
          </div>
        )}

        {catalogQuery.data && (
          <div className="overflow-x-auto rounded-lg border border-border bg-card">
            <table className="w-full text-xs">
              <thead className="border-b border-border bg-muted/30 text-left">
                <tr>
                  <th className="px-3 py-2">資料集</th>
                  <th className="px-3 py-2">分類</th>
                  <th className="px-3 py-2">描述</th>
                  <th className="px-3 py-2">頻率</th>
                  <th className="px-3 py-2">每股</th>
                  <th className="px-3 py-2">狀態</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((d) => (
                  <tr
                    key={d.dataset_code}
                    className="border-b border-border last:border-b-0 hover:bg-muted/30"
                  >
                    <td className="px-3 py-2 font-mono">
                      {d.dataset_code}
                      {d.sponsor_tier && (
                        <span
                          className="ml-1 rounded bg-amber-100 px-1 text-[10px] text-amber-800 dark:bg-amber-900 dark:text-amber-200"
                          title="FinMind Sponsor 級資料集"
                        >
                          sponsor
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {CATEGORY_LABELS[d.category] ?? d.category}
                    </td>
                    <td className="px-3 py-2">{d.description_zh}</td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {FREQ_LABELS[d.ingest_freq] ?? d.ingest_freq}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {d.per_symbol ? "✓" : "—"}
                    </td>
                    <td className="px-3 py-2">
                      {d.available ? (
                        <span className="rounded bg-green-100 px-1.5 py-0.5 text-[10px] text-green-800 dark:bg-green-900 dark:text-green-200">
                          可查
                        </span>
                      ) : (
                        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          建置中
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
                {filtered.length === 0 && (
                  <tr>
                    <td
                      colSpan={6}
                      className="px-3 py-6 text-center text-muted-foreground"
                    >
                      無符合條件的資料集
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* API quickstart ─────────────────────────────── */}
      <section>
        <h2 className="mb-3 text-lg font-semibold">快速上手</h2>
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="mb-3 text-sm text-muted-foreground">
            取得 <code className="rounded bg-muted px-1">fck_live_</code>{" "}
            金鑰後,在 HTTP header 帶上即可:
          </p>
          <pre className="overflow-x-auto rounded bg-muted/50 p-3 text-xs leading-relaxed">
            <code>{`curl -H "X-Finmind-API-Key: fck_live_xxx" \\
  "https://fincept.example.com/api/finmind/data/TaiwanStockPrice?\\
data_id=2330&start_date=2024-01-01&end_date=2024-12-31&limit=10000"`}</code>
          </pre>
          <p className="mt-3 text-xs text-muted-foreground">
            回應格式與 FinMind 公開 API 一致 (
            <code className="rounded bg-muted px-1">{`{status, msg, data, metadata}`}</code>
            ),從 FinMind SDK 切換時不必改寫呼叫端。
          </p>
        </div>
      </section>
    </div>
  );
}
