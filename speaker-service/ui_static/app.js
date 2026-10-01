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
const state = { speakers: [], meetings: [], detail: null, selectedCluster: null, purge: null };

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (sec) => {
  const s = Math.max(0, Number(sec) || 0);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;
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
}

/* ── data loads ──────────────────────────────────────────────────────────── */
async function loadSpeakers() {
  state.speakers = (await api("/speakers")).speakers;
  renderSpeakers();
  renderSpeakerSelects();
}

async function loadMeetings() {
  state.meetings = (await api("/meetings")).meetings;
  renderMeetingOptions();
  renderMeetings();
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
  if (!state.detail) { body.innerHTML = "<p>Select a meeting.</p>"; return; }
  const d = state.detail;
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

/* ── view 3: assign / merge / split ──────────────────────────────────────── */
function renderSpeakerSelects() {
  const options = state.speakers.map((s) => `<option value="${s.id}">${esc(s.name)}${s.organization ? ` (${esc(s.organization)})` : ""}</option>`).join("");
  $("attach-speaker").innerHTML = options || '<option value="">no speakers yet</option>';
  $("merge-target").innerHTML = options || '<option value="">no speakers yet</option>';
  $("merge-source").innerHTML = options || '<option value="">no speakers yet</option>';
  if (state.speakers.length > 1) $("merge-source").selectedIndex = 1;
}

function renderMeetingOptions() {
  const options = state.meetings.map((m) => `<option value="${m.id}">#${m.id} · ${esc(m.title)}</option>`).join("");
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
    `<td class="c-notes">${esc(s.notes || "")}</td><td data-testid="speaker-vp-${s.id}">${s.voiceprint_count}</td>` +
    `<td><button type="button" class="secondary" data-edit="${s.id}">Edit</button> ` +
    `<button type="button" class="danger" data-delete="${s.id}">Delete</button></td></tr>`).join("") ||
    '<tr><td colspan="5">No speakers yet.</td></tr>';
  rows.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => editSpeaker(Number(b.dataset.edit))));
  rows.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", () => openPurge(Number(b.dataset.delete))));
}

function editSpeaker(id) {
  const speaker = state.speakers.find((s) => s.id === id);
  const row = document.querySelector(`tr[data-speaker="${id}"]`);
  row.innerHTML =
    `<td><input value="${esc(speaker.name)}" data-field="name"></td>` +
    `<td><input value="${esc(speaker.organization || "")}" data-field="organization"></td>` +
    `<td><input value="${esc(speaker.notes || "")}" data-field="notes"></td>` +
    `<td>${speaker.voiceprint_count}</td>` +
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
  rows.innerHTML = state.meetings.map((m) =>
    `<tr data-meeting="${m.id}" data-testid="meeting-row-${m.id}"><td>${esc(m.title)}</td><td>${esc(m.date || "")}</td>` +
    `<td data-testid="meeting-speakers-${m.id}">…</td>` +
    `<td><button type="button" class="secondary" data-open="${m.id}">View transcript</button></td></tr>`).join("") ||
    '<tr><td colspan="4">No meetings yet.</td></tr>';
  rows.querySelectorAll("[data-open]").forEach((b) =>
    b.addEventListener("click", async () => {
      await openMeeting(Number(b.dataset.open));
      show("transcript");
    }));
  state.meetings.forEach(async (m) => {
    try {
      const detail = await api(`/meetings/${m.id}`);
      const cell = document.querySelector(`[data-testid="meeting-speakers-${m.id}"]`);
      if (cell) cell.textContent = detail.speakers.length ? detail.speakers.join(", ") : "none";
    } catch (err) { /* leave placeholder */ }
  });
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

  $("enroll-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.selectedCluster) return setStatus("assign-status", "Pick a cluster in the transcript first.");
    try {
      const result = await api("/speakers/enroll", {
        method: "POST",
        body: { name: $("enroll-name").value, organization: $("enroll-org").value || null, notes: $("enroll-notes").value || null, cluster_id: state.selectedCluster },
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
}

document.addEventListener("DOMContentLoaded", init);
