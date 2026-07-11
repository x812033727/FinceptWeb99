/**
 * Between-rounds user-injection form (PR #211). Extracted verbatim
 * from `pages/DiscussionPage.tsx` (PR-8 巨石頁拆分) — rendered in the
 * desktop config bar AND the mobile inject Sheet, so the draft state
 * and mutation stay in the page.
 */
import { useTranslation } from "react-i18next";

export function InjectForm({
  injectDraft,
  setInjectDraft,
  injectMut,
  setInjectSheetOpen,
}: {
  injectDraft: string;
  setInjectDraft: (v: string) => void;
  injectMut: {
    mutate: (content: string) => void;
    isPending: boolean;
    isError: boolean;
    error: unknown;
  };
  setInjectSheetOpen: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="border border-border rounded-md p-2 bg-card/40 space-y-1.5">
      <label className="text-[11px] text-muted-foreground">
        {t("discussion.inject_label")}
      </label>
      <textarea
        value={injectDraft}
        onChange={(e) => setInjectDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            const trimmed = injectDraft.trim();
            if (trimmed && !injectMut.isPending) {
              injectMut.mutate(trimmed);
            }
          }
        }}
        rows={2}
        maxLength={2000}
        placeholder={t("discussion.inject_placeholder")}
        className="w-full resize-none bg-card border border-border rounded px-2 py-1 text-xs text-foreground focus:outline-none focus:border-primary/50"
      />
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-muted-foreground">
          {injectDraft.length}/2000
          <span className="ml-2 opacity-60">
            {t("discussion.inject_shortcut_hint")}
          </span>
        </span>
        <button
          type="button"
          onClick={() => {
            const trimmed = injectDraft.trim();
            if (trimmed && !injectMut.isPending) {
              injectMut.mutate(trimmed);
              setInjectSheetOpen(false);
            }
          }}
          disabled={!injectDraft.trim() || injectMut.isPending}
          className="px-2.5 py-1 rounded text-[11px] border border-warning/30 text-warning hover:bg-warning/10 transition-colors disabled:opacity-40 min-h-[32px]"
        >
          {injectMut.isPending ? t("common.saving") : t("discussion.inject_send")}
        </button>
      </div>
      {injectMut.isError && (
        <p className="text-[10px] text-danger">
          {(injectMut.error as Error)?.message ?? t("common.error")}
        </p>
      )}
    </div>
  );
}
