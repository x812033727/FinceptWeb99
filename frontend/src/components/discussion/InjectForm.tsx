/**
 * User-injection form (PR #211, extended for B4 插話/追問). Extracted
 * from `pages/DiscussionPage.tsx` (PR-8 巨石頁拆分) — rendered in the
 * desktop config bar AND the mobile inject Sheet (and, for the B4
 * modes, inside the TranscriptPane), so the draft state and mutations
 * stay in the page.
 *
 * Three modes:
 *   - "between"  (default): classic between-rounds inject — drops a
 *     `user_input` turn the NEXT round's personas react to. Submits
 *     via `injectMut` (POST /inject). No persona select — the message
 *     is addressed to the whole panel.
 *   - "running": B4 mid-round interjection — the question is queued
 *     and answered by the assigned persona at the next turn boundary.
 *     Submits via `interjectMut` (POST /interject); optional persona
 *     select (blank = moderator assigns).
 *   - "followup": B4 post-conclusion 追問 — one bounded follow-up
 *     turn on a concluded discussion. Same endpoint + persona select.
 */
import { useTranslation } from "react-i18next";
import type { InterjectResponse } from "@/components/discussion/_api";

export type InjectMode = "between" | "running" | "followup";

export interface InjectFormProps {
  mode?: InjectMode;
  injectDraft: string;
  setInjectDraft: (v: string) => void;
  injectMut: {
    mutate: (content: string) => void;
    isPending: boolean;
    isError: boolean;
    error: unknown;
  };
  /** B4: interject mutation — required for the "running" / "followup"
   *  modes; unused in "between". */
  interjectMut?: {
    mutate: (args: { question: string; target_persona?: string }) => void;
    isPending: boolean;
    isError: boolean;
    isSuccess: boolean;
    error: unknown;
    data?: InterjectResponse;
  };
  /** Roster for the optional target-persona select (B4 modes). */
  personaIds?: string[];
  personaName?: (id: string) => string;
  interjectTarget?: string;
  setInterjectTarget?: (v: string) => void;
  setInjectSheetOpen: (open: boolean) => void;
}

export function InjectForm({
  mode = "between",
  injectDraft,
  setInjectDraft,
  injectMut,
  interjectMut,
  personaIds = [],
  personaName,
  interjectTarget = "",
  setInterjectTarget,
  setInjectSheetOpen,
}: InjectFormProps) {
  const { t } = useTranslation();
  const isInterject = mode !== "between" && !!interjectMut;
  const activeMut = isInterject ? interjectMut! : injectMut;

  const labelKey =
    mode === "running"
      ? "discussion.interject_label"
      : mode === "followup"
        ? "discussion.followup_label"
        : "discussion.inject_label";
  const placeholderKey = isInterject
    ? "discussion.interject_placeholder"
    : "discussion.inject_placeholder";
  const sendKey = isInterject
    ? "discussion.interject_send"
    : "discussion.inject_send";

  function submit() {
    const trimmed = injectDraft.trim();
    if (!trimmed || activeMut.isPending) return;
    if (isInterject) {
      interjectMut!.mutate({
        question: trimmed,
        target_persona: interjectTarget || undefined,
      });
    } else {
      injectMut.mutate(trimmed);
    }
    setInjectSheetOpen(false);
  }

  return (
    <div className="border border-border rounded-md p-2 bg-card/40 space-y-1.5">
      <label className="text-meta text-muted-foreground">
        {t(labelKey)}
      </label>
      <textarea
        value={injectDraft}
        onChange={(e) => setInjectDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            submit();
          }
        }}
        rows={2}
        maxLength={2000}
        placeholder={t(placeholderKey)}
        className="w-full resize-none bg-card border border-border rounded px-2 py-1 text-xs text-foreground focus:outline-none focus:border-primary/50"
      />
      {isInterject && (
        <select
          value={interjectTarget}
          onChange={(e) => setInterjectTarget?.(e.target.value)}
          aria-label={t("discussion.interject_persona_label")}
          className="w-full bg-card border border-border rounded px-2 py-1 text-xs text-foreground focus:outline-none focus:border-primary/50"
        >
          <option value="">{t("discussion.interject_persona_auto")}</option>
          {personaIds.map((id) => (
            <option key={id} value={id}>
              {personaName ? personaName(id) : id}
            </option>
          ))}
        </select>
      )}
      <div className="flex items-center justify-between">
        <span className="text-micro text-muted-foreground">
          {injectDraft.length}/2000
          <span className="ml-2 opacity-60">
            {t("discussion.inject_shortcut_hint")}
          </span>
        </span>
        <button
          type="button"
          onClick={submit}
          disabled={!injectDraft.trim() || activeMut.isPending}
          className="px-2.5 py-1 rounded text-meta border border-warning/30 text-warning hover:bg-warning/10 transition-colors disabled:opacity-40 min-h-[32px]"
        >
          {activeMut.isPending ? t("common.saving") : t(sendKey)}
        </button>
      </div>
      {mode === "running" &&
        interjectMut?.isSuccess &&
        interjectMut.data?.status === "queued" && (
          <p className="text-micro text-success">
            {t("discussion.interject_queued")}
          </p>
        )}
      {activeMut.isError && (
        <p className="text-micro text-danger">
          {(activeMut.error as Error)?.message ?? t("common.error")}
        </p>
      )}
    </div>
  );
}
