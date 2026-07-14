/**
 * Discussion config form (topic / rules / personas / market / backtest
 * anchor / save-as-defaults). Extracted verbatim from
 * `pages/DiscussionPage.tsx` (PR-8 巨石頁拆分) — all form state stays
 * in the page (the form renders in BOTH the desktop collapsible bar
 * and the mobile config Sheet, which must share state).
 */
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { AgentInfo, DiscussionMarket } from "@/types/discussion";
import type { CollapseState } from "@/components/discussion/_helpers";

export function DiscussionConfigForm({
  collapse,
  toggleCollapse,
  topic,
  setTopic,
  rules,
  setRules,
  topicDirty,
  rulesDirty,
  saveTopic,
  saveRules,
  agents,
  personaIds,
  togglePersona,
  personaName,
  market,
  setMarket,
  asOfDate,
  setAsOfDate,
  selectedId,
  isDraft,
  isStreaming,
  updateMut,
  saveAsDefaults,
}: {
  collapse: CollapseState;
  toggleCollapse: (key: keyof CollapseState) => void;
  topic: string;
  setTopic: (v: string) => void;
  rules: string;
  setRules: (v: string) => void;
  topicDirty: boolean;
  rulesDirty: boolean;
  saveTopic: () => void;
  saveRules: () => void;
  agents: AgentInfo[];
  personaIds: string[];
  togglePersona: (id: string) => void;
  personaName: (id: string) => string;
  market: DiscussionMarket;
  setMarket: (m: DiscussionMarket) => void;
  asOfDate: string;
  setAsOfDate: (v: string) => void;
  selectedId: string | null;
  isDraft: boolean;
  isStreaming: boolean;
  updateMut: { isPending: boolean };
  saveAsDefaults: () => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      <div>
        <div className="flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => toggleCollapse("topic")}
            className="flex items-center gap-1.5 text-xs font-medium text-foreground hover:text-primary transition-colors"
            aria-expanded={!collapse.topic}
          >
            <span className="text-[9px] text-muted-foreground w-2.5 inline-block">
              {collapse.topic ? "▶" : "▼"}
            </span>
            {t("discussion.topic_label")}
            {topicDirty && (
              <span className="ml-1 text-micro text-warning">
                {t("discussion.unsaved")}
              </span>
            )}
          </button>
          {selectedId && isDraft && (
            <button
              onClick={saveTopic}
              disabled={!topicDirty || updateMut.isPending || isStreaming}
              className="px-2 py-0.5 text-micro border border-border rounded hover:border-primary/40 transition-colors disabled:opacity-30"
            >
              {updateMut.isPending ? t("common.saving") : t("common.save")}
            </button>
          )}
        </div>
        {collapse.topic ? (
          topic && (
            <p className="mt-1 ml-4 text-[11px] text-muted-foreground line-clamp-1">
              {topic}
            </p>
          )
        ) : (
          <textarea
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            disabled={!isDraft || isStreaming}
            rows={2}
            maxLength={500}
            className="w-full mt-1 resize-none bg-card border border-border rounded-md px-3 py-2 text-sm text-foreground focus:outline-none focus:border-primary/50 disabled:opacity-60"
          />
        )}
      </div>

      <div>
        <div className="flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => toggleCollapse("rules")}
            className="flex items-center gap-1.5 text-xs font-medium text-foreground hover:text-primary transition-colors"
            aria-expanded={!collapse.rules}
          >
            <span className="text-[9px] text-muted-foreground w-2.5 inline-block">
              {collapse.rules ? "▶" : "▼"}
            </span>
            {t("discussion.rules_label")}
            {rulesDirty && (
              <span className="ml-1 text-micro text-warning">
                {t("discussion.unsaved")}
              </span>
            )}
          </button>
          {selectedId && isDraft && (
            <button
              onClick={saveRules}
              disabled={!rulesDirty || updateMut.isPending || isStreaming}
              className="px-2 py-0.5 text-micro border border-border rounded hover:border-primary/40 transition-colors disabled:opacity-30"
            >
              {updateMut.isPending ? t("common.saving") : t("common.save")}
            </button>
          )}
        </div>
        {collapse.rules ? (
          rules && (
            <p className="mt-1 ml-4 text-[11px] text-muted-foreground line-clamp-1">
              {rules.split("\n")[0]}
            </p>
          )
        ) : (
          <textarea
            value={rules}
            onChange={(e) => setRules(e.target.value)}
            disabled={!isDraft || isStreaming}
            rows={5}
            maxLength={2000}
            className="w-full mt-1 resize-none bg-card border border-border rounded-md px-3 py-2 text-xs text-foreground font-mono focus:outline-none focus:border-primary/50 disabled:opacity-60"
          />
        )}
      </div>

      <div>
        <button
          type="button"
          onClick={() => toggleCollapse("personas")}
          className="flex items-center gap-1.5 text-xs font-medium text-foreground hover:text-primary transition-colors"
          aria-expanded={!collapse.personas}
        >
          <span className="text-[9px] text-muted-foreground w-2.5 inline-block">
            {collapse.personas ? "▶" : "▼"}
          </span>
          {t("discussion.personas_label")} ({personaIds.length})
        </button>
        {!collapse.personas && (
          <div className="mt-1 flex flex-wrap gap-1.5">
            {agents.map((a) => {
              const selected = personaIds.includes(a.id);
              return (
                <button
                  key={a.id}
                  onClick={() => togglePersona(a.id)}
                  disabled={!isDraft || isStreaming}
                  className={cn(
                    "px-2 py-1 rounded text-[11px] border transition-colors disabled:opacity-60 min-h-[32px]",
                    selected
                      ? "border-primary bg-primary/15 text-primary"
                      : "border-border bg-card text-muted-foreground hover:border-primary/40"
                  )}
                >
                  {personaName(a.id)}
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="flex items-center gap-2 text-xs flex-wrap">
        <span className="text-muted-foreground">{t("discussion.market_label")}</span>
        <select
          value={market}
          onChange={(e) => setMarket(e.target.value as DiscussionMarket)}
          disabled={!isDraft || isStreaming}
          className="bg-card border border-border rounded px-2 py-1 text-foreground focus:outline-none focus:border-primary/50 disabled:opacity-60 min-h-[32px]"
        >
          <option value="TW">TW</option>
          <option value="US">US</option>
          <option value="GLOBAL">GLOBAL</option>
        </select>
        <span className="text-muted-foreground ml-2">
          {t("discussion.as_of_label")}
        </span>
        <input
          type="date"
          value={asOfDate}
          onChange={(e) => setAsOfDate(e.target.value)}
          disabled={!!selectedId || isStreaming}
          max={new Date().toISOString().slice(0, 10)}
          placeholder={t("discussion.as_of_placeholder")}
          className="bg-card border border-border rounded px-2 py-1 text-foreground focus:outline-none focus:border-primary/50 disabled:opacity-60 min-h-[32px]"
        />
        {asOfDate && (
          <span className="px-1.5 py-0.5 rounded text-micro border border-warning/30 bg-warning/10 text-warning">
            {t("discussion.backtest_badge")}
          </span>
        )}
      </div>

      <div className="flex items-center gap-2 pt-1 border-t border-border/40">
        <button
          type="button"
          onClick={saveAsDefaults}
          disabled={isStreaming}
          className="px-2 py-1 text-[11px] rounded border border-border text-muted-foreground hover:border-primary/40 hover:text-foreground transition-colors disabled:opacity-50 min-h-[28px]"
          title={t("discussion.save_as_defaults_hint")}
        >
          {t("discussion.save_as_defaults")}
        </button>
        <span className="text-micro text-muted-foreground">
          {t("discussion.save_as_defaults_hint")}
        </span>
      </div>
    </>
  );
}
