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
