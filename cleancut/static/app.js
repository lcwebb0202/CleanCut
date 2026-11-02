"use strict";

const $ = (id) => document.getElementById(id);
const SCREENS = ["setup", "waiting", "working", "review-screen", "done", "failed"];
let STATE = null;          // last /api/state
let movie = null;          // {path, shown, dir} chosen input (media-relative path)
let outDir = null;         // {path, shown}
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

// ------------------------------------------------------------ setup screen

function initSetup() {
  const filters = load("filters2", ["nudity", "sex", "suggestive", "fword", "strong", "mild", "blasphemy", "slurs"]);
  document.querySelectorAll('input[name="f"]').forEach((c) => (c.checked = filters.includes(c.value)));
  setSeg("audio", load("audio", "mute"));
  setSeg("cover", load("cover", "box"));
  outDir = load("outDir2", null);
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

function updateWhen() {
  const now = $("start-now").checked;
  $("start-time").disabled = now;
  const at = now ? null : nextStart();
  let hint;
  if (now) hint = "It starts as soon as you press Start.";
  else if (!at) hint = "Pick a time, or tick Start right away.";
  else {
    hint = `It starts at ${fmtClock(at)}, in ${fmtDuration(Math.round((at - Date.now()) / 1000))}. `
      + "Leave the computer on and plugged in, and don't let it go to sleep.";
    if ($("review").checked) hint += " It will wait for your review before saving; untick the review box above to have the movie saved by morning.";
  }
  $("when-hint").textContent = hint;
  $("start").textContent = now ? "Start" : at ? "Start at " + fmtClock(at) : "Start";
  return now || !!at;
}

$("start-now").addEventListener("change", updateSetup);
$("start-time").addEventListener("input", updateSetup);
$("review").addEventListener("change", updateSetup);
setInterval(() => { if (!$("setup").hidden) updateWhen(); }, 30000);  // keeps "in 5 h 10 min" current

// Where to save: the folder picked last time, else the save folder from the settings
// (e.g. "CleanCut Edited Media"), else next to the movie.
function saveDir() {
  if (outDir) return outDir;
  if (STATE && STATE.default_out) return STATE.default_out;
  return movie && { path: movie.dir, shown: movie.shown.split("/").slice(0, -1).join("/") };
}

// The exact file name, plus a warning when Jellyfin wouldn't show it as a version of the original.
async function showOutputInfo(input, outPath) {
  try {
    const info = await api(`/api/output?input=${encodeURIComponent(input)}&out_dir=${encodeURIComponent(outPath)}`);
    if (!movie || movie.path !== input || saveDir().path !== outPath) return;  // choice changed meanwhile
    $("out-name").textContent = "File name: " + info.name;
    $("out-note").textContent = info.note || "";
    $("out-note").hidden = !info.note;
  } catch (e) { /* keep the page usable */ }
}

function chosenFilters() {
  return [...document.querySelectorAll('input[name="f"]:checked')].map((c) => c.value);
}

function updateSetup() {
  $("drop").hidden = !!movie;
  $("movie").hidden = !movie;
  if (movie) {
    $("movie-name").textContent = movie.shown.split("/").pop();
    $("movie-path").textContent = movie.shown;
  }
  const dir = saveDir();
  $("out-path").textContent = dir ? dir.shown : "Choose a movie first";
  $("out-name").textContent = "";
  $("out-note").hidden = true;
  if (movie && dir) showOutputInfo(movie.path, dir.path);
  const timeOk = updateWhen();
  $("start").disabled = !movie || chosenFilters().length === 0 || !timeOk;
}

document.querySelectorAll('input[name="f"]').forEach((c) => c.addEventListener("change", updateSetup));

// Drag and drop: the browser doesn't reveal where a dropped file lives, so ask the server to find
// a file with the same name and size in the media folder. Only if it isn't there, copy it in.
const drop = $("drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());
drop.addEventListener("drop", async (e) => {
  e.preventDefault();
  const file = e.dataTransfer.files[0];
  if (file) await useDroppedFile(file);
});

async function useDroppedFile(file) {
  $("start-error").textContent = "";
  try {
    movie = await api("/api/find", { method: "POST", body: { name: file.name, size: file.size } });
    updateSetup();
    return;
  } catch (e) { /* not found: offer to copy */ }
  const ok = confirm(
    `"${file.name}" isn't in your media folder (${STATE.media_root}).\n\n` +
    `CleanCut can copy it there (${fmtSize(file.size)}, needs that much free space). ` +
    `Copying a big movie can take several minutes.\n\nCopy it now?`);
  if (!ok) return;
  $("upload").hidden = false;
  $("drop").hidden = true;
  const xhr = new XMLHttpRequest();
  xhr.open("PUT", "/api/upload?name=" + encodeURIComponent(file.name));
  xhr.upload.onprogress = (ev) => {
    const pct = ev.total ? (ev.loaded / ev.total) * 100 : 0;
    $("upload-bar").style.width = pct + "%";
    $("upload-text").textContent = `Copying ${file.name}… ${pct.toFixed(0)}%`;
  };
  xhr.onload = () => {
    $("upload").hidden = true;
    if (xhr.status === 200) movie = JSON.parse(xhr.responseText);
    else $("start-error").textContent = "Copying failed: " + xhr.responseText;
    updateSetup();
  };
  xhr.onerror = () => { $("upload").hidden = true; $("start-error").textContent = "Copying failed."; updateSetup(); };
  xhr.send(file);
}

$("browse-movie").addEventListener("click", () => openBrowser("file"));
$("change-movie").addEventListener("click", () => { movie = null; updateSetup(); openBrowser("file"); });
$("browse-out").addEventListener("click", () => openBrowser("folder"));

$("start").addEventListener("click", async () => {
  const filters = chosenFilters();
  const body = {
    input: movie.path,
    out_dir: saveDir().path,
    filters, audio: segValue("audio"), cover: segValue("cover"), review: $("review").checked,
    start_at: $("start-now").checked ? 0 : nextStart().getTime() / 1000,
  };
  save("filters2", filters); save("audio", body.audio); save("cover", body.cover);
  $("start").disabled = true;
  $("start-error").textContent = "";
  try {
    await api("/api/start", { method: "POST", body });
    await refresh();
  } catch (e) {
    $("start-error").textContent = e.message;
    $("start").disabled = false;
  }
});

// ------------------------------------------------------------ file / folder browser

let browserMode = "file";
let browserPath = "";

function openBrowser(mode) {
  browserMode = mode;
  $("b-title").textContent = mode === "file" ? "Choose a movie" : "Choose where to save";
  $("b-use").hidden = $("b-newfolder").hidden = mode !== "folder";
  const start = mode === "folder" ? (saveDir() || { path: "" }).path : load("lastDir2", "");
  $("browser").showModal();
  browseTo(start).catch(() => browseTo(""));
}

async function browseTo(path) {
  const d = await api("/api/browse?path=" + encodeURIComponent(path));
  browserPath = d.path;
  if (browserMode === "file") save("lastDir2", d.path);
  $("b-use").disabled = $("b-newfolder").disabled = !d.path;  // the top level lists folders but isn't one itself
  const crumbs = $("b-crumbs");
  crumbs.replaceChildren(d.shown);
  const list = $("b-list");
  list.replaceChildren();
  if (d.parent != null) list.append(el("li", {}, el("button", { onclick: () => browseTo(d.parent) }, el("span", { class: "ico" }, "↑"), "Up one folder")));
  for (const name of d.dirs) {
    list.append(el("li", {}, el("button", { onclick: () => browseTo((d.path ? d.path + "/" : "") + name) }, el("span", { class: "ico" }, "📁"), name)));
  }
  if (browserMode === "file") {
    for (const f of d.files) {
      const path = (d.path ? d.path + "/" : "") + f.name;
      list.append(el("li", {}, el("button", {
        onclick: () => {
          movie = { path, shown: d.shown + "/" + f.name, dir: d.path };
          $("browser").close();
          updateSetup();
        },
      }, el("span", { class: "ico" }, "🎬"), f.name, el("span", { class: "size" }, fmtSize(f.size)))));
    }
  }
  if (!list.children.length || (browserMode === "file" && !d.files.length && !d.dirs.length)) {
    list.append(el("li", { class: "empty" }, browserMode === "file" ? "No movies in this folder." : "No folders here."));
  }
  $("browser").dataset.shown = d.shown;
}

$("b-close").addEventListener("click", () => $("browser").close());
$("b-use").addEventListener("click", () => {
  outDir = { path: browserPath, shown: $("browser").dataset.shown };
  save("outDir2", outDir);
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

// ------------------------------------------------------------ waiting for a scheduled start

function showWaiting(job) {
  show("waiting");
  const at = new Date(job.start_at * 1000);
  $("wait-movie").textContent = job.input_name;
  $("wait-at").textContent = fmtClock(at);
  const left = Math.round(job.start_at - Date.now() / 1000);
  $("wait-left").textContent = left > 0 ? "Starts in " + fmtDuration(left) : "Starting…";
  $("wait-review").textContent = job.review
    ? "When it's done looking through the movie, it waits for your review before saving."
    : "The filtered movie is saved without a review, usually by morning.";
}

$("wait-cancel").addEventListener("click", async () => {
  if (!confirm("Cancel the scheduled start?")) return;
  await api("/api/cancel", { method: "POST" });
  refresh();
});
$("wait-now").addEventListener("click", async () => {
  await api("/api/start-now", { method: "POST" });
  refresh();
});

// ------------------------------------------------------------ progress

function showWorking(job) {
  show("working");
  $("w-movie").textContent = job.input_name;
  $("w-stage").textContent = job.stage;
  $("w-bar").style.width = job.pct + "%";
  $("w-pct").textContent = job.pct ? job.pct.toFixed(0) + "%" : "";
  let time = "Running for " + fmtDuration(job.elapsed);
  if (job.pct > 3 && job.pct < 100) {
    const left = (job.stage_elapsed / job.pct) * (100 - job.pct);
    time += " · this step: about " + fmtDuration(Math.round(left)) + " left";
  }
  $("w-time").textContent = time;
  $("w-log").textContent = job.log.join("\n");
}

$("cancel").addEventListener("click", async () => {
  if (!confirm("Stop working on this movie?")) return;
  await api("/api/cancel", { method: "POST" });
  refresh();
});

// ------------------------------------------------------------ review

const ACTIONS_BODY = [["auto", "Box"], ["blackout", "Black screen"], ["skip", "Cut out"], ["ignore", "Allow"]];
const ACTIONS_SCENE = [["blackout", "Black screen"], ["skip", "Cut out"], ["ignore", "Allow"]];
const CATEGORY_NAMES = { fword: "F-word", strong: "Strong", mild: "Mild", blasphemy: "God's name", slurs: "Slur" };

async function showReview() {
  show("review-screen");
  const err = STATE.job && STATE.job.error;
  $("r-error").hidden = !err;
  $("r-error-msg").textContent = err || "";
  $("r-error-log").textContent = err ? STATE.job.log.join("\n") : "";
  if (review) return;
  const p = await api("/api/plan");
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
  keepTimer = setTimeout(() => api("/api/plan", { method: "POST", body: choices() }).catch(() => { /* saved with the movie */ }), 600);
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
  const img = el("img", { alt: "Blurred preview", src: `/api/thumb/${v.id}?level=${level}&f=${fi}` });
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
    await api("/api/plan", { method: "POST", body: choices() });
    await api("/api/render", { method: "POST" });
    review = null;
    await refresh();
  } catch (e) {
    alert(e.message);
  } finally {
    $("r-save").disabled = false;
  }
});

$("r-cancel").addEventListener("click", async () => {
  if (!confirm("Throw away this review and go back to the start? Nothing will be saved.")) return;
  await api("/api/reset", { method: "POST" });
  review = null;
  refresh();
});

// ------------------------------------------------------------ done / failed

$("d-again").addEventListener("click", startOver);
$("f-again").addEventListener("click", startOver);
async function startOver() {
  await api("/api/reset", { method: "POST" });
  movie = null;
  review = null;
  resetChoices();
  updateSetup();
  refresh();
}

// ------------------------------------------------------------ main loop

async function refresh() {
  clearTimeout(pollTimer);
  try {
    STATE = await api("/api/state");
  } catch (e) {
    pollTimer = setTimeout(refresh, 3000);
    return;
  }
  const job = STATE.job;
  if (!job) {
    show("setup");
    updateSetup();
  } else if (job.state === "waiting") {
    showWaiting(job);
    pollTimer = setTimeout(refresh, 5000);
  } else if (job.state === "analyzing" || job.state === "rendering") {
    showWorking(job);
    pollTimer = setTimeout(refresh, 1500);
  } else if (job.state === "review") {
    await showReview();
  } else if (job.state === "done") {
    show("done");
    $("d-out").textContent = job.output;
  } else {
    show("failed");
    const cancelled = job.state === "cancelled";
    $("f-title").textContent = cancelled ? "Cancelled" : "Something went wrong";
    $("f-icon").textContent = cancelled ? "×" : "!";
    $("f-msg").textContent = cancelled ? "Nothing was saved." : job.error;
    $("f-log").textContent = job.log.join("\n");
  }
}

initSetup();
refresh();
