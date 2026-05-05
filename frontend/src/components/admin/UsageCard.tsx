import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";

interface UsageBucket {
  provider: string;
  model: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
}

interface UsageDay {
  date: string;
  cost_usd: number;
  requests: number;
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
}

const PROVIDER_COLOR: Record<string, string> = {
  openai: "text-green-400",
  anthropic: "text-orange-400",
  gemini: "text-blue-400",
  minimax: "text-purple-400",
  groq: "text-pink-400",
  deepseek: "text-cyan-400",
  openrouter: "text-yellow-400",
  ollama: "text-violet-400",
};

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-secondary/30 rounded p-2">
      <p className="text-[10px] text-muted-foreground uppercase tracking-wider">{label}</p>
      <p className="text-sm font-semibold mt-0.5 tabular-nums">{value}</p>
    </div>
  );
}

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
        <p className="text-xs text-muted-foreground py-3">{t("usage.empty")}</p>
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <Stat label={t("usage.cost")} value={`$${data.total_cost_usd.toFixed(4)}`} />
            <Stat label={t("usage.requests")} value={data.total_requests.toLocaleString()} />
            <Stat label={t("usage.prompt_tokens")} value={data.total_prompt_tokens.toLocaleString()} />
            <Stat label={t("usage.completion_tokens")} value={data.total_completion_tokens.toLocaleString()} />
          </div>

          <div className="space-y-1">
            <p className="text-[10px] text-muted-foreground uppercase tracking-wider">{t("usage.by_provider")}</p>
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted-foreground border-b border-border/50">
                  <th className="text-left py-1 font-medium">{t("personas.provider")}</th>
                  <th className="text-left py-1 font-medium">{t("personas.model")}</th>
                  <th className="text-right py-1 font-medium">{t("usage.requests")}</th>
                  <th className="text-right py-1 font-medium">tokens</th>
                  <th className="text-right py-1 font-medium">{t("usage.cost")}</th>
                </tr>
              </thead>
              <tbody>
                {data.by_provider.map((b, i) => (
                  <tr key={i} className="border-b border-border/30">
                    <td className={`py-1 font-medium ${PROVIDER_COLOR[b.provider] ?? "text-foreground"}`}>{b.provider}</td>
                    <td className="py-1 font-mono text-muted-foreground">{b.model}</td>
                    <td className="py-1 text-right">{b.requests.toLocaleString()}</td>
                    <td className="py-1 text-right text-muted-foreground">
                      {(b.prompt_tokens + b.completion_tokens).toLocaleString()}
                    </td>
                    <td className="py-1 text-right font-medium">${b.cost_usd.toFixed(4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      </>
      )}
    </div>
  );
}
