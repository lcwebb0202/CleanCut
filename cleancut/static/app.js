"use strict";

const $ = (id) => document.getElementById(id);
const SCREENS = ["setup", "queue", "review-screen"];
let STATE = null;          // last /api/state
let screen = null;         // "setup" (adding movies), "queue" (the list) or "review"
let reviewId = null;       // the movie being reviewed
let movies = [];           // [{path, shown, dir}] chosen to add (media-relative paths)
let outDir = null;         // {path, shown}: a folder picked in step 3, for the movies being added now only
let pollTimer = null;
let review = null;         // {visual, words, settings, actions: {id: action}, blur: {id: level}}

// ------------------------------------------------------------ helpers

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  return res.json();
}

function show(screen) {
  for (const s of SCREENS) $(s).hidden = s !== screen;
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}

function fmtLength(sec) {
  sec = Math.round(sec);
  if (sec < 60) return sec + " s";
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + " min" + (s ? " " + s + " s" : "");
}

function fmtDuration(sec) {
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60;
  return h ? `${h} h ${m} min` : m ? `${m} min` : `${sec} s`;
}

function fmtSize(bytes) {
  return bytes > 1e9 ? (bytes / 1e9).toFixed(1) + " GB" : Math.round(bytes / 1e6) + " MB";
}

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k);
  return e;
}

function load(key, fallback) {
  try { const v = localStorage.getItem("cleancut." + key); return v == null ? fallback : JSON.parse(v); }
  catch (e) { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem("cleancut." + key, JSON.stringify(value)); } catch (e) { /* private mode */ }
}

// Segmented buttons: <div class="seg" data-name="x"><button data-v="a">…
function segValue(name) {
  const on = document.querySelector(`.seg[data-name="${name}"] button.on`);
  return on ? on.dataset.v : null;
}
function setSeg(name, value) {
  document.querySelectorAll(`.seg[data-name="${name}"] button`).forEach((b) => b.classList.toggle("on", b.dataset.v === value));
}
document.querySelectorAll(".seg[data-name]").forEach((seg) => {
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    setSeg(seg.dataset.name, b.dataset.v);
    seg.dispatchEvent(new Event("change"));
  });
});

// ------------------------------------------------------------ setup screen (adding movies)

function initSetup() {
  const filters = load("filters2", ["nudity", "sex", "suggestive", "fword", "strong", "mild", "blasphemy", "slurs"]);
  document.querySelectorAll('input[name="f"]').forEach((c) => (c.checked = filters.includes(c.value)));
  setSeg("audio", load("audio", "mute"));
  setSeg("cover", load("cover", "box"));
  resetChoices();
  updateSetup();
}

// When to start and review: always back to 2 AM, "not right away" and no review (on purpose not
// remembered, so an unattended overnight run is the default every time).
const DEFAULT_START = "02:00";

function resetChoices() {
  $("start-time").value = DEFAULT_START;
  $("start-now").checked = false;
  $("review").checked = false;
}

function fmtClock(date) {
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// The next time the clock shows the chosen time (later today, or tomorrow if it has passed).
function nextStart() {
  const [h, m] = $("start-time").value.split(":").map(Number);
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  const at = new Date();
  at.setHours(h, m, 0, 0);
  if (at <= new Date()) at.setDate(at.getDate() + 1);
  return at;
}

// Movies already lined up: new ones join the end of the list and the start time doesn't apply.
const queueBusy = () => !!(STATE && STATE.queue.has_work);

function updateWhen() {
  const busy = queueBusy();
  const n = movies.length;
  $("when-pick").hidden = busy;
  $("when-queued").hidden = !busy;
  if (busy) {
    $("when-queued").textContent = STATE.queue.waiting
      ? `Other movies are already lined up to start at ${fmtClock(new Date(STATE.queue.start_at * 1000))}. ` +
        `${n === 1 ? "This one is" : "These are"} added to the end of the list and start after them.`
      : `Other movies are already in the list. ${n === 1 ? "This one is" : "These are"} added to the end and ` +
        "start when it's their turn.";
    $("start").textContent = "Add to the list";
    return true;
  }
  const now = $("start-now").checked;
  $("start-time").disabled = now;
  const at = now ? null : nextStart();
  let hint;
  if (now) hint = "It starts as soon as you press Start.";
  else if (!at) hint = "Pick a time, or tick Start right away.";
  else {
    hint = `It starts at ${fmtClock(at)}, in ${fmtDuration(Math.round((at - Date.now()) / 1000))}. `
      + "Leave the computer on and plugged in, and don't let it go to sleep.";
    if ($("review").checked) hint += " Each movie waits for your review before it's saved, while the next one carries on.";
  }
  $("when-hint").textContent = hint;
  $("start").textContent = now ? "Start" : at ? "Start at " + fmtClock(at) : "Start";
  return now || !!at;
}

$("start-now").addEventListener("change", updateSetup);
$("start-time").addEventListener("input", updateSetup);
$("review").addEventListener("change", updateSetup);
setInterval(() => { if (!$("setup").hidden) updateWhen(); }, 30000);  // keeps "in 5 h 10 min" current

// Where to save: next to each movie, in its own folder, unless another folder is picked in step 3.
// A picked folder is only for the movies being added now; the next ones go back to their own folder
// (so Jellyfin shows the filtered copy as a version of the original).
function saveDir() {
  if (outDir) return outDir;
  if (!movies.length) return null;
  const dirs = new Set(movies.map((m) => m.dir));
  if (dirs.size > 1) return { path: "", shown: "Next to each movie, in the same folder as it", own: true };
  const m = movies[0];
  return { path: m.dir, shown: m.shown.split("/").slice(0, -1).join("/"), own: true };
}

// The exact file name, plus a warning when Jellyfin wouldn't show it as a version of the original.
async function showOutputInfo(input, outPath) {
  try {
    const info = await api(`/api/output?input=${encodeURIComponent(input)}&out_dir=${encodeURIComponent(outPath)}`);
    if (movies.length !== 1 || movies[0].path !== input || saveDir().path !== outPath) return;  // changed meanwhile
    $("out-name").textContent = "File name: " + info.name;
    $("out-note").textContent = info.note || "";
    $("out-note").hidden = !info.note;
  } catch (e) { /* keep the page usable */ }
}

function chosenFilters() {
  return [...document.querySelectorAll('input[name="f"]:checked')].map((c) => c.value);
}

function addMovies(list) {
  for (const m of list) if (!movies.some((x) => x.path === m.path)) movies.push(m);
  updateSetup();
}

function updateSetup() {
  $("drop").hidden = movies.length > 0;
  $("movies").hidden = $("movies-more").hidden = !movies.length;
  $("movies").replaceChildren(...movies.map((m, i) => el("li", {},
    el("div", { class: "grow" },
      el("div", { class: "picked-name" }, m.shown.split("/").pop()),
      el("div", { class: "picked-path" }, m.shown)),
    el("button", { class: "x", title: "Remove", "aria-label": "Remove",
      onclick: () => { movies.splice(i, 1); updateSetup(); } }, "×"))));
  const dir = saveDir();
  $("out-path").textContent = dir ? dir.shown : "Choose a movie first";
  $("out-why").textContent = !dir ? "" : dir.own ? "The same folder as the movie." : "A different folder, just for these movies.";
  $("out-reset").hidden = !outDir;
  $("out-name").textContent = movies.length > 1 ? `Each is saved as its own name plus " - Filtered".` : "";
  $("out-note").hidden = true;
  if (movies.length === 1 && dir) showOutputInfo(movies[0].path, dir.path);
  const timeOk = updateWhen();
  $("start").disabled = !movies.length || chosenFilters().length === 0 || !timeOk;
  $("to-queue").hidden = !(STATE && STATE.queue.items.length);
}

document.querySelectorAll('input[name="f"]').forEach((c) => c.addEventListener("change", updateSetup));

// Drag and drop: the browser doesn't reveal where a dropped file lives, so ask the server to find
// a file with the same name and size in the media folder. Only if it isn't there, copy it in.
const drop = $("drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  if (!$("setup").hidden && e.target !== drop && !drop.contains(e.target)) dropFiles(e.dataTransfer.files);
});
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  dropFiles(e.dataTransfer.files);
});

async function dropFiles(files) {
  $("start-error").textContent = "";
  for (const file of files) await useDroppedFile(file);
}

async function useDroppedFile(file) {
  try {
    addMovies([await api("/api/find", { method: "POST", body: { name: file.name, size: file.size } })]);
    return;
  } catch (e) { /* not found: offer to copy */ }
  const ok = confirm(
    `"${file.name}" isn't in your media folder (${STATE.media_root}).\n\n` +
    `CleanCut can copy it there (${fmtSize(file.size)}, needs that much free space). ` +
    `Copying a big movie can take several minutes.\n\nCopy it now?`);
  if (!ok) return;
  $("upload").hidden = false;
  $("drop").hidden = true;
  await new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", "/api/upload?name=" + encodeURIComponent(file.name));
    xhr.upload.onprogress = (ev) => {
      const pct = ev.total ? (ev.loaded / ev.total) * 100 : 0;
      $("upload-bar").style.width = pct + "%";
      $("upload-text").textContent = `Copying ${file.name}… ${pct.toFixed(0)}%`;
    };
    xhr.onload = () => {
      $("upload").hidden = true;
      if (xhr.status === 200) addMovies([JSON.parse(xhr.responseText)]);
      else $("start-error").textContent = "Copying failed: " + xhr.responseText;
      updateSetup();
      resolve();
    };
    xhr.onerror = () => { $("upload").hidden = true; $("start-error").textContent = "Copying failed."; updateSetup(); resolve(); };
    xhr.send(file);
  });
}

$("browse-movie").addEventListener("click", () => openBrowser("file"));
$("add-more").addEventListener("click", () => openBrowser("file"));
$("clear-movies").addEventListener("click", () => { movies = []; updateSetup(); });
$("browse-out").addEventListener("click", () => openBrowser("folder"));
$("out-reset").addEventListener("click", () => { outDir = null; updateSetup(); });
$("to-queue").addEventListener("click", () => go("queue"));

$("start").addEventListener("click", async () => {
  const filters = chosenFilters();
  const body = {
    inputs: movies.map((m) => m.path),
    out_dir: outDir ? outDir.path : "",  // empty: each movie's own folder
    filters, audio: segValue("audio"), cover: segValue("cover"), review: $("review").checked,
    start_at: queueBusy() || $("start-now").checked ? 0 : nextStart().getTime() / 1000,
  };
  save("filters2", filters); save("audio", body.audio); save("cover", body.cover);
  $("start").disabled = true;
  $("start-error").textContent = "";
  try {
    await api("/api/start", { method: "POST", body });
    movies = [];
    outDir = null;
    resetChoices();
    go("queue");
  } catch (e) {
    $("start-error").textContent = e.message;
    $("start").disabled = false;
  }
});

// ------------------------------------------------------------ file / folder browser

// Choosing movies: tick any number, or "Add all in this folder" (a TV season).
let browserMode = "file";
let browserPath = "";
let browserFiles = [];     // the movies in the folder being shown
let ticked = new Map();    // ticked movies, path -> movie (they can come from several folders)

function openBrowser(mode) {
  browserMode = mode;
  ticked = new Map();
  $("b-title").textContent = mode === "file" ? "Choose movies" : "Choose where to save";
  $("b-newfolder").hidden = mode !== "folder";
  const start = mode === "folder" ? (outDir || (movies[0] && { path: movies[0].dir }) || { path: "" }).path
                                   : load("lastDir2", "");
  $("browser").showModal();
  browseTo(start).catch(() => browseTo(""));
}

function updateBrowserButtons() {
  if (browserMode === "folder") {
    $("b-use").textContent = "Use this folder";
    $("b-use").disabled = !browserPath;
    $("b-all").hidden = true;
    return;
  }
  $("b-use").textContent = ticked.size ? `Add ${ticked.size} movie${ticked.size === 1 ? "" : "s"}` : "Add";
  $("b-use").disabled = !ticked.size;
  $("b-all").hidden = browserFiles.length < 2;
  $("b-all").textContent = `Add all ${browserFiles.length} in this folder`;
}

async function browseTo(path) {
  const d = await api("/api/browse?path=" + encodeURIComponent(path));
  browserPath = d.path;
  if (browserMode === "file") save("lastDir2", d.path);
  $("b-newfolder").disabled = !d.path;  // the top level lists folders but isn't one itself
  $("b-crumbs").replaceChildren(d.shown);
  const list = $("b-list");
  list.replaceChildren();
  if (d.parent != null) list.append(el("li", {}, el("button", { onclick: () => browseTo(d.parent) }, el("span", { class: "ico" }, "↑"), "Up one folder")));
  for (const name of d.dirs) {
    list.append(el("li", {}, el("button", { onclick: () => browseTo((d.path ? d.path + "/" : "") + name) }, el("span", { class: "ico" }, "📁"), name)));
  }
  browserFiles = browserMode === "file"
    ? d.files.map((f) => ({ path: (d.path ? d.path + "/" : "") + f.name, shown: d.shown + "/" + f.name, dir: d.path, size: f.size }))
    : [];
  for (const f of browserFiles) {
    const box = el("input", { type: "checkbox", checked: ticked.has(f.path),
      onchange: (e) => { e.target.checked ? ticked.set(f.path, f) : ticked.delete(f.path); updateBrowserButtons(); } });
    list.append(el("li", {}, el("label", { class: "pick" }, box,
      el("span", { class: "ico" }, "🎬"), f.shown.split("/").pop(), el("span", { class: "size" }, fmtSize(f.size)))));
  }
  if (!list.children.length || (browserMode === "file" && !browserFiles.length && !d.dirs.length)) {
    list.append(el("li", { class: "empty" }, browserMode === "file" ? "No movies in this folder." : "No folders here."));
  }
  $("browser").dataset.shown = d.shown;
  updateBrowserButtons();
}

function useTicked(files) {
  $("browser").close();
  addMovies(files.map(({ path, shown, dir }) => ({ path, shown, dir })));
}

$("b-close").addEventListener("click", () => $("browser").close());
$("b-all").addEventListener("click", () => useTicked(browserFiles));
$("b-use").addEventListener("click", () => {
  if (browserMode === "file") {
    useTicked([...ticked.values()]);
    return;
  }
  outDir = { path: browserPath, shown: $("browser").dataset.shown };
  $("browser").close();
  updateSetup();
});
$("b-newfolder").addEventListener("click", async () => {
  const name = prompt("Name of the new folder:");
  if (!name) return;
  try {
    const d = await api("/api/folder", { method: "POST", body: { path: browserPath, name } });
    await browseTo(d.path);
  } catch (e) { alert(e.message); }
});

// ------------------------------------------------------------ the list of movies (queue)

const openLogs = new Set();  // movies whose Details are open, kept across the list being redrawn

async function itemAction(id, what, question) {
  if (question && !confirm(question)) return;
  try {
    await api(`/api/items/${id}/${what}`, { method: "POST" });
  } catch (e) { alert(e.message); }
  refresh();
}

function timeLeft(j) {
  if (!(j.pct > 3 && j.pct < 100)) return "";
  return "about " + fmtDuration(Math.round((j.stage_elapsed / j.pct) * (100 - j.pct))) + " left";
}

function queueRow(j, i, items) {
  const running = j.state === "analyzing" || j.state === "rendering";
  const lined = j.state === "queued" || j.state === "ready";
  const q = STATE.queue;
  let stage = j.stage, meta = "", pct = 0, cls = j.state;
  if (running && j.paused) {
    pct = j.pct;
    stage = "Paused · " + j.stage;
    meta = (j.pct ? j.pct.toFixed(0) + "% · " : "") + "click Resume to carry on from here";
    cls += " paused";
  } else if (running) {
    pct = j.pct;
    meta = [j.pct ? j.pct.toFixed(0) + "%" : "", timeLeft(j)].filter(Boolean).join(" · ");
  } else if (lined && q.waiting) {
    stage = j.state === "ready" ? "Reviewed, saves after the start time" : `Starts at ${fmtClock(new Date(q.start_at * 1000))}`;
  } else if (j.state === "review") {
    stage = "Ready for your review";
    meta = "The next movie carries on meanwhile";
    pct = 100;
  } else if (j.state === "done") {
    stage = "✓ Saved";
    meta = j.output;
    pct = 100;
  } else if (j.state === "error") {
    stage = "✗ " + (j.stage === "Interrupted" ? "Interrupted" : "Didn't work");
    meta = j.error;
  } else if (j.state === "cancelled") {
    meta = "Nothing was saved.";
  }

  const buttons = [];
  const btn = (label, onclick, extra = {}) => el("button", { class: "btn small", onclick, ...extra }, label);
  if (j.state === "review") buttons.push(btn("Review", () => go("review", j.id), { class: "btn small primary" }));
  if (j.state === "error" || j.state === "cancelled") buttons.push(btn("Try again", () => itemAction(j.id, "retry")));
  if (running) {
    buttons.push(j.paused ? btn("Resume", () => itemAction(j.id, "resume"), { class: "btn small primary" })
                          : btn("Pause", () => itemAction(j.id, "pause")));
    buttons.push(btn("Cancel", () => itemAction(j.id, "cancel",
      `Stop working on "${j.input_name}"? Nothing is saved for it${items.some((x) => x.state === "queued" || x.state === "ready") ? ", and the next movie starts" : ""}.`)));
  }
  if (lined) {
    buttons.push(btn("↑", () => itemAction(j.id, "move?by=-1"), { title: "Move up", disabled: i === 0 }));
    buttons.push(btn("↓", () => itemAction(j.id, "move?by=1"), { title: "Move down", disabled: i === items.length - 1 }));
  }
  if (!running) {
    const ask = j.state === "done" ? null
      : j.state === "review" || j.state === "ready" ? `Take "${j.input_name}" off the list? Your review is thrown away and nothing is saved.`
      : `Take "${j.input_name}" off the list?`;
    buttons.push(btn("×", () => itemAction(j.id, "remove", ask), { title: "Take off the list", "aria-label": "Take off the list" }));
  }

  const details = j.log.length ? el("details", { class: "log", open: openLogs.has(j.id),
    ontoggle: (e) => { e.target.open ? openLogs.add(j.id) : openLogs.delete(j.id); } },
    el("summary", {}, "Details"), el("pre", {}, j.log.join("\n"))) : null;
  return el("li", { class: "q-item " + cls },
    el("div", { class: "q-name" }, j.input_name,
      el("div", { class: "q-sub" }, j.review ? "Review before saving" : "Saved without review")),
    el("div", { class: "q-mid" },
      el("div", { class: "q-stage" }, stage),
      el("div", { class: "bar" }, el("div", { style: `width:${pct}%` })),
      el("div", { class: "q-meta" }, meta)),
    el("div", { class: "q-actions" }, buttons),
    details);
}

function renderQueue() {
  const q = STATE.queue;
  $("q-wait").hidden = !q.waiting;
  if (q.waiting) {
    const at = new Date(q.start_at * 1000);
    $("q-at").textContent = fmtClock(at);
    const left = Math.round(q.start_at - Date.now() / 1000);
    $("q-left").textContent = left > 0 ? "in " + fmtDuration(left) : "starting…";
  }
  $("q-disk").hidden = !q.disk_note;
  $("q-disk").textContent = q.disk_note;
  $("q-stop").hidden = !q.has_work;
  $("q-clear").hidden = !q.items.some((j) => ["done", "error", "cancelled"].includes(j.state));
  $("q-list").replaceChildren(...q.items.map((j, i) => queueRow(j, i, q.items)));
  if (!q.items.length) $("q-list").append(el("li", { class: "q-empty hint" }, "No movies yet."));
}

$("q-new").addEventListener("click", () => go("setup"));
$("q-now").addEventListener("click", async () => { await api("/api/start-now", { method: "POST" }); refresh(); });
$("q-clear").addEventListener("click", async () => { await api("/api/clear", { method: "POST" }); refresh(); });
$("q-stop").addEventListener("click", async () => {
  if (!confirm("Stop everything? The movie being worked on is cancelled, and the ones waiting are taken off " +
               "the to-do list (you can start them again with Try again). Reviews you've done are kept.")) return;
  await api("/api/stop", { method: "POST" });
  refresh();
});

// ------------------------------------------------------------ review

const ACTIONS_BODY = [["auto", "Box"], ["blackout", "Black screen"], ["skip", "Cut out"], ["ignore", "Allow"]];
const ACTIONS_SCENE = [["blackout", "Black screen"], ["skip", "Cut out"], ["ignore", "Allow"]];
const CATEGORY_NAMES = { fword: "F-word", strong: "Strong", mild: "Mild", blasphemy: "God's name", slurs: "Slur" };

async function showReview() {
  show("review-screen");
  const item = STATE.queue.items.find((j) => j.id === reviewId);
  $("r-movie").textContent = item ? item.input_name : "";
  if (review) return;
  const p = await api(`/api/items/${reviewId}/plan`);
  review = { ...p, actions: {}, blur: {}, frame: {} };
  for (const v of p.visual) review.actions[v.id] = v.action === "box" ? "auto" : v.action;
  for (const w of p.words) review.actions[w.id] = w.action;
  setSeg("r-audio", p.settings.audio);
  setSeg("r-cover", p.settings.cover);
  renderReview();
}

// CleanCut planned to cover it, so it's fairly sure. Letting one of these play asks first:
// a real one slipped through that way once, because the blurred picture looked harmless.
const isSure = (v) => v.suggested !== "ignore";

function renderReview() {
  const only = $("only-flagged").checked;
  const sure = $("r-grid-sure"), maybe = $("r-grid-maybe");
  sure.replaceChildren();
  maybe.replaceChildren();
  const flagged = review.visual.filter((v) => v.unsure.length).length;
  const nSure = review.visual.filter(isSure).length;
  $("r-summary").textContent =
    `${review.visual.length} moment${review.visual.length === 1 ? "" : "s"} found` +
    (flagged ? ` · ${flagged} marked ⚠ to check` : "");
  $("r-empty").hidden = review.visual.length > 0;
  $("r-sure-head").hidden = !nSure;
  $("r-sure-count").textContent = nSure;
  $("r-maybe-head").hidden = nSure === review.visual.length;
  $("r-maybe-count").textContent = review.visual.length - nSure;
  for (const v of review.visual) {
    if (only && !v.unsure.length) continue;
    (isSure(v) ? sure : maybe).append(reviewItem(v));
  }
  renderWords();
}

// Choices are saved as they're made, so a page refresh or an app update keeps them.
let keepTimer = null;
const choices = () => ({ actions: review.actions, audio: segValue("r-audio"), cover: segValue("r-cover") });
function keepChoices() {
  clearTimeout(keepTimer);
  keepTimer = setTimeout(() => api(`/api/items/${reviewId}/plan`, { method: "POST", body: choices() }).catch(() => { /* saved with the movie */ }), 600);
}

function setAction(v, val) {
  const pct = v.confidence != null ? ` (${Math.round(v.confidence * 100)}% sure)` : "";
  if (val === "ignore" && isSure(v) && review.actions[v.id] !== "ignore" &&
      !confirm(`CleanCut is fairly sure this is real${pct}, so it planned to cover it.\n\n` +
               "Let it play uncovered anyway?")) return;
  review.actions[v.id] = val;
  keepChoices();
  renderReview();
}

function reviewItem(v) {
  const action = review.actions[v.id];
  const level = review.blur[v.id] || 0;
  const frames = v.frames || [];
  const fi = Math.min(review.frame[v.id] || 0, Math.max(0, frames.length - 1));
  const fr = frames[fi] || { t: v.start, box: false };
  const img = el("img", { alt: "Blurred preview", src: `/api/items/${reviewId}/thumb/${v.id}?level=${level}&f=${fi}` });
  const frame = el("div", { class: "frame" }, img);
  const thumb = el("div", { class: "thumb" + (level >= STATE.light_blur ? " light" : "") }, frame);
  img.addEventListener("load", () => {
    // Size the frame to the picture so the box outline lines up with it.
    const r = img.naturalWidth / img.naturalHeight;
    const tw = thumb.clientWidth, th = thumb.clientHeight;
    const w = Math.min(tw, th * r);
    frame.style.width = w + "px";
    frame.style.height = w / r + "px";
  });
  const badge =
    action === "ignore" ? "Allowed - plays normally" :
    action === "skip" ? "Cut out" :
    action === "blackout" || (fr.box === null && action === "auto") || v.scene ? "Whole screen black" : null;
  if (action === "auto" && fr.box && !v.scene) {
    const [x0, y0, x1, y1] = fr.box;
    frame.append(el("div", { class: "box", style: `left:${x0 * 100}%;top:${y0 * 100}%;width:${(x1 - x0) * 100}%;height:${(y1 - y0) * 100}%` }));
  }
  if (badge) thumb.append(el("div", { class: "badge" }, badge));
  if (v.unsure.length) thumb.append(el("div", { class: "flag", title: "Worth checking" }, "⚠"));
  if (level >= STATE.light_blur) {
    thumb.append(el("div", { class: "light-note" }, "Lightly blurred. Black patches hide what CleanCut detected, but it can miss things."));
  }
  if (frames.length > 1) {
    const go = (d) => { review.frame[v.id] = (fi + d + frames.length) % frames.length; renderReview(); };
    thumb.append(el("div", { class: "frame-nav" },
      el("button", { title: "Previous picture", onclick: () => go(-1) }, "‹"),
      el("span", {}, `${fi + 1}/${frames.length}`),
      el("button", { title: "Next picture", onclick: () => go(1) }, "›")));
  }
  const max = v.max_level ?? STATE.blur_levels - 1;
  thumb.append(el("div", { class: "blur-ctl" },
    el("button", { disabled: level === 0, onclick: () => { review.blur[v.id] = level - 1; renderReview(); } }, "More blur"),
    el("button", { disabled: level >= max, onclick: () => { review.blur[v.id] = level + 1; renderReview(); } }, "Less blur")));

  const seg = el("div", { class: "seg small" });
  for (const [val, label] of v.scene ? ACTIONS_SCENE : ACTIONS_BODY) {
    seg.append(el("button", { class: val === action ? "on" : "", onclick: () => setAction(v, val) }, label));
  }
  const notes = [];
  if (v.scene && v.suggested === "skip") notes.push("No real dialogue, so it's cut out.");
  else if (v.scene && v.suggested === "blackout" && v.dialogue_words >= 6) {
    notes.push(`People talk during it (${v.dialogue_words} words), so it's blacked out instead of cut.`);
  }
  if (!v.scene && v.boxes > 1) notes.push(`${v.boxes} camera shots, each with its own box.`);
  return el("div", { class: "item" + (v.unsure.length ? " flagged" : "") + (action === "ignore" ? " allowed" : "") },
    thumb,
    el("div", { class: "item-body" },
      el("div", { class: "item-title" }, v.label,
        el("span", { class: "item-time" }, `${fmtTime(v.start)} · ${fmtLength(v.end - v.start)}`)),
      el("div", { class: "meta" }, `Picture from ${fmtTime(fr.t)}` +
        (v.confidence != null ? ` · ${Math.round(v.confidence * 100)}% sure` : "")),
      v.unsure.length ? el("ul", { class: "reasons" }, v.unsure.map((r) => el("li", {}, "⚠ " + r))) : null,
      notes.map((n) => el("div", { class: "note" }, n)),
      seg));
}

function renderWords() {
  const words = review.words;
  $("r-words-card").hidden = !words.length;
  const muted = words.filter((w) => review.actions[w.id] === "censor").length;
  $("r-words-summary").textContent = `${muted} of ${words.length} words will be ${segValue("r-audio") === "bleep" ? "bleeped" : "muted"}. Click a word to let it play.`;
  const box = $("r-words");
  box.replaceChildren();
  for (const w of words) {
    const on = review.actions[w.id] === "censor";
    box.append(el("label", { class: "word" + (on ? "" : " allowed") },
      el("input", { type: "checkbox", checked: on, onchange: (e) => { review.actions[w.id] = e.target.checked ? "censor" : "ignore"; keepChoices(); renderWords(); } }),
      el("span", { class: "t" }, fmtTime(w.start)),
      el("span", { class: "w" }, w.word),
      el("span", { class: "c", title: w.subtitles ? (w.line ? "A line that is mostly swearing: the whole subtitle line is muted" : w.approx ? "Found in the subtitles; timing taken from them" : "Found with help from the subtitles") : "" },
        (CATEGORY_NAMES[w.category] || w.category) + (w.line ? " · whole line" : w.subtitles ? " · subs" : ""))));
  }
}

$("only-flagged").addEventListener("change", renderReview);
document.querySelector('.seg[data-name="r-audio"]').addEventListener("change", () => { keepChoices(); renderWords(); });
document.querySelector('.seg[data-name="r-cover"]').addEventListener("change", keepChoices);

$("r-save").addEventListener("click", async () => {
  $("r-save").disabled = true;
  try {
    clearTimeout(keepTimer);
    await api(`/api/items/${reviewId}/plan`, { method: "POST", body: choices() });
    await api(`/api/items/${reviewId}/render`, { method: "POST" });
    go("queue");
  } catch (e) {
    alert(e.message);
  } finally {
    $("r-save").disabled = false;
  }
});

$("r-cancel").addEventListener("click", async () => {
  if (!confirm("Take this movie off the list? Your review is thrown away and nothing is saved.")) return;
  await api(`/api/items/${reviewId}/remove`, { method: "POST" });
  go("queue");
});
$("r-back").addEventListener("click", () => {
  clearTimeout(keepTimer);
  if (review) api(`/api/items/${reviewId}/plan`, { method: "POST", body: choices() }).catch(() => {});
  go("queue");
});

// ------------------------------------------------------------ updates

// The CleanCut helper on Windows checks GitHub at noon each day (or when the round arrow is
// clicked); when there's a new version the pill in the top right says so. "Update now" asks the
// helper to update, which restarts CleanCut; the page notices the restart, reloads itself and
// says it's up to date. Once an update has started the window never goes back to "Update now":
// it ends on "up to date" or on what went wrong.
let updating = null;       // {started: STATE.update.started, at: ms} once "Update now" was clicked
let updateTimer = null;
let checkAsked = 0;        // when the round arrow was clicked (ms)
let checkSeen = false;     // CleanCut has said it's checking since then
let note = null;           // {text, cls, until}: a short message in the pill ("Updated", "Couldn't check")
let dlgView = "news";      // the update window shows: "news" / "updating" / "done" / "current" / "error"
let dlgError = "";
const UPDATE_SLOW = 20 * 60 * 1000;

function fmtChecked(sec) {
  return new Date(sec * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}

function renderUpdate() {
  const u = STATE && STATE.update;
  if (!u) return;
  if (updating) {
    if (u.started !== updating.started) {  // CleanCut restarted with the new version
      try { sessionStorage.setItem("cleancut.updated", "1"); } catch (e) { /* fine */ }
      location.reload();
      return;
    }
    if (u.status === "error") [updating, dlgView, dlgError] = [null, "error", u.error];
    else if (u.status === "current") [updating, dlgView] = [null, "current"];
  }
  // A check is over once a newer "last checked" time arrives. It only failed if CleanCut said it was
  // checking and then stopped without one (or nothing came back for 70 s): judging it any earlier,
  // from the state fetched before the click, said "Couldn't check" for checks that worked.
  if (checkAsked) {
    if (u.checking) checkSeen = true;
    if (u.checked * 1000 >= checkAsked - 2000) {
      checkAsked = 0;
    } else if ((checkSeen && !u.checking) || Date.now() - checkAsked > 70000) {
      checkAsked = 0;
      note = { text: "Couldn't check", cls: "quiet", until: Date.now() + 6000,
               why: "Couldn't reach GitHub. Check the internet connection and try again." };
    }
  }
  if (note && Date.now() > note.until) note = null;
  const busyUpdating = !!updating || u.status === "requested" || u.status === "updating";

  // The pill in the top right: "Update available" (click for what's new) or a greyed-out state.
  const checking = u.checking || checkAsked > 0;
  let label, pill = "", open = false;
  if (busyUpdating) [label, open] = ["Updating…", true];
  else if (note) [label, pill] = [note.text, note.cls];
  else if (checking) [label, pill] = ["Checking…", "quiet"];
  else if (u.available) [label, open] = ["Update available", true];
  else if (u.known) [label, pill] = ["✓ Up to date", "quiet"];
  else [label, pill] = ["Not checked yet", "quiet"];
  $("upd").hidden = !(u.helper || u.known || busyUpdating || note);
  $("upd-label").textContent = label;
  $("upd-btn").className = "upd-btn " + pill;
  $("upd-btn").dataset.open = open ? "1" : "";
  $("upd-btn").title = note && note.why ? note.why : u.checked ? "Last checked " + fmtChecked(u.checked) : "";
  $("upd-check").disabled = !u.helper || checking || busyUpdating;
  $("upd-check").classList.toggle("spin", checking);

  // The update window.
  if (busyUpdating) dlgView = "updating";
  else if (dlgView === "updating") dlgView = u.available ? "news" : "current";
  const view = dlgView;
  const finished = view === "done" || view === "current";
  $("u-title").textContent = finished ? "✓ CleanCut is up to date"
    : view === "error" ? "The update didn't work" : "A new version of CleanCut is ready";
  $("u-intro").textContent = view === "done" ? "The update is installed. You're on the newest version of CleanCut."
    : view === "current" ? "There was nothing new to install. You're on the newest version of CleanCut."
    : view === "error" ? "Nothing was changed. You can keep using CleanCut as it is and try again later."
    : view === "updating" ? "Installing the new version. This takes a few minutes."
    : "Here's what's new. Updating keeps your settings and folders, and takes a few minutes.";
  const news = u.news && u.news.length ? u.news : [{ title: "Improvements and fixes", text: "" }];
  $("u-news").hidden = finished || view === "error";
  $("u-news").replaceChildren(...news.map((n) =>
    el("li", {}, el("div", { class: "t" }, n.title), n.text ? el("div", { class: "d" }, n.text) : null)));
  $("u-checked").textContent = view === "news" && u.checked ? "Checked " + fmtChecked(u.checked) : "";

  const busy = STATE.queue.has_work;
  let msg = "", cls = "";
  if (view === "updating") {
    msg = updating && Date.now() - updating.at > UPDATE_SLOW
      ? "This is taking longer than usual. Leave it running; this page will refresh by itself when it's done."
      : "Updating… CleanCut will restart in a few minutes, and this page will refresh by itself.";
    cls = "working";
  } else if (view === "error") {
    [msg, cls] = [dlgError || "Something went wrong.", "bad"];
  } else if (view === "news") {
    if (busy) {
      msg = STATE.queue.waiting
        ? "Movies are lined up for their start time. Let them finish (or click Stop all) before updating."
        : "CleanCut is working through your movies. You can update once they're all finished.";
    } else if (u.git) {
      msg = "This copy of CleanCut is kept up to date with Git (git pull), not from here.";
    } else if (!u.helper) {
      msg = "The CleanCut helper isn't running, so the update can't start from here. "
          + "Double-click \"Update CleanCut\" in your CleanCut folder instead.";
    }
  }
  $("u-msg").hidden = !msg;
  $("u-msg").className = "upd-msg " + cls;
  $("u-msg").textContent = msg;
  $("u-go").hidden = view !== "news" && view !== "updating";
  $("u-go").disabled = view === "updating" || busy || !u.helper || u.git;
  $("u-go").textContent = view === "updating" ? "Updating…" : "Update now";
  $("u-later").textContent = view === "news" ? "Later" : view === "updating" ? "Close" : "OK";
}

async function pollUpdate() {
  clearTimeout(updateTimer);
  try {
    STATE = await api("/api/state");
    renderUpdate();
  } catch (e) { /* restarting */ }
  const fast = updating || checkAsked || (STATE && STATE.update && STATE.update.checking) || note;
  updateTimer = setTimeout(pollUpdate, fast ? 2000 : 60000);
}

$("upd-btn").addEventListener("click", () => {
  if (!$("upd-btn").dataset.open) return;  // greyed out: nothing to show
  renderUpdate();
  $("update-dlg").showModal();
});
$("upd-check").addEventListener("click", async () => {
  checkAsked = Date.now();
  checkSeen = false;
  note = null;
  renderUpdate();
  try {
    await api("/api/update-check", { method: "POST" });
  } catch (e) {
    checkAsked = 0;
    note = { text: "Couldn't check", cls: "quiet", until: Date.now() + 6000, why: e.message };
  }
  pollUpdate();
});
$("u-close").addEventListener("click", () => $("update-dlg").close());
$("u-later").addEventListener("click", () => $("update-dlg").close());
// Closed: next time it opens on what's new (unless an update is still running).
$("update-dlg").addEventListener("close", () => { if (dlgView !== "updating") dlgView = "news"; });
$("u-go").addEventListener("click", async () => {
  try {
    await api("/api/update", { method: "POST" });
    updating = { started: STATE.update.started, at: Date.now() };
  } catch (e) {
    $("u-msg").hidden = false;
    $("u-msg").className = "upd-msg bad";
    $("u-msg").textContent = e.message;
    return;
  }
  renderUpdate();
  pollUpdate();
});

// Just restarted after an update: say so, and that it's up to date.
try {
  if (sessionStorage.getItem("cleancut.updated")) {
    sessionStorage.removeItem("cleancut.updated");
    note = { text: "✓ Updated", cls: "quiet done", until: Date.now() + 15000 };
    dlgView = "done";
    $("update-dlg").showModal();
  }
} catch (e) { /* fine */ }

// ------------------------------------------------------------ main loop

function go(to, id) {
  screen = to;
  if (to === "review") {
    if (reviewId !== id) review = null;
    reviewId = id;
  } else {
    review = null;
  }
  if (to === "setup") updateSetup();
  window.scrollTo(0, 0);
  refresh();
}

async function refresh() {
  clearTimeout(pollTimer);
  try {
    STATE = await api("/api/state");
  } catch (e) {
    pollTimer = setTimeout(refresh, 3000);
    return;
  }
  renderUpdate();
  const q = STATE.queue;
  if (screen === null) screen = q.items.length ? "queue" : "setup";
  if (screen === "review") {
    const item = q.items.find((j) => j.id === reviewId);
    if (!item || !["review", "ready"].includes(item.state)) screen = "queue";
  }
  let wait = 10000;
  if (screen === "setup") {
    show("setup");
    updateWhen();
    $("to-queue").hidden = !q.items.length;
  } else if (screen === "queue") {
    show("queue");
    renderQueue();
    wait = q.items.some((j) => j.state === "analyzing" || j.state === "rendering") ? 1500 : 5000;
  } else {
    await showReview();
  }
  pollTimer = setTimeout(refresh, wait);
}

initSetup();
refresh();
updateTimer = setTimeout(pollUpdate, 60000);
