import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { AgentInfo, AutoRunConfig, DiscussionMarket } from "@/types/discussion";
import { fetchAutoRunConfig, saveAutoRunConfig } from "./_helpers";

// Sits at the top of the sidebar. Lets each user opt themselves into
// the daily 20:00 UTC = 04:00 台北 scheduler and pick their own topic
// / rules / persona roster.
export function AutoRunConfigCard({
  agents,
  collapsed,
  onToggleCollapse,
  personaName,
  hideHeader,
}: {
  agents: AgentInfo[];
  collapsed: boolean;
  onToggleCollapse: () => void;
  personaName: (id: string) => string;
  /** When true, omit the inline header row (the popover trigger
   * already labels itself). The body still respects `collapsed`
   * so callers using the popover should pass `collapsed={false}`. */
  hideHeader?: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const { data: cfg, isLoading, isError } = useQuery<AutoRunConfig>({
    queryKey: ["discussion-auto-run-config"],
    queryFn: fetchAutoRunConfig,
  });

  const [enabled, setEnabled] = useState(false);
  const [topic, setTopic] = useState("");
  const [rules, setRules] = useState("");
  const [personaIds, setPersonaIds] = useState<string[]>([]);
  const [market, setMarket] = useState<DiscussionMarket>("TW");
  const [sendEmail, setSendEmail] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showSaved, setShowSaved] = useState(false);

  // Hydrate form fields once when the server config arrives. A ref guards
  // against re-hydrating on every refetch (which would clobber whatever
  // the user just typed).
  const hydratedRef = useRef(false);
  useEffect(() => {
    if (!cfg || hydratedRef.current) return;
    hydratedRef.current = true;
    setEnabled(cfg.enabled);
    setTopic(cfg.topic);
    setRules(cfg.rules);
    setPersonaIds(cfg.persona_ids);
    setMarket(cfg.market ?? "TW");
    setSendEmail(!!cfg.send_email);
  }, [cfg]);

  const saveMut = useMutation({
    mutationFn: saveAutoRunConfig,
    onSuccess: (row) => {
      queryClient.setQueryData(["discussion-auto-run-config"], row);
      setShowSaved(true);
      setError(null);
    },
  });

  useEffect(() => {
    if (!showSaved) return;
    const timer = window.setTimeout(() => setShowSaved(false), 3000);
    return () => window.clearTimeout(timer);
  }, [showSaved]);

  function togglePersona(id: string) {
    setPersonaIds((prev) =>
      prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id],
    );
  }

  function handleSave() {
    if (personaIds.length < 2 || personaIds.length > 8) {
      setError(t("discussion.auto_run_validation_personas"));
      return;
    }
    if (!topic.trim()) {
      setError(t("discussion.auto_run_validation_topic"));
      return;
    }
    if (!rules.trim()) {
      setError(t("discussion.auto_run_validation_rules"));
      return;
    }
    setError(null);
    saveMut.mutate({
      enabled,
      persona_ids: personaIds,
      topic: topic.trim(),
      rules: rules.trim(),
      market,
      send_email: sendEmail,
    });
  }

  return (
    <div className="border border-border rounded-md p-2 bg-card/40">
      {hideHeader ? null : (
        <button
          type="button"
          onClick={onToggleCollapse}
          className="flex items-center gap-1.5 w-full text-left text-xs font-medium text-foreground hover:text-primary transition-colors"
          aria-expanded={!collapsed}
        >
          <span className="text-[9px] text-muted-foreground w-2.5 inline-block">
            {collapsed ? "▶" : "▼"}
          </span>
          {t("discussion.auto_run_title")}
          {cfg?.enabled && (
            <span className="ml-auto text-micro px-1.5 py-0.5 rounded bg-success/10 text-success border border-success/30">
              ON
            </span>
          )}
        </button>
      )}

      {!collapsed && (
        <div className="mt-2 space-y-2">
          <p className="text-micro text-muted-foreground leading-relaxed">
            {t("discussion.auto_run_subtitle")}
          </p>

          {isLoading ? (
            <p className="text-micro text-muted-foreground animate-pulse">
              …
            </p>
          ) : isError ? (
            <p className="text-micro text-danger">
              {t("discussion.auto_run_load_failed")}
            </p>
          ) : (
            <>
              <label className="flex items-center gap-2 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => setEnabled(e.target.checked)}
                  className="accent-primary"
                />
                <span>{t("discussion.auto_run_enabled")}</span>
              </label>

              <label className="flex items-start gap-2 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={sendEmail}
                  onChange={(e) => setSendEmail(e.target.checked)}
                  className="accent-primary mt-0.5"
                />
                <span className="flex flex-col">
                  <span>{t("discussion.auto_run_send_email")}</span>
                  <span className="text-micro text-muted-foreground">
                    {t("discussion.auto_run_send_email_hint")}
                  </span>
                </span>
              </label>

              <div className="flex items-center gap-2 text-meta">
                <span className="text-muted-foreground">
                  {t("discussion.market_label")}
                </span>
                <select
                  value={market}
                  onChange={(e) => setMarket(e.target.value as DiscussionMarket)}
                  className="bg-card border border-border rounded px-1.5 py-0.5 text-foreground focus:outline-none focus:border-primary/50"
                >
                  <option value="TW">TW</option>
                  <option value="US">US</option>
                  <option value="GLOBAL">GLOBAL</option>
                </select>
              </div>

              <div>
                <label className="text-meta text-muted-foreground">
                  {t("discussion.auto_run_topic_label")}
                </label>
                <textarea
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  rows={2}
                  maxLength={500}
                  placeholder={t("discussion.auto_run_topic_placeholder")}
                  className="w-full mt-0.5 resize-none bg-card border border-border rounded px-2 py-1 text-meta text-foreground focus:outline-none focus:border-primary/50"
                />
              </div>

              <div>
                <label className="text-meta text-muted-foreground">
                  {t("discussion.auto_run_rules_label")}
                </label>
                <textarea
                  value={rules}
                  onChange={(e) => setRules(e.target.value)}
                  rows={4}
                  maxLength={2000}
                  placeholder={t("discussion.auto_run_rules_placeholder")}
                  className="w-full mt-0.5 resize-none bg-card border border-border rounded px-2 py-1 text-meta text-foreground font-mono focus:outline-none focus:border-primary/50"
                />
              </div>

              <div>
                <label className="text-meta text-muted-foreground">
                  {t("discussion.auto_run_personas_label")} ({personaIds.length})
                </label>
                <div className="mt-1 flex flex-wrap gap-1">
                  {agents.map((a) => {
                    const selected = personaIds.includes(a.id);
                    return (
                      <button
                        key={a.id}
                        type="button"
                        onClick={() => togglePersona(a.id)}
                        className={`px-1.5 py-0.5 rounded text-micro border transition-colors ${
                          selected
                            ? "border-primary bg-primary/15 text-primary"
                            : "border-border bg-card text-muted-foreground hover:border-primary/40"
                        }`}
                      >
                        {personaName(a.id)}
                      </button>
                    );
                  })}
                </div>
              </div>

              {error && (
                <p className="text-micro text-danger">{error}</p>
              )}

              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleSave}
                  disabled={saveMut.isPending}
                  className="px-2.5 py-1 rounded text-meta bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
                >
                  {saveMut.isPending
                    ? t("common.saving")
                    : t("discussion.auto_run_save")}
                </button>
                {showSaved && (
                  <span className="text-micro text-success">
                    ✓ {t("discussion.auto_run_saved")}
                  </span>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
