/**
 * PR-5a: Strategy Lifecycle Timeline.
 *
 * Single integrated chart that overlays four data sources so the
 * operator sees "what happened on this strategy" at a glance:
 *   - brier line (raw + calibrated)
 *   - hit-rate line on a secondary axis
 *   - red dots on metric points whose status_flags is non-empty
 *   - vertical event markers for version changes / sweep
 *     completions / maturity transitions / persona-status changes
 *
 * Hides itself entirely when both the metric series and event
 * list are empty (cold-start strategies) so the strategy card
 * doesn't grow noise.
 */
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  ComposedChart,
  Dot,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import api from "@/lib/api";
import { useCollapsible } from "@/hooks/useCollapsible";
import type {
  RegimeBand,
  RegimeBandsResponse,
  StrategyTimeline,
  TimelineEvent,
} from "./_helpers";


/** Regime → semi-transparent background tint. Stacked when overlap. */
function regimeColor(regime: RegimeBand["regime"]): string {
  switch (regime) {
    case "bull":     return "rgba(16, 185, 129, 0.08)";   // emerald
    case "bear":     return "rgba(239, 68, 68, 0.10)";    // red
    case "high_vol": return "rgba(245, 158, 11, 0.10)";   // amber
    case "low_vol":  return "rgba(59, 130, 246, 0.06)";   // blue
  }
}


/** Map an event kind+trigger to the marker colour. Stable so the
 * legend stays meaningful regardless of operator-tuned filters. */
function eventColor(ev: TimelineEvent): string {
  if (ev.kind === "version_change") {
    if (ev.trigger === "auto_promote") return "#10b981";   // emerald
    if (ev.trigger === "rollback") return "#ef4444";       // red
    if (ev.trigger === "calibration_approved") return "#a855f7"; // purple
    return "#6366f1";   // indigo (sweep_phase3 / manual_fit fallback)
  }
  if (ev.kind === "sweep_completed") {
    if (ev.fold_kind === "train") return "#94a3b8";  // slate
    if (ev.fold_kind === "test") return "#0ea5e9";   // sky
    return "#64748b";   // muted slate (production/other)
  }
  if (ev.kind === "maturity_change") return "#f59e0b";  // amber
  if (ev.kind === "persona_status_change") return "#fb7185";  // rose
  return "#94a3b8";
}


function FlaggedDot(props: any) {
  const { cx, cy, payload } = props;
  if (!payload || !Array.isArray(payload.status_flags)) return null;
  if (payload.status_flags.length === 0) return null;
  return (
    <Dot
      cx={cx} cy={cy} r={4}
      fill="hsl(var(--destructive))"
      stroke="hsl(var(--destructive))"
    />
  );
}


export function StrategyTimelineCard({
  strategyId,
  windowDays = 90,
  market,
}: {
  strategyId: string;
  windowDays?: number;
  /** PR-5c: when supplied, fetch regime bands for this market and
   * paint them as background tints behind the chart. Currently
   * only TW returns data; other markets get an empty band list. */
  market?: string;
}) {
  const { t } = useTranslation();
  // Default closed — the timeline is heavier than the sparkline
  // and we lazy-load the full payload on expand. Operators who
  // open it once persist their pref via the storage key.
  const { open, toggle } = useCollapsible(
    `strategy.${strategyId}.timeline`, false,
  );
  const [selectedEvent, setSelectedEvent] = useState<TimelineEvent | null>(null);
  const [regimesOn, setRegimesOn] = useState(true);

  const { data, isLoading } = useQuery({
    queryKey: ["strategy-timeline", strategyId, windowDays],
    enabled: open,
    queryFn: async () => {
      const { data } = await api.get<StrategyTimeline>(
        `/discussion/strategies/${strategyId}/timeline?days=${windowDays}`,
      );
      return data;
    },
    staleTime: 60_000,
  });

  // PR-5c: fetch regime bands when a market is supplied. Lazy
  // alongside the timeline; both gated by `open`.
  const { data: regimeData } = useQuery({
    queryKey: ["regime-bands", market, windowDays],
    enabled: open && !!market,
    queryFn: async () => {
      const { data } = await api.get<RegimeBandsResponse>(
        `/discussion/regime-bands?market=${market}&days=${windowDays}`,
      );
      return data;
    },
    staleTime: 5 * 60_000,
  });

  const series = useMemo(() => {
    if (!data) return [];
    return data.metrics.map((m) => ({
      date: m.date,
      brier: m.brier_30d,
      calibratedBrier: m.calibrated_brier_30d,
      hitRate: m.hit_rate_30d,
      status_flags: m.status_flags,
    }));
  }, [data]);

  const events = data?.events ?? [];
  const hasContent = series.length > 0 || events.length > 0;

  return (
    <div className="border-t border-border/50 mt-2 pt-2">
      <button
        type="button"
        onClick={toggle}
        className="text-[11px] text-muted-foreground hover:text-foreground
                   flex items-center gap-1"
      >
        <span className="w-3 inline-block">{open ? "▼" : "▶"}</span>
        {t("discussion.timeline.title", { days: windowDays })}
        {events.length > 0 && (
          <span className="ml-1 text-[10px] text-muted-foreground">
            · {events.length} {t("discussion.timeline.events_short")}
          </span>
        )}
      </button>

      {open && (
        <div className="mt-2 space-y-2">
          {isLoading && (
            <div className="text-[11px] text-muted-foreground animate-pulse">
              {t("common.loading")}
            </div>
          )}
          {!isLoading && !hasContent && (
            <div className="text-[11px] text-muted-foreground italic">
              {t("discussion.timeline.empty")}
            </div>
          )}
          {hasContent && (
            <>
              <ResponsiveContainer width="100%" height={180}>
                <ComposedChart data={series} margin={{ top: 4, right: 8, bottom: 16, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                  {/* PR-5c: regime bands paint as background
                      ReferenceAreas BEFORE the lines so they
                      appear underneath. Toggleable via the
                      `regimesOn` checkbox below the chart. */}
                  {regimesOn && (regimeData?.bands ?? []).map((b, i) => (
                    <ReferenceArea
                      key={`rb-${i}`}
                      x1={b.start.slice(0, 10)}
                      x2={b.end.slice(0, 10)}
                      yAxisId="brier"
                      fill={regimeColor(b.regime)}
                      stroke="none"
                      ifOverflow="extendDomain"
                    />
                  ))}
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    tickFormatter={(d: string) => d.slice(5)}
                  />
                  <YAxis
                    yAxisId="brier"
                    domain={[0, 1]}
                    tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    tickFormatter={(v: number) => v.toFixed(2)}
                    width={32}
                  />
                  <YAxis
                    yAxisId="hit"
                    orientation="right"
                    domain={[0, 1]}
                    tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
                    width={32}
                  />
                  <Tooltip
                    wrapperStyle={{ fontSize: 11 }}
                    contentStyle={{
                      backgroundColor: "hsl(var(--popover))",
                      borderColor: "hsl(var(--border))",
                    }}
                  />
                  <Line
                    yAxisId="brier" type="monotone" dataKey="brier"
                    stroke="#f59e0b" strokeWidth={2}
                    dot={<FlaggedDot />} activeDot={{ r: 4 }}
                    connectNulls={false}
                    name={t("discussion.timeline.brier") as string}
                  />
                  <Line
                    yAxisId="brier" type="monotone" dataKey="calibratedBrier"
                    stroke="#10b981" strokeWidth={1.5} strokeDasharray="3 3"
                    dot={false} activeDot={{ r: 4 }}
                    connectNulls={false}
                    name={t("discussion.timeline.calibrated_brier") as string}
                  />
                  <Line
                    yAxisId="hit" type="monotone" dataKey="hitRate"
                    stroke="#3b82f6" strokeWidth={1.5}
                    dot={false} activeDot={{ r: 4 }}
                    connectNulls={false}
                    name={t("discussion.timeline.hit_rate") as string}
                  />
                  {events.map((ev, i) => (
                    <ReferenceLine
                      key={`ev-${i}`}
                      x={ev.date.slice(0, 10)}
                      stroke={eventColor(ev)}
                      strokeDasharray="2 4"
                      strokeWidth={1}
                      yAxisId="brier"
                      label={undefined}
                    />
                  ))}
                </ComposedChart>
              </ResponsiveContainer>

              {market && (regimeData?.bands ?? []).length > 0 && (
                <label className="flex items-center gap-1 text-[10px] text-muted-foreground mt-1">
                  <input
                    type="checkbox"
                    checked={regimesOn}
                    onChange={(e) => setRegimesOn(e.target.checked)}
                    className="accent-emerald-500"
                  />
                  {t("discussion.timeline.regime_overlay")}
                </label>
              )}

              <EventLegend events={events} onSelect={setSelectedEvent} />
              {selectedEvent && (
                <EventDetail
                  event={selectedEvent}
                  onClose={() => setSelectedEvent(null)}
                />
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}


function EventLegend({
  events,
  onSelect,
}: {
  events: TimelineEvent[];
  onSelect: (ev: TimelineEvent) => void;
}) {
  const { t } = useTranslation();
  if (events.length === 0) return null;

  // Compact list under the chart so the operator sees what each
  // marker represents and can click for detail.
  return (
    <ul className="flex flex-wrap gap-1.5 text-[10px]">
      {events.slice().reverse().slice(0, 12).map((ev, i) => (
        <li key={`leg-${i}`}>
          <button
            type="button"
            onClick={() => onSelect(ev)}
            className="inline-flex items-center gap-1 px-1.5 py-0.5
                       rounded border border-border bg-background/60
                       hover:bg-background"
            title={ev.notes ?? undefined}
          >
            <span
              className="inline-block w-2 h-2 rounded-full"
              style={{ backgroundColor: eventColor(ev) }}
            />
            <span className="font-mono text-muted-foreground">
              {ev.date.slice(5, 10)}
            </span>
            <span className="text-foreground">
              {t(`discussion.timeline.event.${ev.kind}`)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}


function EventDetail({
  event,
  onClose,
}: {
  event: TimelineEvent;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="text-[11px] border border-border rounded p-2 bg-background/40">
      <div className="flex items-center justify-between mb-1">
        <span className="font-semibold">
          {t(`discussion.timeline.event.${event.kind}`)}{" "}
          <span className="text-muted-foreground font-normal">
            · {event.date.slice(0, 10)}
          </span>
        </span>
        <button
          type="button"
          onClick={onClose}
          className="text-[10px] text-muted-foreground hover:text-foreground"
        >
          {t("common.close")}
        </button>
      </div>
      <dl className="space-y-0.5 text-[10px]">
        {event.kind === "version_change" && (
          <>
            <div>
              <span className="text-muted-foreground">artifact:</span>{" "}
              <span className="font-mono">{event.artifact}</span>
            </div>
            <div>
              <span className="text-muted-foreground">version:</span>{" "}
              v{event.version_number} ({event.status})
            </div>
            {event.trigger && (
              <div>
                <span className="text-muted-foreground">trigger:</span>{" "}
                <span className="font-mono">{event.trigger}</span>
              </div>
            )}
            {event.notes && (
              <div className="text-muted-foreground italic mt-1">
                {event.notes}
              </div>
            )}
          </>
        )}
        {event.kind === "sweep_completed" && (
          <>
            <div>
              <span className="text-muted-foreground">fold_kind:</span>{" "}
              <span className="font-mono">{event.fold_kind}</span>
            </div>
            <div>
              <span className="text-muted-foreground">discussions:</span>{" "}
              {event.discussions_completed} ✓ /{" "}
              {event.discussions_failed} ✗
            </div>
            {event.anchor_date && (
              <div>
                <span className="text-muted-foreground">anchor:</span>{" "}
                {event.anchor_date}
              </div>
            )}
          </>
        )}
        {event.kind === "maturity_change" && (
          <div>
            <span className="font-mono">{event.from}</span>
            {" → "}
            <span className="font-mono">{event.to}</span>
          </div>
        )}
        {event.kind === "persona_status_change" && (
          <div>
            <span className="text-muted-foreground">non-active:</span>{" "}
            {event.non_active_count} (
            {(event.non_active_personas ?? []).join(", ")})
          </div>
        )}
      </dl>
    </div>
  );
}
