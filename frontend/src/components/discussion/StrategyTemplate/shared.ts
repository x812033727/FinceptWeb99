/**
 * Shared form-state type for the StrategyTemplate sections.
 *
 * `StrategyFormState` is the create/edit form's local state shape. It
 * is used both by the entry `StrategyTemplateCard` (which owns the
 * state + `DEFAULT_FORM` seed) and by the `StrategyFormBlock` section
 * in `form.tsx`, so it lives here rather than in either to avoid a
 * cross-section import.
 */
import type { DiscussionMarket } from "@/types/discussion";

export interface StrategyFormState {
  name: string;
  description: string;
  topic: string;
  rules: string;
  market: DiscussionMarket;
  personaIdsCsv: string;
  defaultRounds: number;
  defaultConcurrency: number;
  defaultAutoPostMortem: boolean;
  autoScheduleEnabled: boolean;
  autoScheduleCadenceHours: number;
  autoScheduleAnchorOffsetDays: number;
  autoScheduleTradingDaysCount: number;
}
