import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

const config: Config = {
  // No `.dark` class in this app — theming is driven by the [data-light]
  // attribute + CSS-var value layer (index.css), so `dark:` variants were
  // dead config (the one stray usage was converted to a semantic token).
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
      },
      colors: {
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        // Surface elevation scale
        surface: {
          0: "hsl(var(--surface-0))",
          1: "hsl(var(--surface-1))",
          2: "hsl(var(--surface-2))",
          3: "hsl(var(--surface-3))",
        },
        // Status semantics
        success: "hsl(var(--success))",
        warning: "hsl(var(--warning))",
        danger: "hsl(var(--danger))",
        info: "hsl(var(--info))",
        // Market direction — follows [data-market-colors] convention
        up: "hsl(var(--up))",
        down: "hsl(var(--down))",
        flat: "hsl(var(--flat))",
        // Categorical chart series
        chart: {
          1: "hsl(var(--chart-1))",
          2: "hsl(var(--chart-2))",
          3: "hsl(var(--chart-3))",
          4: "hsl(var(--chart-4))",
          5: "hsl(var(--chart-5))",
          6: "hsl(var(--chart-6))",
        },
        // Transitional aliases — migrate call sites to up/down, then remove
        positive: "hsl(var(--up))",
        negative: "hsl(var(--down))",
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      // Typographic scale — role-named, not size-named, so a later density
      // or font swap re-tunes one place. Additive: text-xs/text-sm stay for
      // the hundreds of existing call sites; new work uses role tokens.
      // [size, { lineHeight, letterSpacing }]. Negative tracking on large
      // sizes is what reads as "terminal-grade" rather than default Tailwind.
      fontSize: {
        micro: ["10px", { lineHeight: "14px", letterSpacing: "0.02em" }],
        label: ["11px", { lineHeight: "16px", letterSpacing: "0.06em" }],
        data: ["12px", { lineHeight: "18px" }],
        body: ["13px", { lineHeight: "20px" }],
        heading: ["14px", { lineHeight: "20px", letterSpacing: "-0.006em" }],
        stat: ["20px", { lineHeight: "24px", letterSpacing: "-0.02em" }],
        title: ["24px", { lineHeight: "30px", letterSpacing: "-0.014em" }],
        display: ["30px", { lineHeight: "36px", letterSpacing: "-0.02em" }],
      },
      // Spacing rhythm — semantic keys layered on the 4px grid so all pages
      // share one cadence (p-gutter sm:p-page, space-y-stack sm:space-y-section).
      spacing: {
        field: "0.5rem", // 8px  — control / inline gap
        stack: "0.75rem", // 12px — gap inside a card / between tiles
        gutter: "1rem", // 16px — page edge padding (mobile)
        page: "1.5rem", // 24px — page edge padding (desktop)
        section: "1.5rem", // 24px — vertical gap between page sections
      },
      // Font stacks. W0 points at system fonts (zero network cost); a later
      // isolated PR may self-host Inter + JetBrains Mono (woff2, subset,
      // preload) so its LCP/CLS is measured on its own.
      fontFamily: {
        sans: ["Inter var", "Inter", "system-ui", "sans-serif"],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
      // Elevation — inset top-highlight for raised panels; drop shadows are
      // reserved for FLOATING layers only (see index.css tokens).
      boxShadow: {
        highlight: "var(--highlight-top)",
        popover: "var(--shadow-popover)",
        overlay: "var(--shadow-overlay)",
      },
    },
  },
  plugins: [animate],
};

export default config;
