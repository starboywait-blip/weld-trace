"use strict";
const $ = (id) => document.getElementById(id);
const store = {
  get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
};
let CLASSES = [];
let OPT = null; // pick-lists from the server: item types, materials, processes, electrodes
const lab = (list, v) => ((list || []).find((x) => x.value === v) || {}).label || v;
const itemLabel = (v) => lab(OPT && OPT.item_types, v);
const otherText = (v) => (v || "").startsWith("Other:") ? v.slice(6).trim() : null;
const materialText = (v) => !v ? "not recorded" : (otherText(v) ?? lab(OPT && OPT.materials, v));
const electrodeText = (v) => !v ? "not recorded" : (otherText(v) ?? v);
let lastInspection = null;

// ---------- helpers ----------
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function pill(status) { return el("span", String(status).replace(/_/g, " "), "pill " + status); }
function headers() { const k = store.get("key"); return k ? { "X-API-Key": k } : {}; }
async function api(path, opts = {}) {
  const r = await fetch("/api" + path, { ...opts, headers: { ...headers(), ...(opts.headers || {}) } });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r;
}
const jget = async (p) => (await api(p)).json();
const jpost = async (p, body) => (await api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
function fill(sel, items, label, value = (x) => x.id) {
  sel.replaceChildren();
  items.forEach((x) => { const o = el("option", label(x)); o.value = value(x); sel.append(o); });
  if (!items.length) sel.append(el("option", "(none yet)"));
}

// ---------- tabs ----------
document.querySelectorAll("nav button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("on", x === b));
  ["inspect", "jobs", "welders", "settings"].forEach((t) => $("tab-" + t).classList.toggle("hide", t !== b.dataset.tab));
  if (b.dataset.tab === "jobs") loadJobs();
  if (b.dataset.tab === "welders") loadWelders();
});

// ---------- online status ----------
function netState() { $("net").textContent = navigator.onLine ? "online" : "offline"; $("net").classList.toggle("off", !navigator.onLine); }
addEventListener("online", () => { netState(); flushQueue(); });
addEventListener("offline", netState);

// ---------- IndexedDB offline queue ----------
const dbp = new Promise((res, rej) => {
  const r = indexedDB.open("weldtrace", 1);
  r.onupgradeneeded = () => r.result.createObjectStore("q", { keyPath: "qid", autoIncrement: true });
  r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
});
async function qop(mode, fn) {
  const db = await dbp;
  return new Promise((res, rej) => {
    const tx = db.transaction("q", mode); const out = fn(tx.objectStore("q"));
    tx.oncomplete = () => res(out.result); tx.onerror = () => rej(tx.error);
  });
}
const qAdd = (item) => qop("readwrite", (s) => s.add(item));
const qAll = () => qop("readonly", (s) => s.getAll());
const qDel = (id) => qop("readwrite", (s) => s.delete(id));

async function renderQueue() {
  let items = [];
  try { items = await qAll(); } catch {}
  $("queueCard").classList.toggle("hide", !items.length);
  $("queue").textContent = items.length + " photo(s) saved on this phone, will upload when online.";
}
async function flushQueue() {
  if (!navigator.onLine) return;
  let items = [];
  try { items = await qAll(); } catch { return; }
  for (const it of items) {
    try {
      const fd = new FormData();
      fd.append("weld_id", it.weld_id); fd.append("inspector", it.inspector);
      fd.append("captured_at", it.captured_at);
      if (it.lat != null) { fd.append("latitude", it.lat); fd.append("longitude", it.lon); }
      fd.append("photo", it.photo, "weld.jpg");
      await api("/inspections", { method: "POST", body: fd });
      await qDel(it.qid);
    } catch (e) { break; }
  }
  renderQueue();
}

// ---------- data loading ----------
async function loadAll() {
  try {
    const h = await jget("/health");
    CLASSES = h.classes;
    fill($("finalLabel"), CLASSES, (c) => c.replace(/_/g, " "), (c) => c);
    $("modelInfo").textContent = h.model_loaded
      ? "Defect model loaded: " + h.model_version
      : "No trained model loaded. Every photo goes to manual inspection until one is trained.";
    OPT = await jget("/options");
    fillOptions();
    await Promise.all([loadJobSelect(), loadWelderSelect()]);
  } catch (e) { $("sendMsg").textContent = "Cannot reach server: " + e.message; }
}

// ---------- material / process / electrode pick-lists ----------
function fillOptions() {
  fill($("jType"), OPT.item_types, (t) => t.label, (t) => t.value);
  fill($("newMaterial"), [{ value: "", label: "Select the material…" }, ...OPT.materials], (m) => m.label, (m) => m.value);
  fill($("newProcess"), OPT.processes, (p) => p, (p) => p);
  fillElectrodes();
}
function fillElectrodes() {
  const list = OPT.electrodes[$("newProcess").value] || [];
  fill($("newElectrode"), [{ value: "", label: "Select the electrode…" }, ...list], (e) => e.label, (e) => e.value);
  syncOther();
}
function syncOther() {
  $("newMaterialOther").classList.toggle("hide", $("newMaterial").value !== "other");
  $("newElectrodeOther").classList.toggle("hide", $("newElectrode").value !== "other");
  showCompat();
}
// The value to store: the list key, or "Other: <typed text>" (empty if Other is chosen but not described).
function chosen(selId, otherId) {
  const v = $(selId).value;
  if (v !== "other") return v;
  const t = $(otherId).value.trim();
  return t ? "Other: " + t : "";
}
let compatTimer;
function showCompat() {
  clearTimeout(compatTimer);
  compatTimer = setTimeout(async () => {
    const m = chosen("newMaterial", "newMaterialOther"), e = chosen("newElectrode", "newElectrodeOther");
    const box = $("compat");
    if (!m || !e) { box.replaceChildren(); return; }
    try {
      const q = `material=${encodeURIComponent(m)}&process=${encodeURIComponent($("newProcess").value)}&electrode=${encodeURIComponent(e)}`;
      const r = await jget("/compat?" + q);
      box.replaceChildren(...r.warnings.map((w) => el("div", "⚠ " + w)));
    } catch { box.replaceChildren(); }
  }, 250);
}
$("newProcess").onchange = fillElectrodes;
$("newMaterial").onchange = syncOther;
$("newElectrode").onchange = syncOther;
$("newMaterialOther").oninput = showCompat;
$("newElectrodeOther").oninput = showCompat;

async function loadJobSelect() {
  const jobs = await jget("/jobs");
  fill($("job"), jobs, (j) => `${j.ref} (${itemLabel(j.item_type)})`);
  await loadWeldSelect();
}
async function loadWeldSelect() {
  const id = $("job").value;
  if (!id || isNaN(id)) return fill($("weld"), [], () => "");
  const welds = await jget("/welds?job_id=" + id);
  fill($("weld"), welds, (w) => `${w.joint_label}: ${w.welder_name} [${w.status.replace(/_/g, " ")}]`);
}
async function loadWelderSelect() {
  fill($("newWelder"), await jget("/welders"), (w) => `${w.name} (${w.badge})`);
}
$("job").onchange = loadWeldSelect;

$("addWeld").onclick = async () => {
  const base_material = chosen("newMaterial", "newMaterialOther");
  const electrode = chosen("newElectrode", "newElectrodeOther");
  if (!$("job").value || isNaN($("job").value)) return alert("Create or pick a job first (Records tab).");
  if (!$("newJoint").value.trim()) return alert("Enter a joint label.");
  if (isNaN($("newWelder").value)) return alert("Add a welder first (Welders tab).");
  if (!base_material) return alert("Select the material being welded, or describe it under Other.");
  if (!electrode) return alert("Select the electrode used, or describe it under Other.");
  try {
    const r = await jpost("/welds", { job_id: +$("job").value, joint_label: $("newJoint").value.trim(),
      welder_id: +$("newWelder").value, process: $("newProcess").value, base_material, electrode });
    // Keep material / process / electrode selected: joints on one item usually share them.
    $("newJoint").value = ""; await loadWeldSelect();
    if (r.warnings && r.warnings.length) alert("Joint saved. Please check:\n\n" + r.warnings.join("\n\n"));
  } catch (e) { alert(e.message); }
};

// ---------- capture & upload ----------
$("photo").onchange = () => {
  const f = $("photo").files[0];
  $("send").disabled = !f;
  if (f) { $("prev").src = URL.createObjectURL(f); $("prev").classList.remove("hide"); }
};
function getPosition() {
  return new Promise((res) => {
    if (!navigator.geolocation) return res(null);
    navigator.geolocation.getCurrentPosition((p) => res(p.coords), () => res(null), { timeout: 4000 });
  });
}
async function shrink(file, maxSide = 1600) {
  // Resize before upload to save mobile data; keeps enough detail for porosity and undercut.
  try {
    const bmp = await createImageBitmap(file);
    const s = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas"); c.width = Math.round(bmp.width * s); c.height = Math.round(bmp.height * s);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    return await new Promise((res) => c.toBlob((b) => res(b || file), "image/jpeg", 0.9));
  } catch { return file; }
}

$("send").onclick = async () => {
  const weldId = $("weld").value;
  if (!weldId || isNaN(weldId)) { $("sendMsg").textContent = "Pick or add a joint first."; return; }
  if (!store.get("insp")) { $("sendMsg").textContent = "Set your name in Settings first."; return; }
  $("send").disabled = true; $("sendMsg").textContent = "Working…";
  const blob = await shrink($("photo").files[0]);
  const pos = await getPosition();
  const item = { weld_id: weldId, inspector: store.get("insp"), captured_at: new Date().toISOString(),
    lat: pos ? pos.latitude : null, lon: pos ? pos.longitude : null, photo: blob };
  try {
    if (!navigator.onLine) throw new Error("offline");
    const fd = new FormData();
    fd.append("weld_id", item.weld_id); fd.append("inspector", item.inspector); fd.append("captured_at", item.captured_at);
    if (pos) { fd.append("latitude", pos.latitude); fd.append("longitude", pos.longitude); }
    fd.append("photo", blob, "weld.jpg");
    const r = await (await api("/inspections", { method: "POST", body: fd })).json();
    showResult(r); $("sendMsg").textContent = "";
  } catch (e) {
    if (!navigator.onLine || e.message === "offline" || e instanceof TypeError) {
      await qAdd(item); renderQueue();
      $("sendMsg").textContent = "No connection. Photo saved on this phone and will upload automatically.";
    } else { $("sendMsg").textContent = "Error: " + e.message; }
  }
  $("send").disabled = false;
};

function showResult(r) {
  lastInspection = r;
  $("resultCard").classList.remove("hide");
  const box = $("result"); box.replaceChildren();
  const top = el("div"); top.style.cssText = "display:flex;justify-content:space-between;align-items:center";
  top.append(el("strong", "System suggestion"), pill(r.auto_decision));
  box.append(top);
  if (r.auto_decision === "retake") {
    box.append(el("p", r.photo_quality + " Take a new photo.", "mute"));
    $("signoff").classList.add("hide"); return;
  }
  if (r.predicted_label) {
    box.append(el("p", `${r.predicted_label.replace(/_/g, " ")} (${Math.round(r.confidence * 100)}% confidence)`));
    Object.entries(r.probs).sort((a, b) => b[1] - a[1]).slice(0, 4).forEach(([k, v]) => {
      box.append(el("div", `${k.replace(/_/g, " ")} ${Math.round(v * 100)}%`, "mute"));
      const bar = el("div", "", "bar"); const i = el("i"); i.style.width = v * 100 + "%"; bar.append(i); box.append(bar);
    });
    $("finalLabel").value = r.predicted_label;
  } else {
    box.append(el("p", "No trained model is loaded, so this needs your visual assessment.", "mute"));
  }
  box.append(el("p", "The suggestion does not release a weld. Your sign-off is the record.", "mute"));
  $("signoff").classList.remove("hide");
}

async function signoff(decision) {
  if (!lastInspection) return;
  try {
    await jpost(`/inspections/${lastInspection.id}/signoff`, { inspector: store.get("insp"),
      final_label: $("finalLabel").value, final_decision: decision, notes: $("notes").value });
    $("result").append(el("p", "Signed off: " + decision.toUpperCase(), "pill " + decision));
    $("signoff").classList.add("hide"); $("notes").value = ""; $("photo").value = ""; $("prev").classList.add("hide");
    $("send").disabled = true; lastInspection = null; loadWeldSelect();
  } catch (e) { alert(e.message); }
}
$("doPass").onclick = () => signoff("pass");
$("doReject").onclick = () => signoff("reject");

// ---------- jobs & records ----------
$("addJob").onclick = async () => {
  try {
    await jpost("/jobs", { ref: $("jRef").value.trim(), item_type: $("jType").value, customer: $("jCust").value.trim() });
    $("jRef").value = ""; $("jCust").value = ""; loadJobs(); loadJobSelect();
  } catch (e) { alert(e.message); }
};
async function loadJobs() {
  const box = $("jobList"); box.replaceChildren();
  const jobs = await jget("/jobs");
  if (!jobs.length) box.append(el("p", "No jobs yet.", "mute"));
  jobs.forEach((j) => {
    const r = el("div", "", "row"); r.style.cursor = "pointer";
    const l = el("div"); l.append(el("strong", j.ref), el("div", `${itemLabel(j.item_type)} ${j.customer ? "· " + j.customer : ""}`, "mute"));
    r.append(l, pill(j.status)); r.onclick = () => showRecord(j.id); box.append(r);
  });
}
async function showRecord(id) {
  const rec = await jget(`/jobs/${id}/record`);
  $("recCard").classList.remove("hide");
  $("recTitle").textContent = `Quality record: ${rec.job.ref} (${itemLabel(rec.job.item_type)})`;
  const box = $("rec"); box.replaceChildren();
  const head = el("div", "", "row"); head.append(el("strong", "Job status"), pill(rec.status)); box.append(head);
  rec.welds.forEach((w) => {
    const r = el("div", "", "row");
    const l = el("div"); l.append(el("strong", w.joint_label),
      el("div", `${w.welder_name} (${w.badge}) · ${w.process} · ${w.inspections.length} photo(s)`, "mute"),
      el("div", `Material: ${materialText(w.base_material)} · Electrode: ${electrodeText(w.electrode)}`, "mute"));
    const last = w.inspections[w.inspections.length - 1];
    if (last && last.final_label) l.append(el("div", `Finding: ${last.final_label.replace(/_/g, " ")} by ${last.inspector}`, "mute"));
    r.append(l, pill(w.status)); box.append(r);
  });
  $("recCard").scrollIntoView({ behavior: "smooth" });
}
$("csv").onclick = async () => {
  try {
    const b = await (await api("/export.csv")).blob();
    const a = el("a"); a.href = URL.createObjectURL(b); a.download = "weld_records.csv"; a.click();
  } catch (e) { alert(e.message); }
};

// ---------- welders ----------
$("addWelder").onclick = async () => {
  try {
    await jpost("/welders", { name: $("wName").value.trim(), badge: $("wBadge").value.trim(), qualification: $("wQual").value.trim() });
    ["wName", "wBadge", "wQual"].forEach((i) => $(i).value = ""); loadWelders(); loadWelderSelect();
  } catch (e) { alert(e.message); }
};
async function loadWelders() {
  const box = $("welderList"); box.replaceChildren();
  const ws = await jget("/welders");
  if (!ws.length) box.append(el("p", "No welders yet.", "mute"));
  for (const w of ws) {
    const s = await jget(`/welders/${w.id}/stats`);
    const r = el("div", "", "row");
    const l = el("div"); l.append(el("strong", `${w.name} (${w.badge})`), el("div", w.qualification || "No qualification recorded", "mute"));
    const rate = s.reject_rate == null ? "no data" : `${Math.round(s.reject_rate * 100)}% rejected of ${s.welds_assessed}`;
    r.append(l, el("span", rate, "mute")); box.append(r);
  }
}

// ---------- settings ----------
$("insp").value = store.get("insp"); $("key").value = store.get("key");
$("saveSet").onclick = () => { store.set("insp", $("insp").value.trim()); store.set("key", $("key").value.trim()); loadAll(); alert("Saved"); };

netState(); renderQueue(); loadAll(); flushQueue();
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
