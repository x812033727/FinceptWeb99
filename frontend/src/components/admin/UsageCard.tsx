import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Inbox } from "lucide-react";
import api from "@/lib/api";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import { DataTable, type DataTableColumn } from "@/components/ui/table";
import { EmptyState } from "@/components/ui/EmptyState";
import { StatCard } from "@/components/ui/StatCard";

interface UsageBucket {
  provider: string;
  model: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  tool_call_count: number;
}

interface UsageDay {
  date: string;
  cost_usd: number;
  requests: number;
}

interface ToolCallStat {
  name: string;
  count: number;
}

interface UsageSummary {
  range_days: number;
  user_scoped: boolean;
  total_requests: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_cost_usd: number;
  by_provider: UsageBucket[];
  by_day: UsageDay[];
  total_tool_calls: number;
  top_tools: ToolCallStat[];
}

// Stable identity color per provider, drawn from the categorical chart
// tokens (never the raw Tailwind palette — theme layers own the hues).
const PROVIDER_COLOR: Record<string, string> = {
  openai: "text-chart-1",
  anthropic: "text-chart-2",
  gemini: "text-chart-3",
  minimax: "text-chart-4",
  groq: "text-chart-5",
  deepseek: "text-chart-6",
  openrouter: "text-chart-2",
  ollama: "text-chart-3",
};

export function UsageCard({ scope }: { scope: "admin" | "me" }) {
  const { t } = useTranslation();
  const [days, setDays] = useState(30);
  // Per-scope storage key so the admin's collapse on the AdminPage
  // doesn't propagate to /settings (which embeds <UsageCard scope="me">).
  const { open, toggle } = useCollapsible(`usage.${scope}`);
  const path = scope === "admin" ? "/admin/llm-usage" : "/auth/llm-usage";
  const { data, isLoading } = useQuery<UsageSummary>({
    queryKey: ["llm-usage", scope, days],
    queryFn: () => api.get(`${path}?range_days=${days}`).then((r) => r.data),
  });

  const columns: DataTableColumn<UsageBucket>[] = [
    {
      key: "provider",
      header: t("personas.provider"),
      render: (b) => (
        <span className={`font-medium ${PROVIDER_COLOR[b.provider] ?? "text-foreground"}`}>
          {b.provider}
        </span>
      ),
      mobile: "primary",
    },
    {
      key: "model",
      header: t("personas.model"),
      cellClassName: "font-mono text-muted-foreground",
    },
    {
      key: "requests",
      header: t("usage.requests"),
      numeric: true,
      render: (b) => b.requests.toLocaleString(),
    },
    {
      key: "tokens",
      header: "tokens",
      numeric: true,
      cellClassName: "text-muted-foreground",
      render: (b) => (b.prompt_tokens + b.completion_tokens).toLocaleString(),
    },
    {
      key: "tool_calls",
      header: t("usage.tool_calls_short"),
      numeric: true,
      cellClassName: "text-muted-foreground",
      render: (b) => (b.tool_call_count ?? 0).toLocaleString(),
    },
    {
      key: "cost",
      header: t("usage.cost"),
      numeric: true,
      render: (b) => <span className="font-medium">${b.cost_usd.toFixed(4)}</span>,
      mobile: "primary",
    },
  ];

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title={t("usage.title")}
        subtitle={scope === "admin" ? t("usage.subtitle_admin") : t("usage.subtitle_me")}
        headerRight={
          open ? (
            <div
              className="flex gap-1"
              onClick={(e) => e.stopPropagation()}
            >
              {[7, 30, 90].map((d) => (
                <button
                  key={d}
                  onClick={(e) => { e.stopPropagation(); setDays(d); }}
                  className={`px-2.5 py-1 text-xs rounded ${
                    days === d
                      ? "bg-primary text-primary-foreground"
                      : "border border-border text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {d}d
                </button>
              ))}
            </div>
          ) : null
        }
      />

      {open && (
      <>
      {isLoading || !data ? (
        <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
      ) : data.total_requests === 0 ? (
        <EmptyState icon={Inbox} title={t("usage.empty")} className="py-4" />
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
            <StatCard label={t("usage.cost")} value={`$${data.total_cost_usd.toFixed(4)}`} compact />
            <StatCard label={t("usage.requests")} value={data.total_requests.toLocaleString()} compact />
            <StatCard label={t("usage.prompt_tokens")} value={data.total_prompt_tokens.toLocaleString()} compact />
            <StatCard label={t("usage.completion_tokens")} value={data.total_completion_tokens.toLocaleString()} compact />
            <StatCard label={t("usage.tool_calls")} value={(data.total_tool_calls ?? 0).toLocaleString()} compact />
          </div>

          <div className="space-y-1">
            <p className="text-micro text-muted-foreground uppercase tracking-wider">{t("usage.by_provider")}</p>
            <DataTable
              aria-label={t("usage.by_provider")}
              columns={columns}
              rows={data.by_provider}
              rowKey={(b) => `${b.provider}:${b.model}`}
              mobileMode="cards"
            />
          </div>

          {(data.total_tool_calls ?? 0) > 0 && (
            <div className="space-y-1">
              <p className="text-micro text-muted-foreground uppercase tracking-wider">{t("usage.top_tools")}</p>
              <div className="flex flex-wrap gap-1.5">
                {(data.top_tools ?? []).map((tool) => (
                  <span
                    key={tool.name}
                    className="inline-flex items-center gap-1.5 px-2 py-0.5 text-xs rounded bg-secondary/40 border border-border/40"
                  >
                    <span className="font-mono text-foreground">{tool.name}</span>
                    <span className="text-muted-foreground tabular-nums">×{tool.count.toLocaleString()}</span>
                  </span>
                ))}
              </div>
            </div>
          )}
        </>
      )}
      </>
      )}
    </div>
  );
}
