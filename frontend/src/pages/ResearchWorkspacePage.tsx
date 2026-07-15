import { useState, type FormEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, CalendarClock, Plus } from "lucide-react";

import api, { errorDetail } from "@/lib/api";
import { PageHeader } from "@/components/ui/PageHeader";

type Thesis = {
  id: string; market: string; symbol: string; title: string; status: string;
  core_case: string; catalysts: unknown[]; risks: unknown[];
  valuation: Record<string, unknown>; watch_conditions: Array<WatchCondition | string>;
  review_date: string | null; last_reviewed_at: string | null;
};
type WatchCondition = {
  id?: string; label: string;
  metric: "revenue_yoy_pct" | "revenue_mom_pct" | "foreign_net";
  operator: "lt" | "lte" | "gt" | "gte" | "eq";
  threshold: number;
};
type ThesisForm = {
  market: string; symbol: string; title: string; core_case: string;
  review_date: string; watch_conditions: WatchCondition[];
};
type TimelineEvent = {
  id: string; event_type: string; title: string; details: Record<string, unknown>;
  source: string | null; occurred_at: string;
};
type DecisionEntry = {
  id: string; source_type: string; market: string; symbol: string; confidence: number | null;
  entry_price: number | null; outcomes: Record<string, { resolved: boolean; net_return_pct: number | null }>;
  max_drawdown_pct: number | null; observations: number; status: string;
};
type PickCandidate = {
  rank: number; symbol: string; market: string; score: number; confidence: number;
  rationale: string; source_report_id: string; report_quality: number;
  quality_details: { band?: string; issue_counts?: Record<string, number> };
  evidence: Array<{ id?: string; path?: string; source?: string; as_of?: string | null }>;
};
type PickRun = {
  id: string; market: string; run_date: string; methodology_version: string;
  candidate_count: number; candidates: PickCandidate[]; generated_at: string;
};

function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-lg border border-border bg-card p-4 shadow-highlight ${className}`}>{children}</div>;
}

function pct(value: number | null | undefined) {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

export default function ResearchWorkspacePage() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<"summary" | "picks" | "theses" | "journal">("summary");
  const [showCreate, setShowCreate] = useState(false);
  const [showFeedback, setShowFeedback] = useState(false);
  const [selected, setSelected] = useState<Thesis | null>(null);
  const [form, setForm] = useState<ThesisForm>({ market: "TW", symbol: "", title: "", core_case: "", review_date: "", watch_conditions: [] });
  const [condition, setCondition] = useState({ label: "", metric: "revenue_yoy_pct", operator: "lt", threshold: "" });
  const [review, setReview] = useState({ conclusion: "unchanged", notes: "", next_review_date: "" });
  const [feedback, setFeedback] = useState({ market: "TW", symbol: "", category: "stale", endpoint: "", description: "" });

  const summary = useQuery({
    queryKey: ["weekly-research-summary"],
    queryFn: () => api.get("/research/weekly-summary").then((r) => r.data),
  });
  const theses = useQuery<Thesis[]>({
    queryKey: ["theses"], queryFn: () => api.get("/theses").then((r) => r.data),
  });
  const journal = useQuery<{ entries: DecisionEntry[]; summary: any }>({
    queryKey: ["decision-journal"], queryFn: () => api.get("/decision-journal").then((r) => r.data),
  });
  const picks = useQuery<{ runs: PickRun[]; disclaimer: string }>({
    queryKey: ["daily-picks-latest"],
    queryFn: () => api.get("/research/daily-picks/latest").then((r) => r.data),
  });
  const timeline = useQuery<TimelineEvent[]>({
    queryKey: ["thesis-timeline", selected?.id], enabled: !!selected,
    queryFn: () => api.get(`/theses/${selected!.id}/timeline`).then((r) => r.data),
  });

  const create = useMutation({
    mutationFn: () => api.post("/theses", {
      ...form, symbol: form.symbol.toUpperCase(), review_date: form.review_date || null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["theses"] });
      qc.invalidateQueries({ queryKey: ["weekly-research-summary"] });
      setShowCreate(false);
      setForm({ market: "TW", symbol: "", title: "", core_case: "", review_date: "", watch_conditions: [] });
      setCondition({ label: "", metric: "revenue_yoy_pct", operator: "lt", threshold: "" });
    },
  });
  const submitReview = useMutation({
    mutationFn: () => api.post(`/theses/${selected!.id}/review`, {
      ...review, next_review_date: review.next_review_date || null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["theses"] });
      qc.invalidateQueries({ queryKey: ["thesis-timeline", selected?.id] });
      qc.invalidateQueries({ queryKey: ["weekly-research-summary"] });
      setReview({ conclusion: "unchanged", notes: "", next_review_date: "" });
    },
  });
  const submitFeedback = useMutation({
    mutationFn: () => api.post("/feedback/data-quality", {
      ...feedback, symbol: feedback.symbol.trim().toUpperCase() || null,
      endpoint: feedback.endpoint.trim() || null, observed_meta: {},
    }),
    onSuccess: () => {
      setShowFeedback(false);
      setFeedback({ market: "TW", symbol: "", category: "stale", endpoint: "", description: "" });
    },
  });
  const generatePicks = useMutation({
    mutationFn: (market: "TW" | "US") => api.post(`/research/daily-picks/generate?market=${market}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["daily-picks-latest"] });
      qc.invalidateQueries({ queryKey: ["decision-journal"] });
      qc.invalidateQueries({ queryKey: ["weekly-research-summary"] });
    },
  });

  function createThesis(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  function addWatchCondition() {
    const threshold = Number(condition.threshold);
    if (!condition.label.trim() || condition.threshold.trim() === "" || !Number.isFinite(threshold)) return;
    setForm({
      ...form,
      watch_conditions: [...form.watch_conditions, {
        label: condition.label.trim(),
        metric: condition.metric as WatchCondition["metric"],
        operator: condition.operator as WatchCondition["operator"],
        threshold,
      }],
    });
    setCondition({ label: "", metric: "revenue_yoy_pct", operator: "lt", threshold: "" });
  }

  const loading = summary.isLoading || theses.isLoading || journal.isLoading || picks.isLoading;
  return (
    <div className="min-h-screen bg-background p-gutter sm:p-page space-y-section">
      <PageHeader
        title="Research Workspace"
        description="Track investment theses, evidence-driven events and realised decision outcomes. Decision support only."
        actions={<>
          <button onClick={() => setShowFeedback(true)} className="rounded-md border border-border px-3 py-2 text-sm text-muted-foreground hover:text-foreground">Report data issue</button>
          <button onClick={() => setShowCreate(true)} className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground">
            <Plus className="h-4 w-4" /> New thesis
          </button>
        </>}
      />

      <div className="flex w-fit gap-1 rounded-lg bg-secondary/30 p-1">
        {(["summary", "picks", "theses", "journal"] as const).map((item) => (
          <button key={item} onClick={() => setTab(item)} className={`rounded-md px-4 py-1.5 text-sm capitalize ${tab === item ? "bg-card text-foreground shadow-sm" : "text-muted-foreground"}`}>
            {item}
          </button>
        ))}
      </div>

      {loading && <p className="text-sm text-muted-foreground">Loading research workspace…</p>}
      {(summary.isError || theses.isError || journal.isError || picks.isError) && (
        <Card><p className="text-sm text-negative">Unable to load research data. {errorDetail(summary.error || theses.error || journal.error || picks.error)}</p></Card>
      )}

      {tab === "summary" && summary.data && (
        <div className="space-y-5">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <Card><p className="text-xs text-muted-foreground">Active theses</p><p className="mt-1 text-2xl font-semibold">{summary.data.theses.active}</p></Card>
            <Card><p className="text-xs text-muted-foreground">Events this week</p><p className="mt-1 text-2xl font-semibold">{summary.data.theses.events}</p></Card>
            <Card><p className="text-xs text-muted-foreground">Alerts this week</p><p className="mt-1 text-2xl font-semibold">{summary.data.alerts.count}</p></Card>
            <Card><p className="text-xs text-muted-foreground">D5 calibration</p><p className="mt-1 text-2xl font-semibold">{summary.data.ai.d5_brier_score ?? "—"}</p><p className="text-xs text-muted-foreground">n={summary.data.ai.calibration_sample_size}</p></Card>
            <Card><p className="text-xs text-muted-foreground">Daily candidates</p><p className="mt-1 text-2xl font-semibold">{picks.data?.runs.reduce((total, run) => total + run.candidate_count, 0) ?? 0}</p><button onClick={() => setTab("picks")} className="text-xs text-primary hover:underline">Review ranking</button></Card>
          </div>
          <div className="grid gap-5 lg:grid-cols-2">
            <Card>
              <h2 className="mb-3 flex items-center gap-2 font-medium"><BookOpen className="h-4 w-4 text-primary" /> Recent thesis events</h2>
              <div className="space-y-3">
                {summary.data.theses.recent.length ? summary.data.theses.recent.map((event: any) => (
                  <div key={`${event.thesis_id}-${event.occurred_at}`} className="border-l-2 border-primary/40 pl-3">
                    <p className="text-sm">{event.title}</p><p className="text-xs text-muted-foreground">{event.type} · {new Date(event.occurred_at).toLocaleString()}</p>
                  </div>
                )) : <p className="text-sm text-muted-foreground">No new events this week.</p>}
              </div>
            </Card>
            <Card>
              <h2 className="mb-3 flex items-center gap-2 font-medium"><CalendarClock className="h-4 w-4 text-warning" /> Pending reviews</h2>
              <div className="space-y-2">
                {summary.data.pending.thesis_reviews.map((item: any) => <button key={item.id} onClick={() => { const row = theses.data?.find((t) => t.id === item.id); if (row) { setSelected(row); setTab("theses"); } }} className="block w-full rounded border border-border p-2 text-left text-sm hover:bg-secondary/30">{item.market}:{item.symbol} · {item.title}<span className="block text-xs text-negative">Due {item.review_date}</span></button>)}
                {!summary.data.pending.thesis_reviews.length && <p className="text-sm text-muted-foreground">No overdue reviews.</p>}
              </div>
              {!!summary.data.pending.watch_triggers?.length && <div className="mt-4 border-t border-border pt-3"><h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-warning">Triggered guardrails</h3><div className="space-y-2">{summary.data.pending.watch_triggers.map((item: any) => <button key={`${item.thesis_id}-${item.occurred_at}`} onClick={() => { const row = theses.data?.find((thesis) => thesis.id === item.thesis_id); if (row) { setSelected(row); setTab("theses"); } }} className="block w-full rounded border border-warning/40 bg-warning/5 p-2 text-left text-sm hover:bg-warning/10"><span className="font-medium">{item.condition?.label || item.title}</span><span className="block text-xs text-muted-foreground">Observed {item.observed_value ?? "—"} · {new Date(item.occurred_at).toLocaleString()}</span></button>)}</div></div>}
            </Card>
          </div>
        </div>
      )}

      {tab === "picks" && picks.data && (
        <div className="space-y-5">
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div><h2 className="font-medium">Daily AI research candidates</h2><p className="mt-1 text-xs text-muted-foreground">Ranks recent evidence-backed bullish reports. Every candidate enters the D1/D5/D20 decision journal.</p></div>
              <div className="flex gap-2">{(["TW", "US"] as const).map((market) => <button key={market} onClick={() => generatePicks.mutate(market)} disabled={generatePicks.isPending} className="rounded border border-border px-3 py-2 text-xs hover:bg-secondary/30 disabled:opacity-50">Generate {market}</button>)}</div>
            </div>
            {generatePicks.isError && <p className="mt-3 text-sm text-warning">{errorDetail(generatePicks.error)}</p>}
            <p className="mt-3 text-xs text-muted-foreground">{picks.data.disclaimer}</p>
          </Card>
          {picks.data.runs.map((run) => (
            <div key={run.id} className="space-y-3">
              <div className="flex items-end justify-between gap-3"><div><h2 className="font-medium">{run.market} · {run.run_date}</h2><p className="text-xs text-muted-foreground">{run.methodology_version} · generated {new Date(run.generated_at).toLocaleString()}</p></div><span className="text-xs text-muted-foreground">{run.candidate_count} candidates</span></div>
              <div className="grid gap-3 lg:grid-cols-2">{run.candidates.map((candidate) => <Card key={`${run.id}-${candidate.symbol}`}>
                <div className="flex items-start justify-between gap-3"><div><p className="text-xs text-muted-foreground">Rank #{candidate.rank}</p><h3 className="text-lg font-semibold">{candidate.market}:{candidate.symbol}</h3></div><div className="text-right"><p className="text-lg font-semibold text-primary">{candidate.score.toFixed(1)}</p><p className="text-xs text-muted-foreground">confidence {(candidate.confidence * 100).toFixed(0)}%</p></div></div>
                <p className="mt-3 text-sm text-muted-foreground">{candidate.rationale}</p>
                <div className="mt-3 flex flex-wrap gap-2 text-xs"><span className="rounded bg-secondary px-2 py-1">report quality {(candidate.report_quality * 100).toFixed(0)}%</span><span className="rounded bg-secondary px-2 py-1">{candidate.quality_details?.band || "unknown"}</span><span className="rounded bg-secondary px-2 py-1">{candidate.evidence.length} evidence refs</span></div>
              </Card>)}</div>
            </div>
          ))}
          {!picks.data.runs.length && <Card><p className="text-sm text-muted-foreground">No daily run yet. Generate evidence-backed AI reports with a bullish conclusion and at least 70% quality, then create a ranking.</p></Card>}
        </div>
      )}

      {tab === "theses" && (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.8fr)]">
          <div className="space-y-3">
            {theses.data?.map((thesis) => (
              <button key={thesis.id} onClick={() => setSelected(thesis)} className={`w-full rounded-lg border bg-card p-4 text-left ${selected?.id === thesis.id ? "border-primary" : "border-border"}`}>
                <div className="flex items-center justify-between gap-3"><h2 className="font-medium">{thesis.market}:{thesis.symbol} · {thesis.title}</h2><span className="rounded-full bg-secondary px-2 py-0.5 text-xs">{thesis.status}</span></div>
                <p className="mt-2 line-clamp-2 text-sm text-muted-foreground">{thesis.core_case}</p>
                <p className="mt-2 text-xs text-muted-foreground">Review {thesis.review_date || "not scheduled"}</p>
              </button>
            ))}
            {!theses.data?.length && <Card><p className="text-sm text-muted-foreground">Create your first thesis to turn research into a reviewable decision record.</p></Card>}
          </div>
          <Card>
            {selected ? <div className="space-y-5">
              <div><h2 className="font-medium">{selected.title}</h2><p className="mt-1 text-sm text-muted-foreground">{selected.core_case}</p></div>
              {!!selected.watch_conditions.length && <div className="rounded-md border border-border bg-secondary/15 p-3"><h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Watch conditions</h3><div className="space-y-1.5">{selected.watch_conditions.map((item, index) => typeof item === "string" ? <p key={`${item}-${index}`} className="text-sm">{item}</p> : <p key={item.id || `${item.metric}-${index}`} className="text-sm"><span className="font-medium">{item.label}</span><span className="ml-2 text-xs text-muted-foreground">{item.metric} {item.operator} {item.threshold}</span></p>)}</div></div>}
              <form onSubmit={(event) => { event.preventDefault(); submitReview.mutate(); }} className="space-y-3 border-t border-border pt-4">
                <h3 className="text-sm font-medium">Record review</h3>
                <select value={review.conclusion} onChange={(e) => setReview({ ...review, conclusion: e.target.value })} className="w-full rounded border border-border bg-background px-3 py-2 text-sm"><option value="unchanged">Unchanged</option><option value="strengthened">Strengthened</option><option value="weakened">Weakened</option><option value="invalidated">Invalidated</option></select>
                <textarea required value={review.notes} onChange={(e) => setReview({ ...review, notes: e.target.value })} placeholder="What changed and which evidence supports it?" className="min-h-24 w-full rounded border border-border bg-background px-3 py-2 text-sm" />
                <input type="date" value={review.next_review_date} onChange={(e) => setReview({ ...review, next_review_date: e.target.value })} className="w-full rounded border border-border bg-background px-3 py-2 text-sm" />
                <button disabled={submitReview.isPending} className="rounded bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50">Save review</button>
              </form>
              <div className="border-t border-border pt-4"><h3 className="mb-3 text-sm font-medium">Timeline</h3><div className="space-y-3">{timeline.data?.map((event) => <div key={event.id} className={`border-l-2 pl-3 ${event.event_type === "watch_condition_triggered" ? "border-warning bg-warning/5 py-2 pr-2" : "border-border"}`}><p className="text-sm">{event.title}</p><p className="text-xs text-muted-foreground">{event.event_type} · {event.source || "user"} · {new Date(event.occurred_at).toLocaleString()}</p>{event.event_type === "watch_condition_triggered" && typeof event.details.observed_value === "number" && <p className="mt-1 text-xs font-medium text-warning">Observed value: {event.details.observed_value}</p>}</div>)}{timeline.isLoading && <p className="text-xs text-muted-foreground">Loading timeline…</p>}</div></div>
            </div> : <p className="text-sm text-muted-foreground">Select a thesis to review its evidence timeline.</p>}
          </Card>
        </div>
      )}

      {tab === "journal" && journal.data && (
        <div className="space-y-5">
          <div className="grid gap-3 sm:grid-cols-3">{(["d1", "d5", "d20"] as const).map((h) => <Card key={h}><p className="text-xs uppercase text-muted-foreground">{h}</p><p className="mt-1 text-lg font-semibold">{pct(journal.data.summary.horizons[h].average_net_return_pct)}</p><p className="text-xs text-muted-foreground">Win rate {pct(journal.data.summary.horizons[h].win_rate_pct)} · n={journal.data.summary.horizons[h].sample_size}</p></Card>)}</div>
          <div className="overflow-x-auto rounded-lg border border-border"><table className="w-full text-sm"><thead className="bg-secondary/30 text-left text-xs text-muted-foreground"><tr><th className="p-3">Decision</th><th className="p-3">Confidence</th><th className="p-3">D1 net</th><th className="p-3">D5 net</th><th className="p-3">D20 net</th><th className="p-3">Max drawdown</th><th className="p-3">Status</th></tr></thead><tbody>{journal.data.entries.map((row) => <tr key={row.id} className="border-t border-border"><td className="p-3"><span className="font-medium">{row.market}:{row.symbol}</span><span className="block text-xs text-muted-foreground">{row.source_type}</span></td><td className="p-3">{row.confidence == null ? "—" : `${(row.confidence * 100).toFixed(0)}%`}</td>{(["d1", "d5", "d20"] as const).map((h) => <td key={h} className={`p-3 ${(row.outcomes[h]?.net_return_pct ?? 0) >= 0 ? "text-positive" : "text-negative"}`}>{pct(row.outcomes[h]?.net_return_pct)}</td>)}<td className="p-3 text-negative">{pct(row.max_drawdown_pct)}</td><td className="p-3">{row.status}<span className="block text-xs text-muted-foreground">n={row.observations}</span></td></tr>)}</tbody></table></div>
        </div>
      )}

      {showCreate && <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4"><form onSubmit={createThesis} className="max-h-[90vh] w-full max-w-2xl space-y-4 overflow-y-auto rounded-lg border border-border bg-card p-6"><div className="flex items-center justify-between"><h2 className="font-semibold">New investment thesis</h2><button type="button" onClick={() => setShowCreate(false)} className="text-muted-foreground">×</button></div><div className="grid grid-cols-[100px_1fr] gap-3"><select aria-label="Market" value={form.market} onChange={(e) => setForm({ ...form, market: e.target.value, watch_conditions: e.target.value === "TW" ? form.watch_conditions : [] })} className="rounded border border-border bg-background px-3 py-2"><option>TW</option><option>US</option></select><input required pattern="[A-Za-z0-9.\-]+" value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })} placeholder="Symbol" className="rounded border border-border bg-background px-3 py-2" /></div><input required value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="Thesis title" className="w-full rounded border border-border bg-background px-3 py-2" /><textarea required minLength={1} value={form.core_case} onChange={(e) => setForm({ ...form, core_case: e.target.value })} placeholder="Core case, catalysts, risks and invalidation conditions" className="min-h-32 w-full rounded border border-border bg-background px-3 py-2" />{form.market === "TW" && <fieldset className="space-y-3 rounded-md border border-border p-3"><legend className="px-1 text-sm font-medium">Automated watch conditions</legend><p className="text-xs text-muted-foreground">Trigger the thesis timeline when archived TW revenue or institutional-flow data crosses a guardrail.</p><div className="grid gap-2 sm:grid-cols-2"><input aria-label="Condition label" value={condition.label} onChange={(e) => setCondition({ ...condition, label: e.target.value })} placeholder="e.g. Revenue growth below 10%" className="rounded border border-border bg-background px-3 py-2 text-sm sm:col-span-2" /><select aria-label="Watch metric" value={condition.metric} onChange={(e) => setCondition({ ...condition, metric: e.target.value })} className="rounded border border-border bg-background px-3 py-2 text-sm"><option value="revenue_yoy_pct">Revenue YoY %</option><option value="revenue_mom_pct">Revenue MoM %</option><option value="foreign_net">Foreign investor net flow</option></select><div className="grid grid-cols-[1fr_1.2fr] gap-2"><select aria-label="Comparison operator" value={condition.operator} onChange={(e) => setCondition({ ...condition, operator: e.target.value })} className="rounded border border-border bg-background px-2 py-2 text-sm"><option value="lt">below</option><option value="lte">at or below</option><option value="gt">above</option><option value="gte">at or above</option><option value="eq">equals</option></select><input aria-label="Condition threshold" type="number" step="any" value={condition.threshold} onChange={(e) => setCondition({ ...condition, threshold: e.target.value })} placeholder="Threshold" className="rounded border border-border bg-background px-3 py-2 text-sm" /></div></div><button type="button" onClick={addWatchCondition} disabled={!condition.label.trim() || condition.threshold.trim() === ""} className="rounded border border-border px-3 py-1.5 text-xs disabled:opacity-40">Add condition</button>{!!form.watch_conditions.length && <div className="space-y-2">{form.watch_conditions.map((item, index) => <div key={`${item.metric}-${index}`} className="flex items-center justify-between rounded bg-secondary/30 px-3 py-2 text-xs"><span><strong>{item.label}</strong> · {item.metric} {item.operator} {item.threshold}</span><button type="button" aria-label={`Remove ${item.label}`} onClick={() => setForm({ ...form, watch_conditions: form.watch_conditions.filter((_, itemIndex) => itemIndex !== index) })} className="ml-3 text-muted-foreground hover:text-negative">×</button></div>)}</div>}</fieldset>}<label className="block text-xs text-muted-foreground">Next review date<input type="date" value={form.review_date} onChange={(e) => setForm({ ...form, review_date: e.target.value })} className="mt-1 w-full rounded border border-border bg-background px-3 py-2 text-sm" /></label>{create.isError && <p className="text-sm text-negative">{errorDetail(create.error)}</p>}<div className="flex justify-end gap-2"><button type="button" onClick={() => setShowCreate(false)} className="rounded border border-border px-3 py-2 text-sm">Cancel</button><button disabled={create.isPending} className="rounded bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50">Create thesis</button></div></form></div>}
      {showFeedback && <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4"><form onSubmit={(event) => { event.preventDefault(); submitFeedback.mutate(); }} className="w-full max-w-lg space-y-4 rounded-lg border border-border bg-card p-6"><div className="flex items-center justify-between"><div><h2 className="font-semibold">Report a data-quality issue</h2><p className="mt-1 text-xs text-muted-foreground">Include the endpoint and visible timestamp when possible.</p></div><button type="button" onClick={() => setShowFeedback(false)} className="text-muted-foreground">×</button></div><div className="grid grid-cols-2 gap-3"><select value={feedback.market} onChange={(e) => setFeedback({ ...feedback, market: e.target.value })} className="rounded border border-border bg-background px-3 py-2 text-sm"><option>TW</option><option>US</option><option>GLOBAL</option><option>CRYPTO</option></select><input value={feedback.symbol} onChange={(e) => setFeedback({ ...feedback, symbol: e.target.value })} placeholder="Symbol (optional)" className="rounded border border-border bg-background px-3 py-2 text-sm" /></div><select value={feedback.category} onChange={(e) => setFeedback({ ...feedback, category: e.target.value })} className="w-full rounded border border-border bg-background px-3 py-2 text-sm"><option value="stale">Stale timestamp</option><option value="missing">Missing data</option><option value="conflict">Source conflict</option><option value="incorrect">Incorrect value</option><option value="other">Other</option></select><input value={feedback.endpoint} onChange={(e) => setFeedback({ ...feedback, endpoint: e.target.value })} placeholder="Endpoint or page (optional)" className="w-full rounded border border-border bg-background px-3 py-2 text-sm" /><textarea required minLength={5} value={feedback.description} onChange={(e) => setFeedback({ ...feedback, description: e.target.value })} placeholder="Describe what appears wrong and what you expected" className="min-h-28 w-full rounded border border-border bg-background px-3 py-2 text-sm" />{submitFeedback.isError && <p className="text-sm text-negative">{errorDetail(submitFeedback.error)}</p>}{submitFeedback.isSuccess && <p className="text-sm text-positive">Feedback recorded.</p>}<div className="flex justify-end gap-2"><button type="button" onClick={() => setShowFeedback(false)} className="rounded border border-border px-3 py-2 text-sm">Cancel</button><button disabled={submitFeedback.isPending} className="rounded bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50">Submit issue</button></div></form></div>}
    </div>
  );
}
