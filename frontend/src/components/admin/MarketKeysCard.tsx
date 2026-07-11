import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";

interface MarketKeyInfo {
  provider: string;
  has_key: boolean;
  source: "db" | "env" | "none";
  masked: string;
  last_validated_at: string | null;
  last_validation_ok: boolean | null;
  last_validation_message: string | null;
  updated_at: string | null;
}

const MARKET_PROVIDER_LABELS: Record<string, { name: string; tagline: string; placeholder: string; signupUrl: string }> = {
  finnhub: {
    name: "Finnhub",
    tagline: "US 4th-tier quote fallback · 60 req/min free",
    placeholder: "c…",
    signupUrl: "https://finnhub.io/dashboard",
  },
  finmind: {
    name: "FinMind",
    tagline: "TW news + institutional + monthly revenue · 600 req/day free",
    placeholder: "eyJhbGc…",
    signupUrl: "https://finmindtrade.com/",
  },
  fred: {
    name: "FRED",
    tagline: "US macro indicators (CPI, GDP, fed funds, yield curve, DXY) · free",
    placeholder: "abc123…",
    signupUrl: "https://fred.stlouisfed.org/docs/api/api_key.html",
  },
};

function MarketKeyRow({ info }: { info: MarketKeyInfo }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const meta = MARKET_PROVIDER_LABELS[info.provider] ?? {
    name: info.provider, tagline: "", placeholder: "API key", signupUrl: "",
  };
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: () => api.put(`/admin/market-keys/${info.provider}`, { api_key: draft }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "market-keys"] });
      setDraft("");
      setError(null);
    },
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "save failed");
    },
  });

  const clear = useMutation({
    mutationFn: () => api.delete(`/admin/market-keys/${info.provider}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "market-keys"] }),
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "clear failed");
    },
  });

  const test = useMutation<{ data: { ok: boolean; message: string } }>({
    mutationFn: () => api.post(`/admin/market-keys/${info.provider}/test`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "market-keys"] }),
    onError: (err: Error) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? "test failed");
    },
  });

  const sourceBadge =
    info.source === "db" ? "DB" :
    info.source === "env" ? ".env" :
    "—";
  const sourceColor =
    info.source === "db" ? "text-success bg-success/10 border-success/30" :
    info.source === "env" ? "text-blue-400 bg-blue-400/10 border-blue-400/30" :
    "text-muted-foreground bg-muted/10 border-border";

  const lastValidationBadge = info.last_validation_ok === true
    ? <span className="text-xs text-success">✓ {t("llm_keys.validated")}</span>
    : info.last_validation_ok === false
    ? <span className="text-xs text-danger" title={info.last_validation_message ?? ""}>
        ✗ {info.last_validation_message ? info.last_validation_message.slice(0, 60) : t("llm_keys.invalid")}
      </span>
    : null;

  const testResult = test.data?.data;

  return (
    <div className="border border-border rounded-lg p-3 space-y-2">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <div>
          <span className="font-medium text-sm">{meta.name}</span>
          <span className="text-xs text-muted-foreground ml-2">{meta.tagline}</span>
          {meta.signupUrl && !info.has_key && (
            <a
              href={meta.signupUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-primary hover:underline ml-2"
            >
              {t("market_keys.get_key")} →
            </a>
          )}
        </div>
        <span className={`text-[10px] border px-1.5 py-0.5 rounded ${sourceColor}`}>{sourceBadge}</span>
      </div>

      {info.has_key && (
        <p className="text-xs text-muted-foreground font-mono">
          {info.masked || "(set)"}
        </p>
      )}

      <div className="flex gap-2 items-center flex-wrap">
        <input
          type="password"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={info.has_key ? t("llm_keys.replace_placeholder") : meta.placeholder}
          className="flex-1 min-w-[200px] bg-background border border-border rounded px-2 py-1.5 text-xs font-mono text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:border-primary/50"
        />
        <button
          onClick={() => draft.trim() && save.mutate()}
          disabled={!draft.trim() || save.isPending}
          className="px-3 py-1.5 text-xs bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-40"
        >
          {save.isPending ? t("common.saving") : t("common.save")}
        </button>
        {info.has_key && info.source === "db" && (
          <button
            onClick={() => clear.mutate()}
            disabled={clear.isPending}
            className="px-3 py-1.5 text-xs border border-border text-muted-foreground rounded hover:text-danger disabled:opacity-40"
          >
            {t("llm_keys.clear")}
          </button>
        )}
        <button
          onClick={() => test.mutate()}
          disabled={!info.has_key || test.isPending}
          className="px-3 py-1.5 text-xs border border-border text-muted-foreground rounded hover:text-foreground disabled:opacity-40"
        >
          {test.isPending ? t("llm_keys.testing") : t("llm_keys.test")}
        </button>
      </div>

      <div className="flex items-center gap-3 min-h-[1.25rem]">
        {error && <span className="text-xs text-danger">{error}</span>}
        {testResult && (
          <span className={`text-xs ${testResult.ok ? "text-success" : "text-danger"}`}>
            {testResult.ok ? `✓ ${t("llm_keys.validated")}` : `✗ ${testResult.message.slice(0, 80)}`}
          </span>
        )}
        {!error && !testResult && lastValidationBadge}
      </div>
    </div>
  );
}

export function MarketKeysCard() {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible("admin.market-keys");
  const { data: keys = [], isLoading } = useQuery<MarketKeyInfo[]>({
    queryKey: ["admin", "market-keys"],
    queryFn: () => api.get("/admin/market-keys").then((r) => r.data),
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title={t("market_keys.title")}
        subtitle={t("market_keys.subtitle")}
      />
      {open && (
        isLoading ? (
          <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>
        ) : (
          <div className="space-y-2">
            {keys.map((k) => <MarketKeyRow key={k.provider} info={k} />)}
          </div>
        )
      )}
    </div>
  );
}
