import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

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
