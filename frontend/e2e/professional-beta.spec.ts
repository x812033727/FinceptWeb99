import { expect, request, test } from "@playwright/test";

const apiBase = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8000";
const adminEmail = process.env.E2E_ADMIN_EMAIL ?? "admin-e2e@example.com";
const adminPassword = process.env.E2E_ADMIN_PASSWORD ?? "AdminPass99!";

test("administrator invitation, account activation, and disclosures", async ({ page }) => {
  const api = await request.newContext({ baseURL: apiBase });
  const email = `analyst-${Date.now()}@example.com`;

  // Exercise the administrator surface, not just the invitation API.
  await page.goto("/login");
  await page.getByLabel("Email").fill(adminEmail);
  await page.getByLabel("Password").fill(adminPassword);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  const adminDisclosureGate = page.getByRole("heading", { name: "Review required disclosures" });
  if (await adminDisclosureGate.isVisible()) {
    await page.getByRole("button", { name: "Accept all and continue" }).click();
    await expect(adminDisclosureGate).not.toBeVisible();
  }
  await page.goto("/admin?tab=users");
  await expect(page.getByRole("heading", { name: "Invite a user" })).toBeVisible();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Role").selectOption("analyst");
  await page.getByRole("button", { name: "Create invitation" }).click();
  const activationLink = await page.getByLabel("Activation link").inputValue();
  const rawToken = new URL(activationLink).searchParams.get("token");
  expect(rawToken).toBeTruthy();

  await page.goto(activationLink);
  await page.getByLabel("Password").fill("AnalystPass99!");
  await page.getByRole("button", { name: "Accept invitation" }).click();

  await expect(page.getByRole("heading", { name: "Review required disclosures" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "AI and market-data disclosure" })).toBeVisible();
  await page.getByRole("button", { name: "Accept all and continue" }).click();

  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText(email, { exact: true }).first()).toBeVisible();
  await expect(page.getByText("analyst", { exact: true }).first()).toBeVisible();

  const portfolioName = `Taiwan Core ${Date.now()}`;
  await page.goto("/portfolio");
  await page.getByRole("button", { name: /New Portfolio/ }).click();
  await page.getByLabel("Portfolio name").fill(portfolioName);
  await page.getByLabel("Currency").selectOption("TWD");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByRole("button", { name: `${portfolioName} TWD` })).toBeVisible();

  await page.goto("/alerts");
  await page.getByLabel("Symbol").fill("2330");
  await page.getByLabel("Market").selectOption("TW");
  await page.getByLabel("Condition Type").selectOption("price_above");
  await page.getByLabel("Target Price").fill("1200");
  await page.getByRole("button", { name: "Create Alert", exact: true }).click();
  const activeAlerts = page.locator("section").filter({
    has: page.getByRole("heading", { name: /^Active/ }),
  });
  await expect(activeAlerts.getByText("2330", { exact: true })).toBeVisible();
  await expect(activeAlerts.getByText("TW", { exact: true })).toBeVisible();
  await expect(activeAlerts.getByText("1,200", { exact: true })).toBeVisible();

  // Deterministically verify the stock lookup + streamed AI-report UI. The
  // backend's evidence, persistence and fail-closed contract is covered by
  // integration tests; browser CI must not depend on a billable external LLM.
  await page.route("**/api/ai/stock-reports?**", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  await page.route("**/api/ai/stock-report/TW/2330", (route) => {
    const report = [
      "## Executive summary",
      "- TSMC is under review using evidence-backed data [E1].",
      "## Risks",
      "- Reassess when the saved thesis conditions change.",
    ].join("\n");
    const frames = [
      { stage: "context" },
      { stage: "generating" },
      { delta: report },
      { done: { report_id: "e2e-report", created_at: "2026-07-15T00:00:00Z" } },
    ].map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join("") + "data: [DONE]\n\n";
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache" },
      body: frames,
    });
  });
  await page.goto("/stock/TW/2330");
  await expect(page.getByText("2330", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "AI Report" }).click();
  await page.getByRole("button", { name: "Generate Report" }).click();
  await expect(page.getByTestId("ai-report-content")).toContainText("TSMC is under review");
  await expect(page.getByText(/not investment advice/i)).toBeVisible();

  await page.goto("/research");
  await expect(page.getByRole("heading", { name: "Research Workspace" })).toBeVisible();
  await page.getByRole("button", { name: "New thesis" }).click();
  await page.getByRole("textbox", { name: "Symbol", exact: true }).fill("2330");
  await page.getByPlaceholder("Thesis title").fill("Advanced-node demand");
  await page.getByPlaceholder(/Core case/).fill("Advanced-node demand remains durable; review if revenue growth weakens.");
  await page.getByRole("button", { name: "Create thesis" }).click();
  await page.getByRole("button", { name: "theses" }).click();
  await page.getByRole("button", { name: /TW:2330 · Advanced-node demand/ }).click();
  await page.getByPlaceholder(/What changed/).fill("Initial review confirms the core case and monitoring conditions.");
  await page.getByRole("button", { name: "Save review" }).click();
  await expect(page.getByText(/Thesis review: unchanged/)).toBeVisible();

  await page.getByRole("button", { name: "Report data issue" }).click();
  await page.getByPlaceholder("Symbol (optional)").fill("2330");
  await page.getByPlaceholder("Endpoint or page (optional)").fill("/api/tw/quote/2330");
  await page.getByPlaceholder(/Describe what appears wrong/).fill("E2E verification of the data-quality feedback workflow.");
  await page.getByRole("button", { name: "Submit issue" }).click();

  await page.goto("/portfolio");
  await page.getByRole("button", { name: "Stress test" }).click();
  await page.getByRole("button", { name: "Run scenarios" }).click();
  await expect(page.getByText("TAIEX -10%", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/Deterministic decision-support scenarios/)).toBeVisible();

  const replay = await api.post("/api/auth/accept-invite", {
    data: { token: rawToken!, email, password: "AnotherPass99!" },
  });
  expect(replay.status()).toBe(400);
  await api.dispose();
});

test("forgot-password response does not disclose account existence", async ({ page }) => {
  await page.goto("/forgot-password");
  await page.getByLabel("Email").fill(`unknown-${Date.now()}@example.com`);
  await page.getByRole("button", { name: "Send reset link" }).click();
  await expect(page.getByRole("status")).toHaveText(
    "If the account exists, reset instructions have been sent.",
  );
});
