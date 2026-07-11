import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

export default tseslint.config(
  // Global ignores
  { ignores: ["dist/**", "node_modules/**"] },

  // Base TypeScript rules
  ...tseslint.configs.recommended,

  // React hooks rules
  {
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
    },
  },

  // React Refresh — flags modules that mix component exports with
  // non-component exports, which breaks Vite's HMR boundary detection
  // and forces a full reload on every edit.
  //
  // Skipped patterns:
  //   - test / setup / i18n: legitimate non-component exports.
  //   - components/ui/**: shadcn-ui convention exports variant fns
  //     (cva) alongside the component; not worth splitting per file.
  //   - components/admin/**: existing cards co-locate small pure
  //     helpers used by their own tests; refactoring is out of scope
  //     for HMR ergonomics work.
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: [
      "src/**/*.test.{ts,tsx}",
      "src/test/**",
      "src/i18n/**",
      "src/components/ui/**",
      "src/components/admin/**",
    ],
    plugins: { "react-refresh": reactRefresh },
    rules: {
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
    },
  },

  // Market/status colour tokens — PR-2 codemod guard.
  //
  // Hardcoded red/green/emerald palette classes are banned: price/value
  // direction must use text-up/text-down (they flip under the
  // [data-market-colors] convention) and status must use
  // success/danger/warning, so a raw green-400 is always a bug waiting
  // for the TW-convention flip. amber/yellow are not restricted (too
  // many legitimate decorative uses).
  //
  // Exempted files keep palette classes on purpose:
  //   - HealthMetricsSparkline: legend dots must match recharts hex
  //     strokes (chart colours migrate in PR-4).
  //   - SignalQualityCard: emerald/orange/yellow correlation scale.
  //   - DiscussionLessonsCard / MaturityBadge / CapturedSessionBadge /
  //     LessonLibraryPage / RoundContextsCard: categorical badge sets
  //     (green sits alongside purple/blue/slate — not direction/status).
  //   - UsageCard / AIPage: per-provider brand colours.
  //   - DividendsPanel: cash-vs-stock dividend column accents.
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: [
      "src/components/discussion/HealthMetricsSparkline.tsx",
      "src/components/admin/SignalQualityCard.tsx",
      "src/components/admin/DiscussionLessonsCard.tsx",
      "src/components/discussion/MaturityBadge.tsx",
      "src/components/discussion/CapturedSessionBadge.tsx",
      "src/components/discussion/RoundContextsCard.tsx",
      "src/pages/LessonLibraryPage.tsx",
      "src/components/admin/UsageCard.tsx",
      "src/pages/AIPage.tsx",
      "src/components/stock/DividendsPanel.tsx",
    ],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "Literal[value=/(?:text|bg|border|ring|accent)-(?:red|green|emerald)-[0-9]/]",
          message:
            "Hardcoded red/green/emerald Tailwind classes are banned. Use text-up/text-down (market direction, flips with [data-market-colors]) or text-success/text-danger (status).",
        },
        {
          selector:
            "TemplateElement[value.cooked=/(?:text|bg|border|ring|accent)-(?:red|green|emerald)-[0-9]/]",
          message:
            "Hardcoded red/green/emerald Tailwind classes are banned. Use text-up/text-down (market direction, flips with [data-market-colors]) or text-success/text-danger (status).",
        },
      ],
    },
  },

  // Project-wide overrides
  {
    rules: {
      // Allow explicit `any` where genuinely needed (API responses, mutations)
      "@typescript-eslint/no-explicit-any": "off",
      // Allow unused vars if prefixed with _
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      // Allow non-null assertions (we use them for refs and DOM access)
      "@typescript-eslint/no-non-null-assertion": "off",
      // We do not use React Compiler — suppress its memoization-compatibility warnings
      "react-hooks/incompatible-library": "off",
    },
  }
);
