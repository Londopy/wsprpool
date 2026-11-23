"use strict";

const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
// kiwirecorder writes 12 kHz complex 16-bit samples: 48000 bytes a second
const BYTES_PER_SECOND = 48000;
const TAB_NAMES = ["record", "decode", "sim"];

// the key arrives in the address fragment; keep it for reloads of this tab, then drop it
// from the address bar so it doesn't sit in history or get copied along with the link
function readToken() {
  const fromHash = new URLSearchParams(location.hash.slice(1)).get("t");
  try {
    if (fromHash) sessionStorage.setItem("wsprpool-token", fromHash);
    return fromHash || sessionStorage.getItem("wsprpool-token") || "";
  } catch (e) {
    return fromHash || "";
  }
}

const token = readToken();

async function api(path, body) {
  const opts = { headers: { "X-Token": token } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch (e) {
    throw new Error("wsprpool isn't answering. Is it still running in the terminal?");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `the request failed with HTTP ${r.status}`);
  return data;
}

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (v !== false && v !== null && v !== undefined) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids) e.append(k);
  return e;
}

function svg(tag, attrs = {}, text) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}

function fileName(path) {
  return (path || "").split(/[\\/]/).pop();
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function mmss(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function utcTime(t) {
  return new Date(t * 1000).toISOString().slice(11, 19);
}

// only touch a live region when its words change, so screen readers hear each change once
function setStatus(node, text, error = false) {
  const shown = error && text ? text[0].toUpperCase() + text.slice(1) : text;
  if (node.textContent !== shown) node.textContent = shown;
  node.classList.toggle("error", error);
}

function showFatal(text) {
  const f = $("fatal");
  f.textContent = text;
  f.hidden = false;
}

/* clock: WSPR transmissions start on even UTC minutes */

function nextStart(now) {
  let start = (Math.floor(now / 120) + 1) * 120 - 8;
  if (start - now < 2) start += 120;
  return start;
}

function tick() {
  const now = new Date();
  $("utc").textContent = now.toISOString().slice(11, 19) + " UTC";
  const into = (now.getUTCMinutes() % 2) * 60 + now.getUTCSeconds();
  $("slot").textContent = `next slot in ${mmss(120 - into)}`;
  drawPlan();
}

/* tabs */

function showTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) {
    const on = b.dataset.tab === name;
    if (on) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
    $(b.dataset.tab).hidden = !on;
  }
  history.replaceState(null, "", `?tab=${name}`);
  if (name === "decode") loadSessions();
}

/* receivers: the public KiwiSDR list on a Maidenhead grid */

const REGIONS = {
  world: { box: [0, 0, 360, 180] },
  americas: { box: [10, 15, 140, 135] },
  europe: { box: [155, 18, 70, 38] },
  asia: { box: [275, 40, 85, 100] },
};
const rx = { all: [], picked: new Map(), region: "world", loading: false };

// some owners put coordinates in the location field; the name says more then
function place(r) {
  if (r.loc && !r.loc.trim().startsWith("(")) return r.loc;
  return (r.name || "").replace(/\s*[|,]?\s*\([-\d.,\s]+\)\s*$/, "") || r.id;
}

// a session file names receivers the way the recorder labels its folders
function labelOf(id) {
  return id.replace(":", "-").toLowerCase().replace(/[^a-z0-9.-]/g, "-");
}

function placeOfLabel(label) {
  const r = rx.all.find((o) => labelOf(o.id) === label);
  return r ? place(r) : label;
}

function mapXY(r) {
  return [r.lon + 180, 90 - r.lat];
}

function drawGrid() {
  const map = $("map");
  const letters = "ABCDEFGHIJKLMNOPQR";
  for (let i = 0; i <= 18; i++) {
    map.append(svg("line", { class: "gridline", x1: i * 20, y1: 0, x2: i * 20, y2: 180 }));
    map.append(svg("line", { class: "gridline", x1: 0, y1: i * 10, x2: 360, y2: i * 10 }));
  }
  for (let i = 0; i < 18; i++) {
    for (let j = 0; j < 18; j++) {
      map.append(svg("text", { class: "field", x: i * 20 + 1.2, y: 180 - j * 10 - 1.4 },
        letters[i] + letters[j]));
    }
  }
  map.append(svg("g", { id: "dots" }));
}

function freqHz() {
  return Number($("rec-freq").value) * 1000;
}

function whyNot(r) {
  if (!r.usable) return r.why;
  const f = freqHz();
  if (f && (f < r.low_hz || f > r.high_hz)) return `doesn't cover ${$("rec-freq").value} kHz`;
  return null;
}

function passesFilters(r) {
  const q = $("rx-search").value.trim().toLowerCase();
  if (q && !`${r.name} ${r.loc} ${r.grid} ${r.id}`.toLowerCase().includes(q)) return false;
  if ($("rx-usable").checked && whyNot(r)) return false;
  if ($("rx-nolimit").checked && r.time_limit !== "none") return false;
  return true;
}

function inRegion(r) {
  if (rx.region === "world") return true;
  if (r.lat === null) return false;
  const [x0, y0, w, h] = REGIONS[rx.region].box;
  const [x, y] = mapXY(r);
  return x >= x0 && x <= x0 + w && y >= y0 && y <= y0 + h;
}

function drawDots() {
  const g = $("dots");
  g.replaceChildren();
  const scale = REGIONS[rx.region].box[2] / 360;
  // picked receivers last, so the crowd around them doesn't cover them
  const shown = rx.all.filter((r) => !rx.picked.has(r.id) && passesFilters(r));
  for (const r of shown.concat([...rx.picked.values()])) {
    if (r.lat === null) continue;
    const [x, y] = mapXY(r);
    const state = rx.picked.has(r.id) ? "sel" : whyNot(r) ? "no" : "ok";
    const c = svg("circle", { class: `rx ${state}`, cx: x, cy: y, r: (state === "sel" ? 2.4 : 1.5) * Math.max(scale, 0.22) });
    c.append(svg("title", {}, `${place(r)} (${r.grid})${whyNot(r) ? ": " + whyNot(r) : ""}`));
    c.addEventListener("click", () => togglePick(r));
    g.append(c);
  }
}

function drawList() {
  const body = $("rx-table").tBodies[0];
  body.replaceChildren();
  const shown = rx.all.filter((r) => passesFilters(r) && inRegion(r))
    .sort((a, b) => place(a).localeCompare(place(b)));
  const cap = 300;
  for (const r of shown.slice(0, cap)) {
    const why = whyNot(r);
    const box = el("input", { type: "checkbox", "aria-label": `Pick ${place(r)}`, disabled: !!why && !rx.picked.has(r.id) });
    box.checked = rx.picked.has(r.id);
    box.addEventListener("change", () => togglePick(r));
    const users = r.users === null ? "?" : `${r.users}/${r.users_max ?? "?"}`;
    body.append(el("tr", { class: why ? "no" : "" },
      el("td", {}, box),
      el("td", { text: place(r), title: r.name }),
      el("td", { class: "mono", text: r.grid }),
      el("td", { class: "mono", text: users }),
      el("td", { text: { none: "none", yes: "yes", unknown: "not stated" }[r.time_limit] }),
      el("td", { class: "why", text: why || "can record" })));
  }
  const total = rx.all.length;
  if (!total) return;
  $("rx-wrap").hidden = shown.length === 0;
  if (!shown.length) setStatus($("rx-state"), "No receivers match these filters.");
  else if (shown.length > cap) setStatus($("rx-state"), `Showing ${cap} of ${shown.length} matching receivers; search to narrow it down.`);
  else setStatus($("rx-state"), `${shown.length} of ${total} receivers shown.`);
}

function drawPicked() {
  const list = $("rec-picked");
  list.replaceChildren();
  $("rec-count").textContent = rx.picked.size;
  if (!rx.picked.size) {
    list.append(el("li", { class: "empty", text: "None yet. Pick receivers on the map or in the list; two to five is plenty." }));
  }
  for (const r of rx.picked.values()) {
    const drop = el("button", { type: "button", "aria-label": `Remove ${place(r)}`, text: "remove" });
    drop.addEventListener("click", () => togglePick(r));
    const limit = { none: "no time limit", yes: "has a time limit", unknown: "time limit not stated" }[r.time_limit];
    const users = r.users === null ? "" : `, ${r.users} of ${r.users_max ?? "?"} listening`;
    list.append(el("li", {},
      el("span", {}, `${place(r)} (${r.grid})`, el("small", { text: `${limit}${users}` })),
      drop));
  }
  drawPlan();
}

// what pressing Start will do, said before it's pressed, since it uses other people's receivers
function drawPlan() {
  const n = rx.picked.size;
  const minutes = Number($("rec-minutes").value);
  const busy = $("rec-start").dataset.busy === "1";
  $("rec-start").textContent = n ? `Record ${minutes} min on ${plural(n, "receiver")}` : "Start recording";
  if (!n || busy) {
    $("rec-plan").hidden = true;
    return;
  }
  const start = nextStart(Date.now() / 1000);
  const name = $("rec-output").value.trim() || $("rec-output").placeholder;
  $("rec-plan").hidden = false;
  $("rec-plan").textContent = `${$("rec-freq").value} kHz from ${utcTime(start + 8).slice(0, 5)} to `
    + `${utcTime(start + 8 + minutes * 60).slice(0, 5)} UTC, saved as ${name}. Each owner sees a listener called wsprpool.`;
}

function togglePick(r) {
  if (rx.picked.has(r.id)) rx.picked.delete(r.id);
  else if (whyNot(r)) return;
  else if (rx.picked.size >= 8) {
    setStatus($("rec-status"), "Eight receivers is the most one session takes.", true);
    return;
  } else rx.picked.set(r.id, r);
  drawDots();
  drawList();
  drawPicked();
}

function setRegion(name) {
  rx.region = name;
  for (const b of document.querySelectorAll("[data-region]")) b.setAttribute("aria-pressed", b.dataset.region === name);
  const map = $("map");
  map.setAttribute("viewBox", REGIONS[name].box.join(" "));
  map.dataset.region = name;
  // keep labels and lines the same size on screen at every zoom
  map.style.setProperty("--zoom", REGIONS[name].box[2] / 360);
  drawDots();
  drawList();
}

async function loadReceivers(refresh = false) {
  if (rx.loading) return;
  rx.loading = true;
  $("rx-refresh").disabled = true;
  setStatus($("rx-state"), "Loading the receiver list");
  try {
    const data = await api("/api/receivers" + (refresh ? "?refresh=1" : ""));
    rx.all = data.receivers;
    if (!rx.all.length) setStatus($("rx-state"), "The public list came back empty. Try again in a minute.");
    drawDots();
    drawList();
    if (dec.report) showReport(dec.report);
  } catch (e) {
    setStatus($("rx-state"), `Couldn't load the receiver list: ${e.message}`, true);
  } finally {
    rx.loading = false;
    $("rx-refresh").disabled = false;
  }
}

/* polling a background job: one loop per job, started by poll() and ended when it settles */

function poller(path, render) {
  let timer = null;
  let running = false;
  const run = async () => {
    running = true;
    try {
      const st = await api(path);
      render(st);
      timer = ["idle", "done", "stopped", "error"].includes(st.phase) ? null : setTimeout(run, 1000);
    } catch (e) {
      render({ phase: "error", error: e.message });
      timer = null;
    }
    running = false;
  };
  return () => {
    if (timer === null && !running) run();
  };
}

/* recording */

const pollRecord = poller("/api/record", renderRecord);

function renderRecord(st) {
  const busy = ["checking", "recording", "saving"].includes(st.phase);
  $("rec-start").disabled = busy;
  $("rec-start").dataset.busy = busy ? "1" : "";
  $("rec-stop").disabled = !busy;
  const status = $("rec-status");
  const clock = $("rec-clock");
  const meter = $("rec-progress");
  meter.hidden = st.phase !== "recording";
  $("rec-done").hidden = !(st.phase === "done" || (st.phase === "stopped" && st.recorded));
  const now = st.now || Date.now() / 1000;
  clock.textContent = "";
  if (st.phase === "idle") setStatus(status, "");
  else if (st.phase === "checking") setStatus(status, "Checking that the picked receivers are free");
  else if (st.phase === "recording" && now < st.start) {
    setStatus(status, `Waiting for the slot at ${utcTime(st.start + 8)} UTC`);
    clock.textContent = `starts in ${mmss(st.start - now)}`;
  } else if (st.phase === "recording") {
    setStatus(status, `Recording until ${utcTime(st.end + 8)} UTC`);
    clock.textContent = `${mmss(now - st.start)} of ${mmss(st.end - st.start)}`;
    meter.firstElementChild.style.width = `${Math.min(100, (100 * (now - st.start)) / (st.end - st.start))}%`;
  } else if (st.phase === "saving") setStatus(status, "Saving the session file");
  else if (st.phase === "done") setStatus(status, `Saved ${fileName(st.output)} with ${plural(st.recorded, "receiver")}`);
  else if (st.phase === "stopped") setStatus(status, st.recorded ? `Stopped early; saved ${fileName(st.output)}` : "Stopped before anything was recorded");
  else if (st.phase === "error") setStatus(status, `Recording failed: ${st.error}`, true);
  status.title = st.output || "";

  const rows = $("rec-rx");
  rows.replaceChildren();
  for (const [id, bytes] of Object.entries(st.bytes || {})) {
    rows.append(el("li", {}, el("span", { text: id }), el("span", { text: bytes ? `${(bytes / BYTES_PER_SECOND / 60).toFixed(1)} min` : "waiting" })));
  }
  const log = $("rec-log");
  log.replaceChildren(...(st.log || []).map((t) => el("li", { text: t })));
  drawPlan();
}

async function startRecord(ev) {
  ev.preventDefault();
  if (!rx.picked.size) {
    setStatus($("rec-status"), "Pick at least one receiver first.", true);
    return;
  }
  const minutes = Number($("rec-minutes").value);
  if (!Number.isInteger(minutes) || minutes < 2 || minutes > 60 || minutes % 2) {
    setStatus($("rec-status"), "Record for an even number of minutes from 2 to 60, so the session covers whole 2-minute slots.", true);
    $("rec-minutes").focus();
    return;
  }
  try {
    const st = await api("/api/record", {
      receivers: [...rx.picked.keys()],
      freq_khz: Number($("rec-freq").value),
      minutes,
      output: $("rec-output").value.trim(),
      kiwirecorder: $("rec-tool").value.trim(),
    });
    renderRecord(st);
    pollRecord();
  } catch (e) {
    setStatus($("rec-status"), e.message, true);
  }
}

/* decoding */

const TAGS = {
  "both": "combined and alone",
  "combined only": "combined only",
  "alone only": "alone only",
  "alone": "alone",
};
const dec = { rows: [], filter: "all", report: null, path: "" };
const pollDecode = poller("/api/decode", renderDecode);

function renderDecode(st) {
  const busy = ["loading", "decoding", "checking"].includes(st.phase);
  $("dec-start").disabled = busy;
  $("dec-stop").disabled = !busy;
  if (st.path) dec.path = st.path;
  if (st.path && !$("dec-path").value) $("dec-path").value = st.path;
  const status = $("dec-status");
  const meter = $("dec-progress");
  meter.hidden = !busy;
  const elapsed = st.started && st.now ? ` (${mmss(st.now - st.started)})` : "";
  if (st.phase === "idle") setStatus(status, "");
  else if (st.phase === "loading") setStatus(status, "Loading the session");
  else if (st.phase === "decoding") setStatus(status, st.total ? `Decoding slot ${Math.min(st.done + 1, st.total)} of ${st.total}` : "Decoding");
  else if (st.phase === "checking") setStatus(status, "Looking the spots up on wspr.live");
  else if (st.phase === "done") setStatus(status, `Decoded ${plural(st.total || 0, "slot")}`);
  else if (st.phase === "stopped") setStatus(status, `Stopped after ${plural(st.done || 0, "slot")}; the table covers those`);
  else if (st.phase === "error") setStatus(status, `Decode failed: ${st.error}`, true);
  $("dec-clock").textContent = busy ? elapsed.trim() : "";
  if (st.total) meter.firstElementChild.style.width = `${(100 * st.done) / st.total}%`;

  $("dec-slots").replaceChildren(...(st.slots || []).map((s) => el("li", { title: Object.entries(s.alone).map(([n, c]) => `${placeOfLabel(n)}: ${c} alone`).join(", ") },
    `${s.utc} `, el("b", { text: s.combined }), " combined")));

  dec.report = st.report || null;
  if (st.report) showReport(st.report);
  else $("dec-result").hidden = true;
}

function showReport(rep) {
  $("dec-result").hidden = false;
  dec.rows = rep.rows;
  const answer = $("dec-answer");
  if (rep.receivers.length > 1) {
    const found = rep.combined_only
      ? `Combining the receivers found ${plural(rep.combined_only, "spot")} that none of them decoded alone`
      : "Combining the receivers found nothing that one of them didn't decode alone";
    let checked = ".";
    if (rep.checked && rep.combined_only === 1) checked = rep.confirmed ? "; other stations reported it on wspr.live too." : "; nobody else reported it on wspr.live, so it may be a false decode.";
    else if (rep.checked && rep.combined_only) checked = `; other stations reported ${rep.confirmed} of them on wspr.live too.`;
    answer.textContent = found + checked;
  } else {
    answer.textContent = `One receiver decoded ${plural(rep.any_alone, "spot")}.`;
  }
  const sum = $("dec-summary");
  sum.replaceChildren();
  const item = (label, value, small, cls) => sum.append(el("div", {}, el("dt", { text: label, class: cls }), el("dd", {}, String(value), small ? el("small", { text: " " + small }) : "")));
  for (const r of rep.receivers) item(placeOfLabel(r.name), r.alone, "alone", "rxname");
  item("Any receiver alone", rep.any_alone);
  if (rep.receivers.length > 1) {
    item("Combined", rep.combined);
    if (rep.correlation) item("Noise in common", rep.correlation.value.toFixed(2), `over ${plural(rep.correlation.pairs, "pair")}; 0 means independent receivers`);
  }
  if (rep.check_error) setStatus($("dec-status"), `Decoded, but the wspr.live check failed: ${rep.check_error}`, true);
  for (const b of document.querySelectorAll("[data-filter]")) {
    const n = b.dataset.filter === "all" ? rep.rows.length : rep.rows.filter((r) => r.tag === b.dataset.filter).length;
    b.textContent = `${b.dataset.label} (${n})`;
  }
  drawSpots();
}

function drawSpots() {
  const body = $("dec-table").tBodies[0];
  const rows = dec.rows.filter((r) => dec.filter === "all" || r.tag === dec.filter);
  body.replaceChildren(...rows.map((r) => {
    const rep = r.reporters === null ? "" : String(r.reporters);
    const repCell = el("td", { class: "num", text: rep });
    if (r.tag === "combined only" && r.reporters !== null) repCell.classList.add(r.reporters ? "confirmed" : "unconfirmed");
    return el("tr", { class: r.tag === "combined only" ? "combined-only" : "" },
      el("td", { class: "mono", text: r.utc.slice(11) }),
      el("td", { class: "num", text: r.mhz.toFixed(6) }),
      el("td", { class: "num", text: r.snr.toFixed(1) }),
      el("td", { class: "num", text: r.dt.toFixed(2) }),
      el("td", { class: "num", text: r.drift.toFixed(1) }),
      el("td", { class: "num", text: r.metric.toFixed(1) }),
      el("td", { class: "msg", text: r.message }),
      el("td", { class: "tag", text: TAGS[r.tag] || r.tag }),
      el("td", { text: r.alone.map(placeOfLabel).join(", ") || "none" }),
      repCell);
  }));
  $("dec-empty").hidden = rows.length > 0;
  $("dec-table-wrap").hidden = rows.length === 0;
}

// the same columns and time format as `wsprpool decode --csv`
function saveCsv() {
  const head = ["utc", "mhz", "snr", "dt", "drift", "metric", "call", "grid", "power", "found_by", "receivers_alone", "wspr_live_reporters"];
  const quote = (v) => `"${String(v).replaceAll('"', '""')}"`;
  const rows = dec.rows.map((r) => [
    r.utc.replace(" ", "T") + ":00Z", r.mhz.toFixed(6), r.snr.toFixed(1), r.dt.toFixed(2),
    r.drift.toFixed(1), r.metric.toFixed(1), r.call || `<${r.callhash}>`, r.grid, r.power,
    r.tag, r.alone.join(" "), r.reporters ?? ""]);
  const lines = [head.join(",")].concat(rows.map((row) => row.map(quote).join(",")));
  const url = URL.createObjectURL(new Blob([lines.join("\r\n")], { type: "text/csv" }));
  const stem = fileName(dec.path).replace(/\.londopy$/, "") || "spots";
  const a = el("a", { href: url, download: `${stem}-spots.csv` });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function startDecode(ev) {
  ev.preventDefault();
  const path = $("dec-path").value.trim();
  if (!path) {
    setStatus($("dec-status"), "Give the path of a .londopy session file.", true);
    $("dec-path").focus();
    return;
  }
  try {
    renderDecode(await api("/api/decode", { path, wspr_live: $("dec-check").checked }));
    pollDecode();
  } catch (e) {
    setStatus($("dec-status"), e.message, true);
  }
}

async function loadSessions() {
  try {
    const data = await api("/api/sessions");
    $("dec-sessions").replaceChildren(...data.sessions.map((s) => el("option", { value: s.path, label: s.name })));
    if (data.sessions.length === 1 && !$("dec-path").value) $("dec-path").value = data.sessions[0].path;
    $("dec-hint").textContent = data.sessions.length
      ? `${plural(data.sessions.length, "session file")} in ${data.workdir}; start typing to pick one.`
      : `No .londopy files in ${data.workdir} yet. Record one, or give the full path of one elsewhere.`;
    if (!$("rec-tool").value) $("rec-tool").value = data.kiwirecorder;
  } catch (e) {
    $("dec-hint").textContent = `Couldn't list session files: ${e.message}`;
  }
}

/* simulator */

const pollSim = poller("/api/sim", renderSim);
// series colours live in the stylesheet so each theme gets a set that reads on its background
const SERIES = 6;

let simShown = false;

// after a reload, show the settings of the run on screen, not the defaults
function fillSimForm(st) {
  simShown = true;
  if (!st.receivers) return;
  $("sim-ns").value = st.receivers.join(",");
  $("sim-lo").value = st.snrs[0];
  $("sim-hi").value = st.snrs[st.snrs.length - 1];
  $("sim-trials").value = st.trials;
  $("sim-doppler").value = st.doppler;
  $("sim-shared").value = st.shared;
  $("sim-spread").value = st.freq_spread;
  $("sim-seed").value = st.seed;
}

function renderSim(st) {
  if (!simShown) fillSimForm(st);
  const busy = st.phase === "running";
  $("sim-start").disabled = busy;
  $("sim-stop").disabled = !busy;
  const status = $("sim-status");
  const total = (st.receivers || []).length * (st.snrs || []).length;
  const done = Object.values(st.rates || {}).reduce((n, v) => n + v.length, 0);
  const meter = $("sim-progress");
  meter.hidden = !busy;
  if (total) meter.firstElementChild.style.width = `${(100 * done) / total}%`;
  if (st.phase === "idle") setStatus(status, "");
  else if (busy) setStatus(status, `Simulating point ${Math.min(done + 1, total)} of ${total}`);
  else if (st.phase === "done") setStatus(status, `Done: ${plural(total, "point")} of ${plural(st.trials, "slot")} each`);
  else if (st.phase === "stopped") setStatus(status, `Stopped after ${done} of ${plural(total, "point")}`);
  else if (st.phase === "error") setStatus(status, `Simulation failed: ${st.error}`, true);
  if (st.snrs && done) drawChart(st);
  $("sim-figure").hidden = !(st.snrs && done);
  drawGains(st.gains);
}

function drawChart(st) {
  const c = $("sim-chart");
  for (const n of [...c.childNodes]) if (n.nodeName !== "title") n.remove();
  const L = 52, R = 124, T = 14, B = 40, W = 640, H = 320;
  // text is sized in chart units; on a narrow screen grow it back to a readable size
  const grow = Math.max(1, Math.min(2, W / Math.max(c.clientWidth, 1)));
  const text = (attrs, words, size) => {
    const t = svg("text", attrs, words);
    t.style.fontSize = `${size * grow}px`;
    return t;
  };
  const lo = st.snrs[0], hi = st.snrs[st.snrs.length - 1];
  const x = (s) => L + ((s - lo) / Math.max(hi - lo, 1)) * (W - L - R);
  const y = (r) => H - B - r * (H - T - B);
  for (const r of [0, 0.25, 0.5, 0.75, 1]) {
    c.append(svg("line", { class: r === 0.5 ? "half" : "gridline", x1: L, x2: W - R, y1: y(r), y2: y(r) }));
    c.append(text({ class: "tick", x: L - 6, y: y(r) + 4, "text-anchor": "end" }, `${r * 100}%`, 11));
  }
  const step = Math.max(1, Math.ceil((hi - lo) / (grow > 1.4 ? 5 : 10)));
  for (let s = lo; s <= hi; s += step) c.append(text({ class: "tick", x: x(s), y: H - B + 16, "text-anchor": "middle" }, s, 11));
  c.append(text({ class: "tick", x: (L + W - R) / 2, y: H - 6, "text-anchor": "middle" }, "SNR in 2500 Hz, dB", 11));
  c.append(svg("line", { class: "axis", x1: L, x2: L, y1: T, y2: H - B }));
  c.append(svg("line", { class: "axis", x1: L, x2: W - R, y1: H - B, y2: H - B }));
  st.receivers.forEach((n, i) => {
    const rates = (st.rates || {})[String(n)] || [];
    if (!rates.length) return;
    const color = `var(--s${i % SERIES})`;
    const pts = rates.map((r, k) => `${x(st.snrs[k])},${y(r)}`).join(" ");
    const line = svg("polyline", { class: "curve", points: pts });
    line.style.stroke = color;
    c.append(line);
    for (const [k, r] of rates.entries()) {
      const dot = svg("circle", { cx: x(st.snrs[k]), cy: y(r), r: 3 });
      dot.style.fill = color;
      c.append(dot);
    }
    const label = text({ class: "label", x: W - R + 18, y: T + 16 + i * 18 * grow }, plural(n, "receiver"), 12);
    label.style.fill = color;
    c.append(label);
  });
}

function drawGains(gains) {
  $("sim-table-wrap").hidden = !gains;
  if (!gains) return;
  $("sim-table").tBodies[0].replaceChildren(...gains.map((g) => el("tr", {},
    el("td", { text: g.n }),
    el("td", { class: "num", text: g.threshold === null ? "outside the range" : `${g.threshold.toFixed(1)} ± ${g.error.toFixed(1)} dB` }),
    el("td", { class: "num", text: g.gain === null ? "" : `${g.gain.toFixed(1)} ± ${g.gain_error.toFixed(1)} dB` }),
    el("td", { class: "num", text: `${g.ideal.toFixed(1)} dB` }))));
}

async function startSim(ev) {
  ev.preventDefault();
  simShown = true;
  const ns = $("sim-ns").value.split(",").map((v) => Number(v.trim())).filter((v) => Number.isInteger(v) && v > 0);
  if (!ns.length) {
    setStatus($("sim-status"), "Give receiver counts like 1,2,4.", true);
    $("sim-ns").focus();
    return;
  }
  try {
    renderSim(await api("/api/sim", {
      receivers: ns,
      snr_low: Number($("sim-lo").value),
      snr_high: Number($("sim-hi").value),
      trials: Number($("sim-trials").value),
      doppler: Number($("sim-doppler").value),
      shared: Number($("sim-shared").value),
      freq_spread: Number($("sim-spread").value),
      seed: Number($("sim-seed").value),
    }));
    pollSim();
  } catch (e) {
    setStatus($("sim-status"), e.message, true);
  }
}

async function stopJob(path, node) {
  try {
    await api(path, {});
    setStatus(node, "Stopping");
  } catch (e) {
    setStatus(node, e.message, true);
  }
}

/* wiring */

function init() {
  tick();
  setInterval(tick, 1000);
  if (!token) {
    showFatal("This page needs the access key in its address. Open the link wsprpool printed in the terminal.");
    return;
  }
  const tab = new URLSearchParams(location.search).get("tab");
  showTab(TAB_NAMES.includes(tab) ? tab : "record");
  for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));
  for (const b of document.querySelectorAll("[data-region]")) b.addEventListener("click", () => setRegion(b.dataset.region));
  for (const b of document.querySelectorAll("[data-filter]")) {
    b.addEventListener("click", () => {
      dec.filter = b.dataset.filter;
      for (const o of document.querySelectorAll("[data-filter]")) o.setAttribute("aria-pressed", o === b);
      drawSpots();
    });
  }
  for (const id of ["rx-search", "rx-usable", "rx-nolimit"]) $(id).addEventListener("input", () => { drawDots(); drawList(); });
  $("rec-freq").addEventListener("change", () => { drawDots(); drawList(); drawPlan(); });
  for (const id of ["rec-minutes", "rec-output"]) $(id).addEventListener("input", drawPlan);
  $("rec-output").placeholder = `session-${new Date().toISOString().slice(0, 16).replace(/[-:]/g, "")}Z.londopy`;
  $("rx-refresh").addEventListener("click", () => loadReceivers(true));
  $("rec-form").addEventListener("submit", startRecord);
  $("rec-stop").addEventListener("click", () => stopJob("/api/record/stop", $("rec-status")));
  $("rec-decode").addEventListener("click", async () => {
    const st = await api("/api/record");
    $("dec-path").value = st.output || "";
    showTab("decode");
    $("dec-path").focus();
  });
  $("dec-form").addEventListener("submit", startDecode);
  $("dec-stop").addEventListener("click", () => stopJob("/api/decode/stop", $("dec-status")));
  $("dec-csv").addEventListener("click", saveCsv);
  $("sim-form").addEventListener("submit", startSim);
  $("sim-stop").addEventListener("click", () => stopJob("/api/sim/stop", $("sim-status")));

  drawGrid();
  setRegion("world");
  drawPicked();
  loadReceivers();
  loadSessions();
  pollRecord();
  pollDecode();
  pollSim();
}

init();
