/*
 * The browser half of the end-to-end run: a person using the dashboard.
 *
 *   node scripts/e2e/browser.cjs <step> <base-url> <out-dir>
 *
 * Steps print one JSON object on the last line of stdout, which scripts/e2e/run.py records as
 * evidence. Playwright drives the pre-installed Chromium; nothing here talks to the database.
 */
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");

const [step, base, outDir] = process.argv.slice(2);
const PASSPHRASE = process.env.E2E_PASSPHRASE;

async function signIn(page) {
  await page.goto(`${base}/login`);
  await page.fill("input", PASSPHRASE);
  await page.click("button[type=submit]");
  await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 30_000 });
}

async function waitForOutcome(page, timeoutMs) {
  const outcomes = ["Published", "An identical edition already exists", "Nothing new", "Waiting for data",
    "Blocked before publication", "Failed", "Cancelled"];
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const title = (await page.locator(".card-title").first().textContent().catch(() => "")) ?? "";
    if (outcomes.includes(title.trim())) return title.trim();
    await page.waitForTimeout(3000);
  }
  throw new Error(`no outcome within ${timeoutMs / 1000}s`);
}

async function stagesSeen(page) {
  return page.$$eval(".stages li", (items) => items.map((li) => `${li.textContent.trim()}:${li.dataset.state}`));
}

const steps = {
  /** J1-J3: ask for the latest monthly with email, double-click, leave mid-run, come back, download. */
  async generate(browser) {
    const ctx = await browser.newContext({ acceptDownloads: true });
    const page = await ctx.newPage();
    await signIn(page);
    await page.goto(`${base}/generate`);
    await page.selectOption("#report-type", "monthly");
    await page.check("text=Email me when it is ready");
    // two clicks in quick succession, and a second request straight from the page, as a second tab would
    const [, second] = await Promise.all([
      page.dblclick("button[type=submit]"),
      page.evaluate(async () => {
        const r = await fetch("/api/jobs", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ report_type: "monthly", params: { period: "latest", refresh: "latest_data" }, email_me: true }) });
        return r.json();
      }),
    ]);
    await page.waitForURL(/\/jobs\/job_/, { timeout: 30_000 });
    const jobId = page.url().split("/jobs/")[1];
    const firstStages = await stagesSeen(page);
    // close the browser entirely while the job is running
    await ctx.close();
    await new Promise((r) => setTimeout(r, 15_000));
    const ctx2 = await browser.newContext({ acceptDownloads: true });
    const page2 = await ctx2.newPage();
    await signIn(page2);
    await page2.goto(`${base}/jobs`);
    const listed = await page2.locator(`text=${jobId}`).count();
    await page2.goto(`${base}/jobs/${jobId}`);
    const midStages = await stagesSeen(page2);
    const outcome = await waitForOutcome(page2, 25 * 60_000);
    const finalStages = await stagesSeen(page2);
    const pdfLink = page2.locator("a.button", { hasText: "PDF" }).first();
    const [download] = await Promise.all([page2.waitForEvent("download"), pdfLink.click()]);
    const pdfPath = path.join(outDir, `J1-${jobId}.pdf`);
    await download.saveAs(pdfPath);
    const bodyText = await page2.locator("body").innerText();
    await page2.screenshot({ path: path.join(outDir, "J1-job-page.png"), fullPage: true });
    await ctx2.close();
    return { jobId, secondRequestJobId: second.job_id, secondRequestCreated: second.created, listedAfterReturn: listed > 0,
      firstStages, midStages, finalStages, outcome, pdfPath, pdfBytes: fs.statSync(pdfPath).size,
      emailSectionMentionsRequester: /manual request/.test(bodyText) };
  },

  /** J4: the same request again is answered with the existing edition; "generate anyway" confirms it. */
  async reuse(browser) {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await signIn(page);
    await page.goto(`${base}/generate`);
    await page.selectOption("#report-type", "monthly");
    await page.click("button[type=submit]");
    await page.waitForSelector("text=An identical validated edition already exists", { timeout: 30_000 });
    const offerText = await page.locator(".notice[role=status]").innerText();
    await page.screenshot({ path: path.join(outDir, "J4-offer.png"), fullPage: true });
    await page.click("text=Generate anyway");
    await page.waitForURL(/\/jobs\/job_/, { timeout: 30_000 });
    const jobId = page.url().split("/jobs/")[1];
    const outcome = await waitForOutcome(page, 20 * 60_000);
    await ctx.close();
    return { offerText, jobId, outcome };
  },

  /** J12: a failed delivery retried from Settings. */
  async retryEmail(browser) {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await signIn(page);
    await page.goto(`${base}/settings`);
    const before = await page.locator("table.data >> text=failed").count();
    await page.locator("button", { hasText: "Retry" }).first().click();
    await page.waitForSelector("text=Retry queued.", { timeout: 30_000 });
    await page.reload();
    await page.screenshot({ path: path.join(outDir, "J12-ledger.png"), fullPage: true });
    const after = await page.locator("table.data >> text=failed").count();
    await ctx.close();
    return { failedRowsBefore: before, failedRowsAfter: after };
  },

  /** J16: the preview deployment's test email goes nowhere. */
  async previewTestEmail(browser) {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await signIn(page);
    await page.goto(`${base}/settings`);
    const deployment = await page.locator("text=Environment").locator("xpath=..").innerText();
    await page.click("text=Send a test email to me");
    await page.waitForSelector("text=Test email:", { timeout: 30_000 });
    const result = await page.locator(".notice[role=status]").innerText();
    await ctx.close();
    return { deployment, result };
  },
};

(async () => {
  const browser = await chromium.launch();
  try {
    const result = await steps[step](browser);
    console.log(JSON.stringify(result));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.log(JSON.stringify({ error: String(error && error.stack || error) }));
  process.exit(1);
});
