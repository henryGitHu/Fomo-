// End-to-end browser test (SIMULATED providers): drives the real UI against the real backend.
// Prereqs: backend on :8000 (worker enabled) and `next start` on :3000. Usage: node e2e/run-e2e.mjs [outDir]
import fs from "node:fs";
import { createRequire } from "node:module";

// Use the project's playwright if installed, else the machine-wide one (require() honours NODE_PATH).
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.env.E2E_BASE || "http://127.0.0.1:3000";
const OUT = process.argv[2] || "e2e-output";
fs.mkdirSync(OUT, { recursive: true });
const results = [];
const step = async (name, fn) => {
  const t0 = Date.now();
  try {
    await fn();
    results.push({ name, ok: true, ms: Date.now() - t0 });
    console.log(`PASS ${name}`);
  } catch (e) {
    results.push({ name, ok: false, error: String(e).slice(0, 500) });
    console.log(`FAIL ${name}: ${e}`);
    throw e;
  }
};
const api = async (path, init) => {
  const r = await fetch(`${BASE}/api${path}`, { headers: { "Content-Type": "application/json" }, ...init });
  if (!r.ok) throw new Error(`${path} -> ${r.status} ${await r.text()}`);
  return r.json();
};
const waitFor = async (fn, ms = 60000, every = 500) => {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > end) throw new Error("timed out waiting");
    await new Promise((r) => setTimeout(r, every));
  }
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const consoleErrors = [];
page.on("pageerror", (e) => consoleErrors.push(String(e)));
let pid;

try {
  await step("dashboard loads", async () => {
    await page.goto(BASE);
    await page.getByRole("heading", { name: "Publishing Dashboard" }).waitFor();
  });

  await step("create a 24-illustration book through New Book Setup", async () => {
    await page.getByRole("link", { name: "+ New book" }).click();
    await page.locator('input[name="title"]').fill("E2E Chemical Engineers");
    await page.locator('input[name="theme"]').fill("chemical engineering");
    await page.locator('input[name="author"]').fill("E2E Press");
    await page.locator('input[name="illustration_count"]').fill("24");
    await page.locator('input[name="budget"]').fill("5");
    await page.getByRole("button", { name: /Create book/ }).click();
    await page.waitForURL(/\/projects\/[a-f0-9]+\/research/);
    pid = page.url().match(/projects\/([a-f0-9]+)/)[1];
  });

  await step("smart research report appears with references", async () => {
    await page.getByText("Report: chemical engineering").waitFor({ timeout: 30000 });
    await page.getByRole("heading", { name: "References" }).waitFor();
    await page.screenshot({ path: `${OUT}/03-research.png`, fullPage: false });
  });

  await step("section planner builds a plan that sums exactly", async () => {
    await page.getByRole("link", { name: "Section Planner" }).first().click();
    await page.getByRole("button", { name: "Build plan from research" }).click();
    await page.getByText(/Sections allocate 24 of 24 illustrations ✓/).waitFor({ timeout: 15000 });
    await page.screenshot({ path: `${OUT}/04-planner.png` });
  });

  await step("generate a 5-page batch in the Illustration Studio", async () => {
    await page.getByRole("link", { name: "Illustration Studio" }).click();
    await page.getByRole("button", { name: /Generate next 5/ }).click();
    await waitFor(async () => (await api(`/projects/${pid}/pages`)).filter((p) => p.current_revision).length === 5, 90000);
    await page.locator('[data-testid="page-card-5"] img').waitFor({ timeout: 20000 });
    await page.screenshot({ path: `${OUT}/06-studio.png` });
  });

  await step("open a page, approve it and lock another", async () => {
    await page.getByRole("button", { name: /Open page 1:/ }).click();
    const dialog = page.getByRole("dialog", { name: "Page detail" });
    await dialog.getByRole("button", { name: "Approve" }).click();
    await waitFor(async () => (await api(`/projects/${pid}/pages`))[0].status === "approved");
    await page.screenshot({ path: `${OUT}/06b-page-detail.png` });
    await dialog.getByRole("button", { name: "Close page detail" }).click();
    const p2 = (await api(`/projects/${pid}/pages`))[1];
    await api(`/pages/${p2.id}/lock`, { method: "POST", body: "{}" });
  });

  await step("Live Book Editor: targeted line-weight edit only touches page 3", async () => {
    const before = await api(`/projects/${pid}/pages`);
    await page.locator("#chat-input").fill("Make the lines thicker on page 3");
    await page.getByRole("button", { name: "Send" }).click();
    await page.getByText(/Queued: lines thicker on page\(s\) \[3\]/).waitFor({ timeout: 15000 });
    await waitFor(async () => (await api(`/projects/${pid}/pages`))[2].current_revision.id !== before[2].current_revision.id);
    const after = await api(`/projects/${pid}/pages`);
    for (const i of [0, 1, 3, 4]) {
      if (after[i].current_revision.id !== before[i].current_revision.id) throw new Error(`page ${i + 1} changed`);
    }
  });

  await step("chat respects locked page 2 on a broad command", async () => {
    const before = await api(`/projects/${pid}/pages`);
    await page.locator("#chat-input").fill("Regenerate pages 2-4");
    await page.getByRole("button", { name: "Send" }).click();
    await page.getByText(/Skipping locked page\(s\): \[2\]/).waitFor({ timeout: 15000 });
    await waitFor(async () => (await api(`/jobs?project_id=${pid}&limit=1`))[0].status === "succeeded", 60000);
    const after = await api(`/projects/${pid}/pages`);
    if (after[1].current_revision.id !== before[1].current_revision.id) throw new Error("locked page changed");
  });

  await step("chat panel persists across navigation", async () => {
    await page.getByRole("link", { name: "Captions" }).click();
    await page.getByRole("heading", { name: "Educational Caption Editor" }).waitFor();
    await page.getByText("Make the lines thicker on page 3").first().waitFor();
    await page.screenshot({ path: `${OUT}/08-captions.png` });
  });

  await step("caption edit saves without regenerating art", async () => {
    const before = await api(`/projects/${pid}/pages`);
    const box = page.locator(`#cap-${before[4].id}`);
    await box.fill("Mixers stir ingredients together evenly!");
    await box.locator("xpath=ancestor::tr").getByRole("button", { name: "Save" }).click();
    await waitFor(async () => (await api(`/projects/${pid}/pages`))[4].caption_text === "Mixers stir ingredients together evenly!");
    const after = await api(`/projects/${pid}/pages`);
    if (after[4].current_revision.id !== before[4].current_revision.id) throw new Error("art changed on caption edit");
  });

  await step("model battle between two providers and choose a winner", async () => {
    await page.getByRole("link", { name: "AI Model Battle" }).click();
    await page.getByRole("heading", { name: "AI Image Model Battle" }).waitFor();
    const pages = await api(`/projects/${pid}/pages`);
    await page.locator("main select").first().selectOption(pages[5].id);
    await page.getByLabel(/Mock Studio.*Mock Line Art v1/).check();
    await page.getByLabel(/Mock Atelier.*Atelier Sketch 1/).check();
    await page.getByRole("button", { name: "Start battle" }).click();
    await page.getByRole("button", { name: "Choose as winner" }).nth(1).waitFor({ timeout: 60000 });
    await page.screenshot({ path: `${OUT}/07-battle.png` });
    await page.getByRole("button", { name: "Choose as winner" }).first().click();
    await page.getByText("🏆").waitFor();
  });

  await step("AI Model Control Center lists providers and tests the mock connection", async () => {
    await page.getByRole("link", { name: "AI Model Control Center" }).click();
    await page.getByRole("heading", { name: "AI Model Control Center" }).waitFor();
    await page.getByRole("button", { name: "Test connection" }).first().click();
    await page.getByText(/✓ .*simulated/).first().waitFor();
    await page.screenshot({ path: `${OUT}/05-control-center.png` });
  });

  await step("finish the book via API, approve, preview and export final PDFs in the UI", async () => {
    for (;;) {
      const r = await fetch(`${BASE}/api/projects/${pid}/generate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ count: 10 }) });
      if (r.status === 409) break;
      const j = await r.json();
      await waitFor(async () => ["succeeded", "failed"].includes((await api(`/jobs/${j.id}`)).status), 120000);
    }
    await waitFor(async () => (await api(`/jobs?project_id=${pid}&status=running`)).length === 0 && (await api(`/jobs?project_id=${pid}&status=queued`)).length === 0, 120000);
    for (const p of await api(`/projects/${pid}/pages`)) {
      if (!p.locked && p.status !== "approved") await api(`/pages/${p.id}/approve`, { method: "POST", body: "{}" });
    }
    await page.goto(`${BASE}/projects/${pid}/preview`);
    await page.getByText(/52 interior pages · trim/).waitFor();
    await page.getByRole("button", { name: "Next →" }).click();
    await page.getByRole("button", { name: "Next →" }).click();
    await page.screenshot({ path: `${OUT}/09-preview.png` });
    await page.getByRole("link", { name: "Publish & Export" }).click();
    await page.getByLabel(/approve it for publication/).check();
    await page.getByText(/All mandatory checks pass/).waitFor({ timeout: 15000 });
    await page.getByRole("button", { name: "Export final KDP files" }).click();
    await waitFor(async () => (await api(`/projects/${pid}/exports`)).some((e) => e.kind === "zip"), 120000);
    await page.reload();
    await page.getByRole("link", { name: "Download" }).first().waitFor();
    await page.screenshot({ path: `${OUT}/10-publish.png` });
  });

  await step("persistence: reload the browser and everything is still there", async () => {
    await page.goto(BASE);
    await page.getByText("E2E Chemical Engineers").first().waitFor();
    await page.screenshot({ path: `${OUT}/01-dashboard.png` });
    const pages = await api(`/projects/${pid}/pages`);
    if (pages.length !== 24 || !pages[1].locked) throw new Error("state lost");
    const chat = await api(`/projects/${pid}/chat`);
    if (chat.length < 4) throw new Error("chat history lost");
  });

  await step("no uncaught page errors", async () => {
    if (consoleErrors.length) throw new Error(consoleErrors.join("\n"));
  });
} catch {
  process.exitCode = 1;
} finally {
  fs.writeFileSync(`${OUT}/results.json`, JSON.stringify({ base: BASE, simulated: true, results }, null, 2));
  await browser.close();
}
