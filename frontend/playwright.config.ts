import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./output/playwright-results",
  // The professional-beta journey intentionally crosses invitation,
  // disclosures, portfolio, alerts, research and stress testing. Thirty
  // seconds is too tight on a cold CI database/browser even when healthy.
  timeout: 120_000,
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { outputFolder: "output/playwright-report", open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    launchOptions: { chromiumSandbox: false },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
