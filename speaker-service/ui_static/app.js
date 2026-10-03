/* VoiceStudio Speaker Service — minimal five-view UI.
 *
 * Auth: the document is loaded from GET / which mints the HttpOnly session
 * cookie; every fetch is same-origin and credentials:'same-origin', so no
 * token ever reaches this file. Mutations additionally carry the browser's
 * Origin header for the server's CSRF check.
 *
 * The UI adds no routes and no business logic: it only calls the existing API.
 */
"use strict";

const KEEPALIVE_MS = 30_000; // low-rate: an open UI counts as service activity
const state = {
  speakers: [], meetings: [], allMeetings: [], detail: null, selectedCluster: null, purge: null,
  wizard: null, wizardIndex: 0, wizardDecisions: {}, wizardSubmitting: false,
};

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (sec) => {
  const s = Math.max(0, Number(sec) || 0);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;
};
const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

async function api(path, { method = "GET", body = null } = {}) {
  const opts = { method, credentials: "same-origin" };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body != null) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    setStatus("speaker-status", "Session expired — reloading page…");
    setTimeout(() => location.reload(), 800);
    throw new Error("unauthorized");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data && data.detail ? JSON.stringify(data.detail) : `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return data;
}

function setStatus(id, text) { $(id).textContent = text; }

/* ── keepalive ───────────────────────────────────────────────────────────── */
async function keepalive() {
  try {
    const res = await fetch("/health", { cache: "no-store" });
    const el = $("health");
    el.classList.toggle("down", !res.ok);
    el.textContent = res.ok ? `service active · /health 200` : `service ${res.status}`;
    console.log(`[vs-ui] keepalive GET /health -> ${res.status} @ ${new Date().toISOString()} (every ${KEEPALIVE_MS}ms)`);
  } catch (err) {
    $("health").classList.add("down");
    $("health").textContent = "service unreachable";
    console.log(`[vs-ui] keepalive failed: ${err.message}`);
  }
}

/* ── navigation (five views, no router) ──────────────────────────────────── */
function show(view) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("active", s.id === `view-${view}`));
  const wizard = $("view-wizard");
  if (wizard) wizard.hidden = view !== "wizard";
}

/* ── data loads ──────────────────────────────────────────────────────────── */
async function loadSpeakers() {
  state.speakers = (await api("/speakers")).speakers;
  renderSpeakers();
  renderSpeakerSelects();
  renderMeetingParticipantFilter();
}

async function loadMeetings() {
  state.allMeetings = (await api("/meetings")).meetings;
  renderMeetingOptions();
  await applyMeetingFilters();
}

function meetingQuery() {
  const params = new URLSearchParams();
  const search = $("meeting-search");
  if (search && search.value.trim()) params.set("q", search.value.trim());
  const unresolved = $("meeting-filter-unresolved");
  if (unresolved && unresolved.checked) params.set("unresolved", "1");
  const participant = $("meeting-filter-participant");
  if (participant && participant.value) params.set("participant_id", participant.value);
  const from = $("meeting-filter-from");
  if (from && from.value) params.set("date_from", `${from.value}T00:00:00+00:00`);
  const to = $("meeting-filter-to");
  if (to && to.value) params.set("date_to", `${to.value}T23:59:59.999999+00:00`);
  const query = params.toString();
  return query ? `?${query}` : "";
}

let filterRequestToken = 0;

async function applyMeetingFilters() {
  const token = ++filterRequestToken;
  const data = await api(`/meetings${meetingQuery()}`);
  if (token !== filterRequestToken) return;
  state.meetings = data.meetings;
  renderMeetings();
}

function renderMeetingParticipantFilter() {
  const select = $("meeting-filter-participant");
  if (!select) return;
  const current = select.value;
  select.innerHTML = ['<option value="">全部</option>']
    .concat(state.speakers.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`))
    .join("");
  select.value = current;
}

function dayLabel(iso) {
  if (!iso) return "未知日期";
  const day = fmtDate(iso).slice(0, 10);
  const today = fmtDate(new Date().toISOString()).slice(0, 10);
  const yesterday = fmtDate(new Date(Date.now() - 86_400_000).toISOString()).slice(0, 10);
  if (day === today) return "今天";
  if (day === yesterday) return "昨天";
  return day;
}

async function openMeeting(meetingId) {
  if (!meetingId) { state.detail = null; renderTranscript(); return; }
  state.detail = await api(`/meetings/${meetingId}`);
  state.selectedCluster = null;
  renderTranscript();
  renderAssign();
}

function allClusters() {
  if (!state.detail) return [];
  const unknown = new Map(state.detail.unknown_clusters.map((c) => [c.cluster_id, c]));
  const names = new Map(state.speakers.map((s) => [s.id, s.name]));
  const seen = new Map();
  for (const link of state.detail.links) {
    const u = unknown.get(link.cluster_id);
    seen.set(link.cluster_id, u ? `Unknown cluster ${u.cluster_id} (${u.segment_count} seg)` : (names.get(link.speaker_id) || `cluster ${link.cluster_id}`));
  }
  for (const [id, u] of unknown) {
    if (!seen.has(id)) seen.set(id, `Unknown cluster ${u.cluster_id} (${u.segment_count} seg)`);
  }
  return [...seen].map(([cluster_id, label]) => ({ cluster_id, label }));
}

/* ── view 2: transcript ──────────────────────────────────────────────────── */
function renderTranscript() {
  const body = $("transcript-body");
  if (!state.detail) {
    renderIntro();
    body.innerHTML = "<p>Select a meeting.</p>";
    return;
  }
  const d = state.detail;
  renderIntro();
  const segments = d.segments.map((s) => {
    const label = s.speaker_name || "unknown";
    const cls = s.speaker_name ? "" : "unknown";
    return `<div class="segment"><span class="time">${fmt(s.start)}–${fmt(s.end)}</span>` +
      `<span class="who ${cls}">${esc(label)}</span><span class="text">${esc(s.text)}</span></div>`;
  }).join("");
  const clusters = d.unknown_clusters.map((c) =>
    `<div class="cluster flagged" data-testid="unknown-cluster-${c.cluster_id}">` +
    `<h3>⚠ Unknown cluster ${c.cluster_id} · ${c.segment_count} segments · ${fmt(c.start)}–${fmt(c.end)}</h3>` +
    `<button type="button" data-resolve="${c.cluster_id}">Resolve this cluster →</button></div>`).join("");
  body.innerHTML =
    `<p><strong>${esc(d.meeting.title)}</strong> · speakers: ${d.speakers.length ? d.speakers.map(esc).join(", ") : "none"}` +
    `${d.unknown_clusters.length ? ` · <strong>${d.unknown_clusters.length} unknown cluster(s)</strong>` : ""}</p>` +
    segments + (clusters || "<p>No unknown clusters.</p>");
  body.querySelectorAll("[data-resolve]").forEach((b) =>
    b.addEventListener("click", () => {
      state.selectedCluster = Number(b.dataset.resolve);
      renderAssign();
      show("assign");
    }));
}

/* ── intro header: editable title + meta line + summary placeholder ──────── */
function renderIntro() {
  const intro = $("transcript-intro");
  if (!state.detail) { intro.hidden = true; intro.innerHTML = ""; return; }
  const d = state.detail;
  const m = d.meeting;
  intro.hidden = false;
  intro.innerHTML =
    `<h3 id="transcript-title" data-testid="transcript-title" class="editable-title" tabindex="0" role="button" title="点击重命名">${esc(m.title || "(untitled)")}</h3>` +
    `<p id="transcript-meta" data-testid="transcript-meta" class="meta">${metaLine(m, d.speakers)}</p>` +
    summaryBlock(d) +
    `<p id="transcript-status" data-testid="transcript-status" class="status" aria-live="polite"></p>` +
    (d.handoff && d.handoff.batch_id
      ? `<button type="button" id="open-wizard" data-testid="open-wizard" class="wizard-cta">` +
        `打开命名向导（${d.unknown_clusters.length} 个未知说话人）</button>`
      : "");
  const generateButton = $("generate-summary");
  if (generateButton) {
    generateButton.addEventListener("click", () => generateSummary(m.id));
  }
  const titleEl = $("transcript-title");
  titleEl.addEventListener("click", () => editMeetingTitle(m.id));
  titleEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      editMeetingTitle(m.id);
    }
  });
  const openWizardBtn = $("open-wizard");
  if (openWizardBtn) {
    openWizardBtn.addEventListener("click", () => {
      openWizard(d.handoff.batch_id).catch((err) => {
        setStatus("wizard-status", `无法加载批次：${err.message}`);
        show("wizard");
      });
    });
  }
}

function summaryBlock(detail) {
  const summary = detail.summary;
  if (!summary) {
    return (
      `<div id="transcript-summary" data-testid="transcript-summary" class="summary">` +
      `<span>摘要尚未生成</span> ` +
      `<button type="button" id="generate-summary" data-testid="generate-summary">生成摘要</button></div>`
    );
  }
  const decisions = (summary.decisions || [])
    .map((item) => `<li>${esc(item)}</li>`)
    .join("");
  const actions = (summary.action_items || [])
    .map(
      (item) =>
        `<li>${esc(item.text)}` +
        `${item.owner ? ` — ${esc(item.owner)}` : ""}` +
        `${item.due ? ` · ${esc(item.due)}` : ""}</li>`
    )
    .join("");
  const chapters = (summary.chapters || [])
    .map((chapter) => `<li>${esc(fmt(chapter.start))} ${esc(chapter.title)}</li>`)
    .join("");
  return (
    `<div id="transcript-summary" data-testid="transcript-summary" class="summary">` +
    `<p class="tldr" data-testid="summary-tldr">${esc(summary.tldr || "")}</p>` +
    (decisions ? `<div class="summary-sec"><strong>关键决策</strong><ul>${decisions}</ul></div>` : "") +
    (actions ? `<div class="summary-sec"><strong>行动项</strong><ul>${actions}</ul></div>` : "") +
    (chapters ? `<div class="summary-sec" data-testid="summary-chapters"><strong>章节</strong><ul>${chapters}</ul></div>` : "") +
    `<p class="ai-note">AI 生成，可编辑 <button type="button" id="generate-summary" data-testid="regenerate-summary">重新生成</button></p></div>`
  );
}

async function generateSummary(meetingId) {
  const button = $("generate-summary");
  if (button) button.disabled = true;
  setStatus("transcript-status", "生成中…（本地模型，可能要一两分钟）");
  try {
    const summary = await api(`/meetings/${meetingId}/summary`, { method: "POST" });
    if (state.detail && state.detail.meeting.id === meetingId) state.detail.summary = summary;
    renderTranscript();
    setStatus("transcript-status", "摘要已生成。");
  } catch (err) {
    setStatus("transcript-status", `摘要生成失败：${err.message}`);
    if (button) button.disabled = false;
  }
}

function metaLine(meeting, speakers) {
  const parts = [];
  if (meeting.date) parts.push(esc(fmtDate(meeting.date)));
  if (meeting.duration_s != null) parts.push(`时长 ${fmt(meeting.duration_s)}`);
  if (meeting.location) parts.push(esc(meeting.location));
  parts.push(`参会人 ${speakers.length ? speakers.map(esc).join("、") : "（未确认）"}`);
  return parts.join(" · ");
}

function editMeetingTitle(meetingId) {
  const title = $("transcript-title");
  if (!title) return;
  const current = state.detail && state.detail.meeting.id === meetingId
    ? state.detail.meeting.title : title.textContent;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "title-input";
  input.value = current;
  input.setAttribute("data-testid", "transcript-title-input");
  title.replaceWith(input);
  input.focus();
  input.select();
  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    const value = input.value.trim();
    let message = "";
    if (save && value && value !== current) {
      try {
        const updated = await api(`/meetings/${meetingId}`, { method: "PATCH", body: { title: value } });
        if (state.detail && state.detail.meeting.id === meetingId) state.detail.meeting.title = updated.title;
        message = `会议标题已更新为「${updated.title}」。`;
        await loadMeetings();
      } catch (err) {
        message = `重命名失败：${err.message}`;
      }
    }
    renderTranscript();
    if (message) setStatus("transcript-status", message);
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    else if (e.key === "Escape") { e.preventDefault(); input.value = current; input.blur(); }
  });
}

/* ── view 3: assign / merge / split ──────────────────────────────────────── */
function renderSpeakerSelects() {
  const options = state.speakers.map((s) => `<option value="${s.id}">${esc(s.name)}${s.organization ? ` (${esc(s.organization)})` : ""}</option>`).join("");
  $("attach-speaker").innerHTML = options || '<option value="">no speakers yet</option>';
  $("merge-target").innerHTML = options || '<option value="">no speakers yet</option>';
  $("merge-source").innerHTML = options || '<option value="">no speakers yet</option>';
  if (state.speakers.length > 1) $("merge-source").selectedIndex = 1;
}

function renderMeetingOptions() {
  const options = state.allMeetings.map((m) => `<option value="${m.id}">#${m.id} · ${esc(m.title)}</option>`).join("");
  $("transcript-meeting").innerHTML = options || '<option value="">no meetings</option>';
  $("split-meeting").innerHTML = options || '<option value="">no meetings</option>';
  if (state.detail) $("transcript-meeting").value = String(state.detail.meeting.id);
  if (state.detail) { $("split-meeting").value = String(state.detail.meeting.id); }
  renderSplitClusters();
}

function renderSplitClusters() {
  const clusters = allClusters();
  $("split-cluster").innerHTML = clusters.map((c) => `<option value="${c.cluster_id}">${esc(c.label)}</option>`).join("") || '<option value="">no clusters</option>';
}

function renderAssign() {
  $("assign-cluster").textContent = state.selectedCluster ? `cluster ${state.selectedCluster}` : "none — pick one in the transcript";
  renderSpeakerSelects();
  renderMeetingOptions();
}

/* ── view 4: speaker directory ───────────────────────────────────────────── */
function renderSpeakers() {
  const rows = $("speaker-rows");
  rows.innerHTML = state.speakers.map((s) =>
    `<tr data-speaker="${s.id}" data-testid="speaker-row-${s.id}">` +
    `<td class="c-name">${esc(s.name)}</td><td class="c-org">${esc(s.organization || "")}</td>` +
    `<td class="c-title" data-testid="speaker-title-${s.id}">${esc(s.title || "")}</td>` +
    `<td class="c-notes">${esc(s.notes || "")}</td><td data-testid="speaker-vp-${s.id}">${s.voiceprint_count}</td>` +
    `<td class="c-consent" data-testid="speaker-consent-${s.id}">${consentCell(s)}</td>` +
    `<td><button type="button" class="secondary" data-edit="${s.id}">Edit</button> ` +
    `<button type="button" class="secondary" data-export="${s.id}">导出</button> ` +
    `<button type="button" class="secondary" data-reenroll="${s.id}">重新登记</button> ` +
    `<button type="button" class="danger" data-delete="${s.id}">Delete</button></td></tr>`).join("") ||
    '<tr><td colspan="7">No speakers yet.</td></tr>';
  rows.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => editSpeaker(Number(b.dataset.edit))));
  rows.querySelectorAll("[data-export]").forEach((b) => b.addEventListener("click", () => exportSpeaker(Number(b.dataset.export))));
  rows.querySelectorAll("[data-reenroll]").forEach((b) => b.addEventListener("click", () => reEnrollSpeaker(Number(b.dataset.reenroll))));
  rows.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", () => openPurge(Number(b.dataset.delete))));
}

function consentCell(speaker) {
  if (!speaker.consent_granted_at) return '<span class="muted">未记录同意</span>';
  if (speaker.consent_revoked_at) {
    const granted = esc(fmtDate(speaker.consent_granted_at).slice(0, 10));
    return `同意 ${granted} · <span class="badge danger">已撤销</span>`;
  }
  const granted = fmtDate(speaker.consent_granted_at).slice(0, 10);
  const retention = speaker.consent_expired
    ? '<span class="badge danger">保留期已到</span>'
    : `保留至 ${esc(fmtDate(speaker.retention_until).slice(0, 10))}`;
  return `同意 ${esc(granted)} · <span data-testid="speaker-retention-${speaker.id}">${retention}</span>`;
}

async function exportSpeaker(id) {
  try {
    const res = await fetch(`/speakers/${id}/export`, { credentials: "same-origin" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const url = URL.createObjectURL(await res.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `speaker-${id}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    setStatus("speaker-status", `已导出说话人 ${id}。`);
  } catch (err) {
    setStatus("speaker-status", `导出失败：${err.message}`);
  }
}

async function reEnrollSpeaker(id) {
  if (!state.selectedCluster) {
    setStatus("speaker-status", "先在 Transcript 里选中要用于重新登记的 cluster。");
    return;
  }
  const speaker = state.speakers.find((s) => s.id === id);
  const name = speaker ? speaker.name : `#${id}`;
  if (!window.confirm(`用选中的 cluster ${state.selectedCluster} 重新登记「${name}」？将清除其过时的声纹。`)) {
    return;
  }
  try {
    const result = await api(`/speakers/${id}/re-enroll`, {
      method: "POST",
      body: { cluster_id: state.selectedCluster },
    });
    setStatus("speaker-status", `已用 cluster ${result.cluster_id} 重新登记。`);
    await refreshAfterMutation();
  } catch (err) {
    setStatus("speaker-status", `重新登记失败：${err.message}`);
  }
}

function editSpeaker(id) {
  const speaker = state.speakers.find((s) => s.id === id);
  const row = document.querySelector(`tr[data-speaker="${id}"]`);
  row.innerHTML =
    `<td><input value="${esc(speaker.name)}" data-field="name"></td>` +
    `<td><input value="${esc(speaker.organization || "")}" data-field="organization"></td>` +
    `<td><input value="${esc(speaker.title || "")}" data-field="title"></td>` +
    `<td><input value="${esc(speaker.notes || "")}" data-field="notes"></td>` +
    `<td>${speaker.voiceprint_count}</td>` +
    `<td class="c-consent">${consentCell(speaker)}</td>` +
    `<td><button type="button" data-save="${id}">Save</button> <button type="button" class="secondary" data-cancel="${id}">Cancel</button></td>`;
  row.querySelector("[data-save]").addEventListener("click", async () => {
    const body = {};
    row.querySelectorAll("[data-field]").forEach((i) => { body[i.dataset.field] = i.value; });
    await api(`/speakers/${id}`, { method: "PATCH", body });
    setStatus("speaker-status", `Saved ${speaker.name}.`);
    await loadSpeakers();
  });
  row.querySelector("[data-cancel]").addEventListener("click", renderSpeakers);
}

/* ── delete with biometric-purge confirmation ────────────────────────────── */
function openPurge(id) {
  const speaker = state.speakers.find((s) => s.id === id);
  state.purge = speaker;
  $("purge-message").textContent =
    `Deleting "${speaker.name}" permanently purges ${speaker.voiceprint_count} biometric voiceprint(s) ` +
    `and unlinks this speaker from every meeting. This cannot be undone.`;
  $("purge-confirm").value = "";
  $("purge-confirm").placeholder = speaker.name;
  $("purge-dialog").showModal();
}

/* ── view 5: meeting history ─────────────────────────────────────────────── */
function renderMeetings() {
  const rows = $("meeting-rows");
  if (!state.meetings.length) {
    rows.innerHTML = '<tr><td colspan="4">没有匹配的会议。</td></tr>';
    return;
  }
  let html = "";
  let group = null;
  for (const m of state.meetings) {
    const label = dayLabel(m.date);
    if (label !== group) {
      html += `<tr class="group-row"><td colspan="4">${esc(label)}</td></tr>`;
      group = label;
    }
    const participants = m.participants && m.participants.length
      ? m.participants.map(esc).join("、")
      : "（未确认）";
    const badge = m.unknown_count > 0
      ? ` <span class="badge danger">未确认 ${m.unknown_count}</span>`
      : "";
    const duration = m.duration_s != null ? ` · ${fmt(m.duration_s)}` : "";
    html +=
      `<tr data-meeting="${m.id}" data-testid="meeting-row-${m.id}">` +
      `<td class="c-title editable-title" data-testid="meeting-title-${m.id}" data-title-edit="${m.id}" tabindex="0" role="button" title="点击重命名">${esc(m.title)}</td>` +
      `<td>${esc(fmtDate(m.date).slice(0, 10))}${duration}</td>` +
      `<td data-testid="meeting-speakers-${m.id}">${participants}${badge}</td>` +
      `<td><button type="button" class="secondary" data-open="${m.id}">View transcript</button></td></tr>`;
  }
  rows.innerHTML = html;
  rows.querySelectorAll("[data-open]").forEach((b) =>
    b.addEventListener("click", async () => {
      await openMeeting(Number(b.dataset.open));
      show("transcript");
    }));
  rows.querySelectorAll("[data-title-edit]").forEach((cell) => {
    cell.addEventListener("click", () => editMeetingTitleInline(Number(cell.dataset.titleEdit), cell));
    cell.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        editMeetingTitleInline(Number(cell.dataset.titleEdit), cell);
      }
    });
  });
}

function editMeetingTitleInline(meetingId, cell) {
  if (cell.querySelector("input")) return;
  const meeting = state.meetings.find((m) => m.id === meetingId);
  const current = meeting ? meeting.title : cell.textContent;
  const input = document.createElement("input");
  input.type = "text";
  input.value = current;
  input.setAttribute("data-testid", `meeting-title-input-${meetingId}`);
  cell.textContent = "";
  cell.appendChild(input);
  input.focus();
  input.select();
  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    const value = input.value.trim();
    if (save && value && value !== current) {
      try {
        await api(`/meetings/${meetingId}`, { method: "PATCH", body: { title: value } });
        setStatus("meeting-status", `会议标题已更新为「${value}」。`);
      } catch (err) {
        setStatus("meeting-status", `重命名失败：${err.message}`);
      }
    }
    await loadMeetings();
    if (state.detail && state.detail.meeting.id === meetingId) await openMeeting(meetingId);
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    else if (e.key === "Escape") { e.preventDefault(); input.value = current; input.blur(); }
  });
}

/* ── view 6: speaker naming wizard (deep-link view, not a nav tab) ───────── */
async function openWizard(batchId) {
  const payload = await api(`/speaker-batches/${batchId}`);
  state.wizard = { batch: payload.batch, items: payload.items.filter((item) => item.resolution == null) };
  state.wizardIndex = 0;
  state.wizardDecisions = {};
  for (const item of state.wizard.items) {
    state.wizardDecisions[item.cluster_id] = {
      action: "skip",
      name: "",
      organization: "",
      title: "",
      speaker_id: item.suggested_speaker_id != null ? item.suggested_speaker_id : null,
      remember: false,
    };
  }
  renderWizard();
  show("wizard");
}

function currentWizardItem() {
  return state.wizard ? state.wizard.items[state.wizardIndex] || null : null;
}

function speakerOptions(selectedId) {
  return ['<option value="">选择已有说话人…</option>']
    .concat(state.speakers.map((s) =>
      `<option value="${s.id}"${s.id === selectedId ? " selected" : ""}>` +
      `${esc(s.name)}${s.organization ? ` (${esc(s.organization)})` : ""}</option>`))
    .join("");
}

function renderWizard() {
  const card = $("wizard-card");
  const w = state.wizard;
  if (!w || !w.items.length) {
    $("wizard-progress").textContent = "";
    card.innerHTML = "<p>没有待处理的说话人。</p>";
    return;
  }
  const item = currentWizardItem();
  const d = state.wizardDecisions[item.cluster_id];
  const isLast = state.wizardIndex === w.items.length - 1;
  $("wizard-progress").textContent = `第 ${state.wizardIndex + 1}/${w.items.length} 个`;
  card.innerHTML =
    `<div class="wizard-card-inner">` +
    `<div class="wizard-cluster" data-testid="wizard-cluster-label">` +
    `${esc(item.label || `cluster ${item.cluster_id}`)} · ${fmt(item.start)}–${fmt(item.end)} · 发言 ${item.segment_count} 段</div>` +
    (item.similarity != null
      ? `<div class="wizard-hint" data-testid="wizard-similarity">最接近的已有声纹相似度 ${Number(item.similarity).toFixed(2)}（仅供参考）</div>`
      : "") +
    `<audio class="wizard-sample" data-testid="wizard-sample" controls preload="none" src="${esc(item.sample_url)}"></audio>` +
    `<label class="wizard-choice"><input type="radio" name="wizard-action" value="attach" data-testid="wizard-action-attach"${d.action === "attach" ? " checked" : ""}> 关联已有说话人</label>` +
    `<select data-testid="wizard-attach-select" aria-label="关联已有说话人">${speakerOptions(d.speaker_id)}</select>` +
    `<label class="wizard-choice"><input type="radio" name="wizard-action" value="enroll" data-testid="wizard-action-enroll"${d.action === "enroll" ? " checked" : ""}> 新建说话人</label>` +
    `<div class="wizard-fields">` +
    `<input data-testid="wizard-new-name" aria-label="姓名" placeholder="姓名" value="${esc(d.name)}">` +
    `<input data-testid="wizard-new-org" aria-label="单位" placeholder="单位" value="${esc(d.organization)}">` +
    `<input data-testid="wizard-new-title" aria-label="职务" placeholder="职务" value="${esc(d.title)}">` +
    `</div>` +
    `<label class="wizard-choice"><input type="radio" name="wizard-action" value="skip" data-testid="wizard-action-skip"${d.action === "skip" ? " checked" : ""}> 跳过</label>` +
    `<label class="remember"><input type="checkbox" data-testid="wizard-remember"${d.remember ? " checked" : ""}> 同时记住此声纹用于以后会议</label>` +
    `<p class="consent">勾选后将保存声纹（生物特征数据）用于以后会议识别，可在声文库删除。未勾选仅用于本次转写，不保存声纹。</p>` +
    `<div class="wizard-nav">` +
    `<button type="button" data-testid="wizard-prev" class="secondary"${state.wizardIndex === 0 ? " disabled" : ""}>上一步</button>` +
    `<button type="button" data-testid="wizard-next"${isLast ? " hidden" : ""}>下一步</button>` +
    `<button type="button" data-testid="wizard-submit"${isLast ? "" : " hidden"}>完成并继续</button>` +
    `</div></div>`;
  card.querySelector('[data-testid="wizard-prev"]').addEventListener("click", () => {
    captureWizardCard();
    state.wizardIndex = Math.max(0, state.wizardIndex - 1);
    renderWizard();
  });
  card.querySelector('[data-testid="wizard-next"]').addEventListener("click", () => {
    captureWizardCard();
    state.wizardIndex = Math.min(w.items.length - 1, state.wizardIndex + 1);
    renderWizard();
  });
  card.querySelector('[data-testid="wizard-submit"]').addEventListener("click", submitWizard);
}

function captureWizardCard() {
  const item = currentWizardItem();
  const card = $("wizard-card");
  if (!item || !card) return;
  const d = state.wizardDecisions[item.cluster_id];
  const checked = card.querySelector('input[name="wizard-action"]:checked');
  d.action = checked ? checked.value : "skip";
  d.name = card.querySelector('[data-testid="wizard-new-name"]').value;
  d.organization = card.querySelector('[data-testid="wizard-new-org"]').value;
  d.title = card.querySelector('[data-testid="wizard-new-title"]').value;
  const select = card.querySelector('[data-testid="wizard-attach-select"]');
  d.speaker_id = select.value ? Number(select.value) : null;
  d.remember = card.querySelector('[data-testid="wizard-remember"]').checked;
}

async function submitWizard() {
  if (!state.wizard || state.wizardSubmitting) return;
  state.wizardSubmitting = true;
  const submitButton = $("wizard-submit");
  if (submitButton) submitButton.disabled = true;
  captureWizardCard();
  for (let i = 0; i < state.wizard.items.length; i += 1) {
    const d = state.wizardDecisions[state.wizard.items[i].cluster_id];
    const missingName = d.action === "enroll" && !String(d.name || "").trim();
    const missingSpeaker = d.action === "attach" && d.speaker_id == null;
    if (missingName || missingSpeaker) {
      state.wizardIndex = i;
      renderWizard();
      if (missingName) $("wizard-new-name").focus();
      setStatus("wizard-status", missingName ? "新建说话人需要填写姓名。" : "请选择要关联的已有说话人。");
      return;
    }
  }
  const batchId = state.wizard.batch.id;
  const items = state.wizard.items.map((item) => {
    const d = state.wizardDecisions[item.cluster_id];
    const decision = { cluster_id: item.cluster_id, action: d.action, remember: Boolean(d.remember) };
    if (d.action === "enroll") {
      decision.name = String(d.name || "").trim();
      decision.organization = String(d.organization || "").trim() || null;
      decision.title = String(d.title || "").trim() || null;
    } else if (d.action === "attach") {
      decision.speaker_id = d.speaker_id;
    }
    return decision;
  });
  try {
    const result = await api(`/speaker-batches/${batchId}/resolve`, { method: "POST", body: { items } });
    await loadSpeakers();
    await loadMeetings();
    if (result.remaining > 0) {
      await openWizard(batchId);
      setStatus("wizard-status", `已处理 ${result.resolved} 个，还剩 ${result.remaining} 个待命名，请继续。`);
      return;
    }
    setStatus("wizard-status", `已处理 ${result.resolved} 个说话人，批次已完成。`);
    await openMeeting(result.batch.meeting_id);
    history.replaceState({}, "", location.pathname);
    show("transcript");
  } catch (err) {
    setStatus("wizard-status", `提交失败：${err.message}`);
  } finally {
    state.wizardSubmitting = false;
    const button = $("wizard-submit");
    if (button) button.disabled = false;
  }
}

async function handleDeepLink() {
  const params = new URLSearchParams(location.search);
  if (params.get("task") !== "speakers") return;
  let batchId = params.get("batch");
  if (!batchId && params.get("meeting")) {
    try {
      const detail = await api(`/meetings/${params.get("meeting")}`);
      batchId = detail.handoff ? detail.handoff.batch_id : null;
    } catch (err) {
      setStatus("wizard-status", `无法加载会议：${err.message}`);
      show("wizard");
      return;
    }
  }
  if (batchId && /^\d+$/.test(String(batchId))) {
    try {
      await openWizard(Number(batchId));
      return;
    } catch (err) {
      setStatus("wizard-status", `无法加载批次：${err.message}`);
      show("wizard");
      return;
    }
  }
  setStatus("wizard-status", "没有待处理的说话人批次。");
  show("wizard");
}

/* ── form handlers ───────────────────────────────────────────────────────── */
function bindForms() {
  document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

  $("upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = $("upload-file").files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    if ($("upload-title").value.trim()) form.append("title", $("upload-title").value.trim());
    $("upload-btn").disabled = true;
    setStatus("upload-status", "Transcribing… diarization + ASR can take up to a minute.");
    try {
      const result = await api("/meetings", { method: "POST", body: form });
      setStatus("upload-status", `Uploaded meeting #${result.meeting_id} (${result.segments.length} segments, ${result.unknown_clusters.length} unknown cluster(s)).`);
      await loadMeetings();
      await loadSpeakers();
      await openMeeting(result.meeting_id);
      show("transcript");
    } catch (err) {
      setStatus("upload-status", `Upload failed: ${err.message}`);
    } finally {
      $("upload-btn").disabled = false;
    }
  });

  $("transcript-meeting").addEventListener("change", (e) => openMeeting(Number(e.target.value)));
  $("split-meeting").addEventListener("change", async (e) => { await openMeeting(Number(e.target.value)); });

  const searchInput = $("meeting-search");
  let searchTimer = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { applyMeetingFilters().catch(() => {}); }, 250);
  });
  $("meeting-filter-unresolved").addEventListener("change", () => {
    applyMeetingFilters().catch(() => {});
  });
  $("meeting-filter-participant").addEventListener("change", () => {
    applyMeetingFilters().catch(() => {});
  });
  $("meeting-filter-from").addEventListener("change", () => {
    applyMeetingFilters().catch(() => {});
  });
  $("meeting-filter-to").addEventListener("change", () => {
    applyMeetingFilters().catch(() => {});
  });
  $("meeting-filters").addEventListener("submit", (e) => e.preventDefault());

  $("enroll-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.selectedCluster) return setStatus("assign-status", "Pick a cluster in the transcript first.");
    try {
      const result = await api("/speakers/enroll", {
        method: "POST",
        body: { name: $("enroll-name").value, organization: $("enroll-org").value || null, title: $("enroll-title").value || null, notes: $("enroll-notes").value || null, cluster_id: state.selectedCluster },
      });
      setStatus("assign-status", `Created speaker "${result.name}" from cluster ${state.selectedCluster}.`);
      await refreshAfterMutation();
    } catch (err) { setStatus("assign-status", `Enroll failed: ${err.message}`); }
  });

  $("attach-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const speakerId = $("attach-speaker").value;
    if (!state.selectedCluster) return setStatus("assign-status", "Pick a cluster in the transcript first.");
    if (!speakerId) return setStatus("assign-status", "No existing speaker to assign to.");
    try {
      const result = await api(`/speakers/${speakerId}/voiceprints`, { method: "POST", body: { cluster_id: state.selectedCluster } });
      setStatus("assign-status", `Attached cluster ${state.selectedCluster} to speaker ${result.speaker_id} (no new speaker row).`);
      await refreshAfterMutation();
    } catch (err) { setStatus("assign-status", `Assign failed: ${err.message}`); }
  });

  $("merge-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const target = $("merge-target").value, source = $("merge-source").value;
    if (!target || !source) return setStatus("assign-status", "Need two speakers to merge.");
    try {
      const result = await api(`/speakers/${target}/merge`, { method: "POST", body: { source_speaker_id: Number(source) } });
      setStatus("assign-status", `Merged source ${result.source_speaker_id} into target ${result.target_speaker_id} (voiceprints ${result.voiceprints_moved}, links ${result.links_moved}, segments ${result.segments_moved}).`);
      await refreshAfterMutation();
    } catch (err) { setStatus("assign-status", `Merge failed: ${err.message}`); }
  });

  $("split-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const meetingId = $("split-meeting").value, clusterId = $("split-cluster").value, at = Number($("split-at").value);
    if (!meetingId || !clusterId || !(at >= 0)) return setStatus("assign-status", "Choose a cluster and a split time.");
    try {
      const result = await api(`/meetings/${meetingId}/clusters/${clusterId}/split`, { method: "POST", body: { at } });
      setStatus("assign-status", `Split cluster ${result.cluster_id}; ${result.moved_segments} segment(s) moved to new cluster ${result.new_cluster_id}.`);
      await openMeeting(Number(meetingId));
      await loadSpeakers();
    } catch (err) { setStatus("assign-status", `Split failed: ${err.message}`); }
  });

  $("wizard-exit").addEventListener("click", () => show(state.detail ? "transcript" : "meetings"));

  $("purge-cancel").addEventListener("click", () => $("purge-dialog").close());
  $("purge-delete").addEventListener("click", async () => {
    const speaker = state.purge;
    if (!speaker) return;
    if ($("purge-confirm").value.trim() !== speaker.name) {
      setStatus("speaker-status", "Confirmation name does not match — deletion cancelled.");
      return;
    }
    try {
      await api(`/speakers/${speaker.id}`, { method: "DELETE" });
      setStatus("speaker-status", `Deleted "${speaker.name}" and purged its voiceprints.`);
      $("purge-dialog").close();
      await refreshAfterMutation();
    } catch (err) { setStatus("speaker-status", `Delete failed: ${err.message}`); }
  });
}

async function refreshAfterMutation() {
  await loadSpeakers();
  await loadMeetings();
  if (state.detail) await openMeeting(state.detail.meeting.id);
}

/* ── init ────────────────────────────────────────────────────────────────── */
async function init() {
  bindForms();
  keepalive();
  setInterval(keepalive, KEEPALIVE_MS);
  await loadSpeakers();
  await loadMeetings();
  await handleDeepLink();
}

document.addEventListener("DOMContentLoaded", init);
