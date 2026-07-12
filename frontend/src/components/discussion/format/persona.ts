/**
 * Persona-name / avatar-glyph lookup hooks for the discussion
 * subsystem. React hooks (call `useTranslation`) but emit only strings
 * — no JSX, so this stays a `.ts` module.
 */
import { useTranslation } from "react-i18next";
import type { AgentInfo } from "@/types/discussion";

// ── persona-name lookup ────────────────────────────────────────────

export function usePersonaName(agents: AgentInfo[]) {
  const { t, i18n } = useTranslation();
  return (id: string) => {
    // The pseudo-persona for between-rounds user injections
    // (PR #211). Not in the agents list — render as a localised
    // "discussion owner" label so the transcript reads naturally.
    if (id === "_user") return t("discussion.user_persona_name");
    const a = agents.find((x) => x.id === id);
    if (!a) return id;
    const key = `personas.agents.${id}.name`;
    return i18n.exists(key) ? t(key) : a.name;
  };
}

// PR-C: per-persona avatar glyph (1-2 char) from
// `personas.agents.<id>.short`. Returns undefined when no explicit
// short label exists, leaving the avatar to derive the initial from
// the localised name (single CJK char or first ASCII letter). The
// _user pseudo-persona uses a dedicated initial.
export function usePersonaShort() {
  const { t, i18n } = useTranslation();
  return (id: string): string | undefined => {
    if (id === "_user") return t("discussion.user_persona_short", { defaultValue: "✎" });
    const key = `personas.agents.${id}.short`;
    return i18n.exists(key) ? t(key) : undefined;
  };
}
