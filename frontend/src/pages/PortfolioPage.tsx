import { useState } from "react";
import { Bot } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { formatPct } from "@/lib/formatters";
import {
  usePortfolios,
  usePortfolioDetail,
  useDeletePortfolio,
  useOptimise,
} from "@/hooks/usePortfolio";
import HoldingsTable from "@/components/portfolio/HoldingsTable";
import AllocationPie from "@/components/portfolio/AllocationPie";
import { AddTransactionForm } from "@/components/portfolio/AddTransactionForm";
import { CreatePortfolioModal } from "@/components/portfolio/CreatePortfolioModal";
import { EditPortfolioModal } from "@/components/portfolio/EditPortfolioModal";
import { ExpertEvaluationCard } from "@/components/portfolio/ExpertEvaluationCard";
import { PerformanceChart } from "@/components/portfolio/PerformanceChart";
import { PortfolioAIReviewCard } from "@/components/portfolio/PortfolioAIReviewCard";
import { RiskDashboardPanel } from "@/components/portfolio/RiskDashboardPanel";
import RebalancePanel from "@/components/portfolio/RebalancePanel";
import StressTestPanel from "@/components/portfolio/StressTestPanel";
import AttributionPanel from "@/components/portfolio/AttributionPanel";
import { TransactionHistory } from "@/components/portfolio/TransactionHistory";
import CashLedgerPanel from "@/components/portfolio/CashLedgerPanel";
import PaperTradingPanel from "@/components/portfolio/PaperTradingPanel";
import { exportCSV } from "@/components/portfolio/_shared";

export default function PortfolioPage() {
  const { t } = useTranslation();
  const { data: portfolios, isLoading } = usePortfolios();
  const [selected, setSelected] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showAddTx, setShowAddTx] = useState(false);
  const [showEdit, setShowEdit] = useState(false);

  const activeId = selected ?? portfolios?.[0]?.id ?? null;
  const { data: detail, isFetching } = usePortfolioDetail(activeId);
  const deleteP = useDeletePortfolio();
  const optimise = useOptimise(activeId ?? "");

  const navigate = useNavigate();

  function analyseWithAI() {
    navigate("/ai", {
      state: {
        agentId: "portfolio_advisor",
        initialMessage: "Review my portfolio and suggest improvements to the allocation, risk profile, and any concentration risks.",
        context: { portfolio: detail ?? null },
      },
    });
  }

  return (
    <div className="min-h-screen bg-background p-gutter sm:p-page space-y-stack sm:space-y-section">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <h1 className="text-title font-semibold text-foreground">{t("portfolio.title")}</h1>
        <div className="flex gap-2">
          {detail && (
            <button
              onClick={analyseWithAI}
              className="flex-1 sm:flex-none px-3 sm:px-4 py-2 text-sm bg-primary/10 border border-primary/30 text-primary rounded-md hover:bg-primary/20 transition-colors"
            >
              <span className="inline-flex items-center gap-1.5"><Bot className="h-4 w-4" aria-hidden="true" /> {t("nav.ai")}</span>
            </button>
          )}
          <button onClick={() => setShowCreate(true)} className="flex-1 sm:flex-none px-3 sm:px-4 py-2 text-sm bg-primary text-primary-foreground rounded-md hover:opacity-90 whitespace-nowrap">
            + {t("portfolio.new_portfolio")}
          </button>
        </div>
      </div>

      {isLoading && <p className="text-muted-foreground text-sm">{t("common.loading")}</p>}

      {/* Portfolio selector tabs */}
      {portfolios && portfolios.length > 0 && (
        <div className="flex gap-2 flex-wrap">
          {portfolios.map((p) => (
            <button
              key={p.id}
              onClick={() => setSelected(p.id)}
              className={`px-4 py-1.5 text-sm rounded-full border transition-colors ${
                (selected ?? portfolios[0].id) === p.id
                  ? "bg-primary text-primary-foreground border-primary"
                  : "border-border text-muted-foreground hover:text-foreground"
              }`}
            >
              {p.name} <span className="text-xs opacity-60">{p.currency}</span>
            </button>
          ))}
        </div>
      )}

      {/* Portfolio detail */}
      {activeId && (
        <PortfolioDetail
          portfolioId={activeId}
          detail={detail}
          isFetching={isFetching}
          onAddTx={() => setShowAddTx(true)}
          onEdit={() => setShowEdit(true)}
          onDelete={async () => {
            await deleteP.mutateAsync(activeId);
            setSelected(null);
          }}
          optimiseResult={optimise.data}
          optimisePending={optimise.isPending}
          onRunOptimise={(risk: string) => optimise.mutate({ target_risk: risk, max_weight: 1 })}
        />
      )}

      {!portfolios?.length && !isLoading && (
        <div className="text-center py-16 text-muted-foreground">
          <p className="text-lg">{t("portfolio.no_portfolios")}</p>
        </div>
      )}

      {showCreate && <CreatePortfolioModal onClose={() => setShowCreate(false)} />}
      {showAddTx && activeId && <AddTransactionForm portfolioId={activeId} onClose={() => setShowAddTx(false)} />}
      {showEdit && activeId && detail && (
        <EditPortfolioModal
          portfolioId={activeId}
          currentName={detail.name}
          currentCurrency={detail.currency}
          onClose={() => setShowEdit(false)}
        />
      )}
    </div>
  );
}

// ── Portfolio detail panel ────────────────────────────────────────
function PortfolioDetail({
  portfolioId, detail, isFetching,
  onAddTx, onEdit, onDelete,
  optimiseResult, optimisePending, onRunOptimise,
}: any) {
  const { t } = useTranslation();
  const [detailTab, setDetailTab] = useState<"overview" | "paper" | "cash" | "attribution" | "risk" | "stress" | "rebalance" | "transactions">("overview");

  if (!detail) return <div className="text-muted-foreground text-sm">{isFetching ? t("common.loading") : ""}</div>;

  const pnlPositive = detail.total_pnl >= 0;

  function exportHoldings() {
    exportCSV(
      detail.holdings.map((h: any) => ({
        symbol: h.symbol,
        market: h.market,
        quantity: h.quantity,
        avg_cost: h.avg_cost,
        current_price: h.current_price,
        current_value: h.current_value,
        unrealized_pnl: h.unrealized_pnl,
        unrealized_pnl_pct: h.unrealized_pnl_pct,
        weight_pct: h.weight_pct,
      })),
      `holdings-${portfolioId}.csv`
    );
  }

  return (
    <div className="space-y-6">
      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        {[
          { label: t("portfolio.summary.net_liquidation_value"), value: `${detail.currency} ${(detail.net_liquidation_value ?? detail.total_value).toLocaleString()}` },
          { label: t("portfolio.summary.cash"), value: `${detail.currency} ${(detail.cash_value ?? 0).toLocaleString()}` },
          { label: t("portfolio.summary.total_cost"),  value: `${detail.currency} ${detail.total_cost.toLocaleString()}` },
          {
            label: t("portfolio.summary.unrealized_pnl"),
            value: `${pnlPositive ? "+" : ""}${detail.total_pnl.toLocaleString(undefined, { maximumFractionDigits: 0 })}`,
            color: pnlPositive ? "text-positive" : "text-negative",
          },
          {
            label: t("portfolio.summary.performance"),
            value: formatPct(detail.total_pnl_pct),
            color: pnlPositive ? "text-positive" : "text-negative",
          },
        ].map((c) => (
          <div key={c.label} className="bg-card shadow-highlight border border-border rounded-lg p-4">
            <p className="text-xs text-muted-foreground">{c.label}</p>
            <p className={`text-lg font-semibold mt-1 ${c.color ?? "text-foreground"}`}>{c.value}</p>
          </div>
        ))}
      </div>

      {/* Tab selector */}
      <div className="flex max-w-full flex-wrap gap-1 rounded-lg bg-secondary/30 p-1 sm:w-fit">
        {(["overview", "paper", "cash", "attribution", "risk", "stress", "rebalance", "transactions"] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setDetailTab(tab)}
            className={`px-4 py-1.5 text-sm rounded-md transition-colors capitalize ${
              detailTab === tab
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab === "overview"
              ? t("portfolio.tabs.holdings")
              : tab === "paper"
                ? t("portfolio.tabs.paper")
              : tab === "cash"
                ? t("portfolio.tabs.cash")
              : tab === "attribution"
                ? t("portfolio.tabs.attribution")
              : tab === "risk"
                ? t("portfolio.tabs.risk")
                : tab === "stress"
                  ? "Stress test"
                : tab === "rebalance"
                  ? t("portfolio.tabs.rebalance")
                  : t("portfolio.tabs.transactions")}
          </button>
        ))}
      </div>

      {detailTab === "overview" && (
        <>
          {/* Holdings + pie */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2 bg-card shadow-highlight border border-border rounded-lg p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-foreground font-medium">{t("portfolio.tabs.holdings")}</h2>
                <div className="flex gap-2">
                  <button
                    onClick={exportHoldings}
                    disabled={!detail.holdings.length}
                    className="text-xs text-primary hover:underline disabled:opacity-40"
                  >
                    CSV
                  </button>
                  <button onClick={onAddTx} className="text-xs px-3 py-1 bg-primary/10 text-primary rounded hover:bg-primary/20">
                    + {t("portfolio.transactions.add")}
                  </button>
                </div>
              </div>
              <HoldingsTable holdings={detail.holdings} currency={detail.currency} />
            </div>

            <div className="bg-card shadow-highlight border border-border rounded-lg p-5">
              <h2 className="text-foreground font-medium mb-2">{t("portfolio.tabs.allocation")}</h2>
              <AllocationPie holdings={detail.holdings} />
            </div>
          </div>

          {/* Performance chart — benchmark switches with currency
              (TWD → TAIEX TR, USD/other → SPY). PR #191 fixed the
              prior SPY-everywhere default. */}
          <PerformanceChart portfolioId={portfolioId} currency={detail.currency} />

      {/* Expert evaluation — pick a persona, get an in-place AI review */}
      <ExpertEvaluationCard portfolioId={portfolioId} detail={detail} />

      {/* Optimiser */}
      <div className="bg-card shadow-highlight border border-border rounded-lg p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-foreground font-medium">{t("portfolio.optimizer.title")}</h2>
            <p className="text-xs text-muted-foreground mt-0.5">{t("portfolio.optimizer.description")}</p>
          </div>
          <div className="flex gap-2">
            {([
              { key: "low", label: t("portfolio.optimizer.risk_low") },
              { key: "medium", label: t("portfolio.optimizer.risk_medium") },
              { key: "high", label: t("portfolio.optimizer.risk_high") },
            ] as const).map(({ key, label }) => (
              <button
                key={key}
                onClick={() => onRunOptimise(key)}
                disabled={optimisePending}
                className="px-3 py-1 text-xs border border-border rounded hover:bg-secondary/50 disabled:opacity-40"
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {optimisePending && <p className="text-sm text-muted-foreground animate-pulse">{t("portfolio.optimizer.running")}</p>}

        {optimiseResult && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {[
                { k: t("portfolio.optimizer.expected_return"), v: `${(optimiseResult.metrics.expected_annual_return * 100).toFixed(1)}%` },
                { k: t("portfolio.optimizer.expected_vol"),    v: `${(optimiseResult.metrics.annual_volatility * 100).toFixed(1)}%` },
                { k: t("portfolio.optimizer.sharpe"),          v: optimiseResult.metrics.sharpe_ratio.toFixed(2) },
                { k: t("analytics.backtest.max_drawdown"),     v: `${(optimiseResult.metrics.max_drawdown * 100).toFixed(1)}%` },
              ].map((m) => (
                <div key={m.k} className="bg-secondary/30 rounded p-3">
                  <p className="text-xs text-muted-foreground">{m.k}</p>
                  <p className="text-sm font-medium text-foreground mt-0.5">{m.v}</p>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap gap-2">
              {Object.entries(optimiseResult.weights).map(([sym, w]: [string, any]) => (
                <span key={sym} className="text-xs bg-secondary/40 px-2 py-1 rounded">
                  {sym} <span className="text-primary font-medium">{(w * 100).toFixed(1)}%</span>
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="flex justify-end gap-3">
        <button
          onClick={onEdit}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          {t("common.edit")}
        </button>
        <button
          onClick={() => {
            if (confirm(t("portfolio.confirm_delete_portfolio"))) onDelete();
          }}
          className="text-xs text-negative/70 hover:text-negative"
        >
          {t("common.delete")}
        </button>
      </div>
        </>
      )}

      {detailTab === "rebalance" && (
        <RebalancePanel portfolioId={portfolioId} />
      )}

      {detailTab === "paper" && <PaperTradingPanel portfolioId={portfolioId} />}

      {detailTab === "cash" && (
        <CashLedgerPanel portfolioId={portfolioId} defaultCurrency={detail.currency} />
      )}

      {detailTab === "attribution" && <AttributionPanel portfolioId={portfolioId} />}

      {detailTab === "risk" && (
        <>
          <RiskDashboardPanel portfolioId={portfolioId} />
          {/* B5 — AI health check streams a zh-TW review of the same
              risk numbers shown above, plus market-regime fit. */}
          <PortfolioAIReviewCard portfolioId={portfolioId} />
        </>
      )}

      {detailTab === "stress" && <StressTestPanel portfolioId={portfolioId} />}

      {detailTab === "transactions" && (
        <TransactionHistory portfolioId={portfolioId} />
      )}
    </div>
  );
}
