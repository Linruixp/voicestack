/* VoiceStudio five-view local UI — Playwright click-through (task 25).
 *
 * Drives the REAL service on 127.0.0.1:3910 through the browser exactly as an
 * operator would, and verifies the required invariants:
 *   1. upload → name a NEW cluster   → it appears in the speaker directory
 *   2. upload → assign a cluster to an EXISTING speaker → NO duplicate speaker row
 *   3. merge two speakers            → the duplicate is gone
 *   4. split a cluster               → two clusters
 *   5. served bundle contains NO service token
 *   6. UI served only on 127.0.0.1:3910 (same-origin)
 *   7. naming/assigning updates the transcript label (not only the directory)
 *   8. the transcript emits a low-rate GET /health keepalive
 *   9. the meeting-history entry lists the linked speaker
 *  10. the deep-link speaker-naming wizard resolves an unknown cluster (no new nav tab)
 *
 * Env:
 *   VS_BASE_URL    default http://127.0.0.1:3910
 *   VS_TOKEN       service bearer token, used ONLY by this script for read-only
 *                  API assertions (never sent to the page)
 *   VS_FIXTURES    fixture root, default ~/voicestack/fixtures
 *   VS_EVIDENCE    evidence dir for screenshots + results JSON
 */
import { chromium, request } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const BASE = process.env.VS_BASE_URL || "http://127.0.0.1:3910";
const TOKEN = process.env.VS_TOKEN || "";
const FIXTURES = process.env.VS_FIXTURES || join(homedir(), "voicestack", "fixtures");
const EVIDENCE = process.env.VS_EVIDENCE || join(homedir(), ".omo", "evidence", "voicestudio-omo-local-stack", "task-25-ui");
const MEETINGS = join(FIXTURES, "meetings");
const AUDIO = join(FIXTURES, "audio");
// A voice that is NOT enrolled by the earlier scenarios, so the upload leaves an
// unknown cluster and opens a speaker batch for the deep-link wizard. run-e2e.sh
// synthesises a fresh voice (VS_WIZARD_AUDIO); the fallback may already be known.
const WIZARD_AUDIO = process.env.VS_WIZARD_AUDIO || join(AUDIO, "en_30s.wav");

mkdirSync(EVIDENCE, { recursive: true });

const results = [];
const checks = [];
function expect(name, cond, detail = "") {
  const ok = Boolean(cond);
  results.push({ name, ok, detail });
  checks.push(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` — ${detail}` : ""}`);
  console.log(`${ok ? "✅" : "❌"} ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) throw new Error(`assertion failed: ${name} (${detail})`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  if (!TOKEN) throw new Error("VS_TOKEN is required for read-only API assertions");
  expect("UI bound to loopback origin", new URL(BASE).origin === "http://127.0.0.1:3910", BASE);

  const api = await request.newContext({ baseURL: BASE, extraHTTPHeaders: { Authorization: `Bearer ${TOKEN}` } });
  const speakers = async () => (await (await api.get("/speakers")).json()).speakers;
  const detail = async (id) => (await api.get(`/meetings/${id}`)).json();

  const browser = await chromium.launch();
  const context = await browser.newContext();
  const page = await context.newPage();

  const keepaliveLines = [];
  page.on("console", (msg) => { if (msg.text().includes("[vs-ui] keepalive")) keepaliveLines.push(msg.text()); });
  page.on("pageerror", (err) => console.log(`[pageerror] ${err.message}`));

  const shot = (name) => page.screenshot({ path: join(EVIDENCE, `${name}.png`), fullPage: true });
  const waitTranscript = () => page.waitForSelector('[data-testid="view-transcript"].active', { timeout: 300_000 });
  const selectCluster = async () => { await page.locator("[data-resolve]").first().click(); };
  const upload = async (fixture, title) => {
    await page.click('[data-testid="tab-upload"]');
    await page.setInputFiles('[data-testid="upload-file"]', join(fixture));
    await page.fill('[data-testid="upload-title"]', title);
    await page.click('[data-testid="upload-btn"]');
    await waitTranscript();
    const id = Number(await page.inputValue('[data-testid="transcript-meeting"]'));
    expect(`upload "${title}" produced a transcript`, Number.isInteger(id) && id > 0, `meeting #${id}`);
    return id;
  };

  // 0. Load the UI root: it mints the session cookie. No token is ever passed in.
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="view-upload"]');
  expect("UI root is the five-view page", await page.locator("nav button").count() === 5);
  await shot("01-upload");

  // ---- 1. upload A1 -> name a NEW cluster "Alice" --------------------------
  const m1 = await upload(join(MEETINGS, "spkA_rec1.wav"), "A1 planning");
  await shot("02-transcript-unknown");

  const d1 = await detail(m1);
  expect("A1 has at least one unknown cluster to name", d1.unknown_clusters.length >= 1, `${d1.unknown_clusters.length}`);
  await selectCluster();
  await page.fill('[data-testid="enroll-name"]', "Alice");
  await page.fill('[data-testid="enroll-org"]', "Acme");
  await page.click('[data-testid="enroll-btn"]');
  await page.waitForFunction(() => document.querySelector('[data-testid="assign-status"]').textContent.includes("Created speaker"));
  await shot("03-assign-named-new");

  let sp = await speakers();
  expect("naming a NEW cluster appears in the directory", sp.length === 1 && sp[0].name === "Alice", JSON.stringify(sp.map((s) => s.name)));
  const aliceId = sp[0].id;
  await page.click('[data-testid="tab-speakers"]');
  await page.waitForSelector(`[data-testid="speaker-row-${aliceId}"]`);
  await shot("04-directory-alice");

  // transcript label updated to Alice (not only the directory)
  const d1b = await detail(m1);
  expect("naming updated the transcript label", d1b.unknown_clusters.length === 0 && d1b.segments.every((s) => s.speaker_name === "Alice"), `unknown=${d1b.unknown_clusters.length}`);

  // ---- 2. upload B1 -> split -> assign to EXISTING Alice (no duplicate) ----
  const m2 = await upload(join(MEETINGS, "spkB_rec1.wav"), "B1 review");
  const d2 = await detail(m2);
  expect("B1 starts with at least one unknown cluster", d2.unknown_clusters.length >= 1, `${d2.unknown_clusters.length}`);
  const before = d2.unknown_clusters.length;

  // Choose the cluster with the most segments and split it at its 2nd segment
  // start, so both halves are guaranteed non-empty (and voiced >= 1 s).
  const candidates = d2.unknown_clusters
    .map((c) => ({ c, segs: d2.segments.filter((s) => s.cluster_id === c.cluster_id) }))
    .filter((x) => x.segs.length >= 2)
    .sort((a, b) => b.segs.length - a.segs.length);
  expect("a splittable cluster exists", candidates.length >= 1, `unknown=${before}`);
  const target = candidates[0];
  const splitAt = target.segs[1].start;

  await page.click('[data-testid="tab-assign"]');
  await page.selectOption('[data-testid="split-cluster"]', String(target.c.cluster_id));
  await page.fill('[data-testid="split-at"]', String(splitAt));
  await page.click('[data-testid="split-btn"]');
  await page.waitForFunction(() => document.querySelector('[data-testid="assign-status"]').textContent.includes("Split cluster"));
  const d2split = await detail(m2);
  expect("split a cluster -> two clusters", d2split.unknown_clusters.length === before + 1, `${before} -> ${d2split.unknown_clusters.length}`);
  await shot("05-split-two-clusters");

  // Attach the unknown cluster with the most voiced audio (>= 1 s to enroll).
  const voiced = (c) => d2split.segments
    .filter((s) => s.cluster_id === c.cluster_id)
    .reduce((sum, s) => sum + (s.end - s.start), 0);
  const attachCluster = [...d2split.unknown_clusters].sort((a, b) => voiced(b) - voiced(a))[0];
  expect("the cluster to assign has >= 1 s of voiced audio", voiced(attachCluster) >= 1, `${voiced(attachCluster).toFixed(2)}s`);
  await page.click('[data-testid="tab-transcript"]');
  await page.click(`[data-testid="unknown-cluster-${attachCluster.cluster_id}"] button[data-resolve]`);
  await page.selectOption('[data-testid="attach-speaker"]', String(aliceId));
  await page.click('[data-testid="attach-btn"]');
  await page.waitForFunction(() => document.querySelector('[data-testid="assign-status"]').textContent.includes("Attached cluster"));

  const afterAttach = await speakers();
  expect("assign to an EXISTING speaker creates NO duplicate row", afterAttach.length === 1 && afterAttach[0].id === aliceId, JSON.stringify(afterAttach.map((s) => s.name)));
  expect("the existing speaker gained a voiceprint", afterAttach[0].voiceprint_count >= 2, `count=${afterAttach[0].voiceprint_count}`);
  const d2b = await detail(m2);
  expect("assigning updated that meeting's transcript label to Alice", d2b.segments.some((s) => s.speaker_name === "Alice"), `unknown=${d2b.unknown_clusters.length}`);
  await shot("06-transcript-after-attach");

  // ---- 3. upload B2 -> auto-recognised as Alice (label + history) ----------
  const m3 = await upload(join(MEETINGS, "spkB_rec2.wav"), "B2 follow-up");
  const d3 = await detail(m3);
  expect("returning speaker auto-recognised (no unknown clusters)", d3.unknown_clusters.length === 0 && d3.segments.every((s) => s.speaker_name === "Alice"), `unknown=${d3.unknown_clusters.length}`);
  await page.click('[data-testid="tab-meetings"]');
  await page.waitForFunction((id) => { const el = document.querySelector(`[data-testid="meeting-speakers-${id}"]`); return el && el.textContent.includes("Alice"); }, m3, { timeout: 10_000 });
  const historyText = await page.textContent(`[data-testid="meeting-speakers-${m3}"]`);
  expect("meeting history lists the linked speaker", historyText.includes("Alice"), historyText);
  await shot("07-meeting-history");

  // ---- 4. upload a NEW voice (ZH) -> name it "Bob" -------------------------
  await upload(join(AUDIO, "zh_30s.wav"), "C1 new voice");
  await selectCluster();
  await page.fill('[data-testid="enroll-name"]', "Bob");
  await page.click('[data-testid="enroll-btn"]');
  await page.waitForFunction(() => document.querySelector('[data-testid="assign-status"]').textContent.includes("Created speaker"));
  sp = await speakers();
  expect("directory now has two speakers", sp.length === 2, JSON.stringify(sp.map((s) => s.name)));
  const bobId = sp.find((s) => s.name === "Bob").id;
  await page.click('[data-testid="tab-speakers"]');
  await page.waitForSelector(`[data-testid="speaker-row-${bobId}"]`);
  await shot("08-directory-alice-bob");

  // ---- 5. merge Bob -> Alice: the duplicate is gone ------------------------
  await page.click('[data-testid="tab-assign"]');
  await page.selectOption('[data-testid="merge-target"]', String(aliceId));
  await page.selectOption('[data-testid="merge-source"]', String(bobId));
  await page.click('[data-testid="merge-btn"]');
  await page.waitForFunction(() => document.querySelector('[data-testid="assign-status"]').textContent.includes("Merged source"));
  const merged = await speakers();
  expect("merge removes the duplicate speaker", merged.length === 1 && merged[0].name === "Alice" && !merged.some((s) => s.name === "Bob"), JSON.stringify(merged.map((s) => s.name)));
  await page.click('[data-testid="tab-speakers"]');
  await shot("09-directory-after-merge");

  // ---- 6. biometric-purge confirmation (dialog present, no delete) ---------
  await page.click(`[data-delete="${aliceId}"]`);
  await page.waitForSelector('[data-testid="purge-dialog"][open]');
  const purgeMsg = await page.textContent('[data-testid="purge-message"]');
  expect("delete asks for biometric-purge confirmation", /purges? \d+ biometric voiceprint/i.test(purgeMsg), purgeMsg.slice(0, 80));
  await shot("10-purge-confirmation");
  await page.click('[data-testid="purge-cancel"]');

  // ---- 7. served bundle contains NO token ---------------------------------
  const anon = await request.newContext({ baseURL: BASE });
  const bodies = [];
  for (const path of ["/", "/static/app.js", "/static/app.css"]) {
    const res = await anon.get(path);
    expect(`served ${path}` , res.ok(), `status=${res.status()}`);
    bodies.push(await res.text());
  }
  expect("served bundle contains NO token string", bodies.every((b) => !b.includes(TOKEN)), `token len=${TOKEN.length}`);
  expect("no login page in served HTML", !bodies[0].toLowerCase().includes("login"));

  // ---- 8. low-rate /health keepalive -------------------------------------
  // The first tick fires on load; wait for a second tick to prove the interval.
  const deadline = Date.now() + 45_000;
  while (keepaliveLines.length < 2 && Date.now() < deadline) await sleep(1000);
  expect("UI emits the low-rate /health keepalive", keepaliveLines.length >= 2, keepaliveLines.join(" | "));
  expect("keepalive reports the 30000ms interval", keepaliveLines.some((l) => l.includes("30000")), keepaliveLines[0] || "");

  // ---- 9. deep-link speaker-naming wizard (a view, not a nav tab) ----------
  const mW = await upload(WIZARD_AUDIO, "D1 wizard voice");
  const dW = await detail(mW);
  expect("wizard fixture left an open handoff batch", dW.handoff.needed === true && dW.handoff.batch_id != null, JSON.stringify(dW.handoff));
  const batchW = dW.handoff.batch_id;
  await page.goto(`${BASE}/?meeting=${mW}&task=speakers&batch=${batchW}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="view-wizard"].active', { timeout: 60_000 });
  expect("deep-linked wizard does NOT add a nav tab", await page.locator("nav button").count() === 5);
  const sampleSrc = await page.getAttribute('[data-testid="wizard-sample"]', "src");
  expect(
    "wizard points an <audio> at the cluster sample",
    Boolean(sampleSrc) && sampleSrc.includes("/clusters/") && sampleSrc.includes("/sample"),
    sampleSrc || "",
  );
  await page.check('[data-testid="wizard-action-enroll"]');
  await page.fill('[data-testid="wizard-new-name"]', "Wizard Speaker");
  await page.fill('[data-testid="wizard-new-title"]', "主讲人");
  expect("remember checkbox defaults OFF", (await page.isChecked('[data-testid="wizard-remember"]')) === false);
  let wizardGuard = 0;
  while (wizardGuard < 25 && await page.locator('[data-testid="wizard-submit"]').isHidden()) {
    wizardGuard += 1;
    await page.click('[data-testid="wizard-next"]');
    await page.check('[data-testid="wizard-action-enroll"]');
    await page.fill('[data-testid="wizard-new-name"]', `Wizard Speaker ${wizardGuard}`);
  }
  await page.click('[data-testid="wizard-submit"]');
  await page.waitForSelector('[data-testid="view-transcript"].active', { timeout: 120_000 });
  const dWafter = await detail(mW);
  expect("wizard resolved every unknown cluster", dWafter.unknown_clusters.length === 0, `${dWafter.unknown_clusters.length}`);
  expect("wizard cleared the handoff", dWafter.handoff.needed === false, JSON.stringify(dWafter.handoff));
  const wizSpeakers = await speakers();
  expect(
    "wizard created the speaker with its job title",
    wizSpeakers.some((s) => s.name === "Wizard Speaker" && s.title === "主讲人"),
    JSON.stringify(wizSpeakers.map((s) => `${s.name}/${s.title}`)),
  );
  const wizardSpeaker = wizSpeakers.find((s) => s.name === "Wizard Speaker");
  expect(
    "unchecked remember stored NO biometric voiceprint",
    wizardSpeaker != null && wizardSpeaker.voiceprint_count === 0,
    `voiceprint_count=${wizardSpeaker ? wizardSpeaker.voiceprint_count : "n/a"}`,
  );
  await shot("11-wizard-resolved");

  await browser.close();
  await api.dispose();
  await anon.dispose();

  const report = { base: BASE, evidence: EVIDENCE, checks, results, keepalive: keepaliveLines };
  writeFileSync(join(EVIDENCE, "playwright-results.json"), JSON.stringify(report, null, 2));
  console.log(`\nALL ${results.length} ASSERTIONS PASSED`);
}

main().catch((err) => {
  console.error(`\nE2E FAILED: ${err.message}`);
  writeFileSync(join(EVIDENCE, "playwright-results.json"), JSON.stringify({ error: err.message, checks, results }, null, 2));
  process.exit(1);
});
