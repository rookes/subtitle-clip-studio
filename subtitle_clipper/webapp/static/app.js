"use strict";

const $ = (id) => document.getElementById(id);
const DEFAULT_PAD = 0.5;
const EPS = 1e-6;
// Emphasis colour, kept in sync with clips.HIGHLIGHT_COLOR (server side) and
// --highlight (style.css).
const HIGHLIGHT_COLOR = "#fff000";

let lastResults = [];      // all Match dicts from the server (capped), paginated in the UI
let lastQuery = "";        // for match highlighting
let lastRegex = false;
let rowRefs = {};          // global-index -> DOM refs, only for the currently rendered page
let itemState = {};        // global-index -> session overrides (timing/extras/video/version/selected)
let episodeCues = {};      // episode_id -> [{cue_index,start,end,text}], cached across searches

// Session link memory — remembered across searches, reset only on page reload.
let rememberedVideo = {};      // episode_id -> {path, status}
let rememberedSeriesDir = {};  // show_slug -> directory

let currentPage = 0;
let perPage = 25;

// --- time formatting --------------------------------------------------------

function fmtTime(sec) {
  sec = Math.max(0, sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

// HH:MM:SS.mmm for the editable timing fields / line timestamps / VTT cues.
function fmtClock(sec) {
  sec = Math.max(0, sec);
  const whole = Math.floor(sec);
  const ms = Math.round((sec - whole) * 1000);
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  const pad = (n, l = 2) => String(n).padStart(l, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}.${pad(ms, 3)}`;
}

// Parse "HH:MM:SS(.mmm)" / "MM:SS" / "SS" back to seconds; NaN if malformed.
function parseClock(str) {
  str = String(str).trim();
  if (!str) return NaN;
  const parts = str.split(":").map((p) => p.trim());
  if (parts.length > 3) return NaN;
  for (const p of parts) {
    if (p === "" || Number.isNaN(Number(p))) return NaN;
  }
  const nums = parts.map(Number);
  let sec;
  if (nums.length === 3) sec = nums[0] * 3600 + nums[1] * 60 + nums[2];
  else if (nums.length === 2) sec = nums[0] * 60 + nums[1];
  else sec = nums[0];
  return sec;
}

// The timing fields behave like a fixed "00:00:00.000" template edited with
// Insert (overtype) always on: the separators never move, only the nine digit
// slots change. Typing a digit overwrites the slot under the caret and steps to
// the next; Backspace/Delete reset a slot to 0; non-digits are ignored.
const CLOCK_TEMPLATE = "00:00:00.000";
const CLOCK_DIGIT_SLOTS = [0, 1, 3, 4, 6, 7, 9, 10, 11]; // char positions of digits

// Coerce any string into the strict template, keeping its digits in order.
function normalizeClock(value) {
  const digits = String(value).match(/\d/g) || [];
  const out = CLOCK_TEMPLATE.split("");
  CLOCK_DIGIT_SLOTS.forEach((slot, k) => {
    if (digits[k] != null) out[slot] = digits[k];
  });
  return out.join("");
}

// Index into CLOCK_DIGIT_SLOTS of the first digit slot at or after `pos`.
function clockSlotAtOrAfter(pos) {
  const k = CLOCK_DIGIT_SLOTS.findIndex((slot) => slot >= pos);
  return k === -1 ? CLOCK_DIGIT_SLOTS.length : k;
}

function ensureClockTemplate(input) {
  if (input.value !== normalizeClock(input.value)) {
    input.value = normalizeClock(input.value);
  }
}

function onClockKeydown(e, input) {
  if (e.ctrlKey || e.metaKey || e.altKey) return; // leave shortcuts alone
  if (["ArrowLeft", "ArrowRight", "Home", "End", "Tab"].includes(e.key)) return;

  // Enter commits the same way blurring does. Because we mutate the field's
  // value programmatically below, the browser never flags it user-dirty, so the
  // native "change" event won't fire — the caller commits on blur instead (see
  // the dataset.dirty flag we set on every mutation here).
  if (e.key === "Enter") { e.preventDefault(); input.blur(); return; }

  const chars = normalizeClock(input.value).split("");
  const pos = input.selectionStart ?? 0;

  if (/^\d$/.test(e.key)) {
    e.preventDefault();
    let k = clockSlotAtOrAfter(pos);
    if (k >= CLOCK_DIGIT_SLOTS.length) k = CLOCK_DIGIT_SLOTS.length - 1; // clamp at last
    chars[CLOCK_DIGIT_SLOTS[k]] = e.key;
    input.value = chars.join("");
    input.dataset.dirty = "1";
    const next = k + 1 < CLOCK_DIGIT_SLOTS.length
      ? CLOCK_DIGIT_SLOTS[k + 1] : CLOCK_DIGIT_SLOTS[k] + 1;
    input.setSelectionRange(next, next);
    return;
  }

  if (e.key === "Backspace") {
    e.preventDefault();
    const k = clockSlotAtOrAfter(pos) - 1; // slot just before the caret
    if (k < 0) { input.setSelectionRange(0, 0); return; }
    const slot = CLOCK_DIGIT_SLOTS[k];
    chars[slot] = "0";
    input.value = chars.join("");
    input.dataset.dirty = "1";
    input.setSelectionRange(slot, slot);
    return;
  }

  if (e.key === "Delete") {
    e.preventDefault();
    const k = clockSlotAtOrAfter(pos);
    if (k >= CLOCK_DIGIT_SLOTS.length) return;
    const slot = CLOCK_DIGIT_SLOTS[k];
    chars[slot] = "0";
    input.value = chars.join("");
    input.dataset.dirty = "1";
    input.setSelectionRange(slot, slot);
    return;
  }

  if (e.key.length === 1) e.preventDefault(); // block any other printable char
}

// --- misc helpers -----------------------------------------------------------

function csv(id) {
  return $(id).value.split(",").map((s) => s.trim()).filter(Boolean);
}

function currentPad() {
  return parseFloat($("pad").value) || DEFAULT_PAD;
}

function setStatus(msg, isError = false) {
  const el = $("status");
  el.textContent = msg;
  el.classList.toggle("error", isError);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function highlight(text, query, isRegex) {
  if (!query) return escapeHtml(text);
  let re;
  try {
    re = isRegex
      ? new RegExp(query, "g")
      : new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  } catch {
    return escapeHtml(text);
  }
  let out = "", last = 0, m;
  re.lastIndex = 0;
  while ((m = re.exec(text)) !== null) {
    out += escapeHtml(text.slice(last, m.index)) + "<mark>" + escapeHtml(m[0]) + "</mark>";
    last = m.index + m[0].length;
    if (m[0].length === 0) re.lastIndex++;
  }
  return out + escapeHtml(text.slice(last));
}

// --- highlight ranges -------------------------------------------------------
// A line's emphasis is stored as half-open [start, end) character offsets into
// its plain text, never as inline markup. That keeps the text a plain string
// everywhere (search display, payloads, preview) and confines the markup to a
// single serialization step on the server.

// Sort, clamp and merge overlapping/touching ranges. Mirrors
// clips.normalize_highlights so both ends agree on the canonical form.
function mergeRanges(ranges, textLen) {
  const clean = (ranges || [])
    .map(([s, e]) => [Math.max(0, Math.min(s, textLen)), Math.max(0, Math.min(e, textLen))])
    .filter(([s, e]) => e > s)
    .sort((a, b) => a[0] - b[0]);
  const out = [];
  for (const [s, e] of clean) {
    if (out.length && s <= out[out.length - 1][1]) {
      out[out.length - 1][1] = Math.max(out[out.length - 1][1], e);
    } else {
      out.push([s, e]);
    }
  }
  return out;
}

function addRange(ranges, start, end, textLen) {
  return mergeRanges([...(ranges || []), [start, end]], textLen);
}

function removeRange(ranges, start, end, textLen) {
  const out = [];
  for (const [s, e] of ranges || []) {
    if (s < start) out.push([s, Math.min(e, start)]);
    if (e > end) out.push([Math.max(s, end), e]);
  }
  return mergeRanges(out, textLen);
}

function rangesCover(ranges, start, end) {
  return (ranges || []).some(([s, e]) => s <= start && e >= end);
}

// Paint {text, highlights} into a contenteditable. Newlines become <br> so a
// genuinely two-line cue survives editing (the old <input> silently dropped them).
function renderLineText(el, text, highlights) {
  const spans = mergeRanges(highlights, text.length);
  const asHtml = (s) => escapeHtml(s).replace(/\n/g, "<br>");
  let html = "", cursor = 0;
  for (const [s, e] of spans) {
    html += asHtml(text.slice(cursor, s)) + '<span class="hl">' + asHtml(text.slice(s, e)) + "</span>";
    cursor = e;
  }
  el.innerHTML = html + asHtml(text.slice(cursor));
}

// The inverse: flatten a contenteditable back to {text, highlights}.
function readLineText(el) {
  let text = "";
  const highlights = [];
  const walk = (node, inHl) => {
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        text += child.data;
      } else if (child.nodeName === "BR") {
        text += "\n";
      } else {
        const start = text.length;
        const hl = inHl || (child.classList && child.classList.contains("hl"));
        walk(child, hl);
        if (hl && !inHl && text.length > start) highlights.push([start, text.length]);
      }
    }
  };
  walk(el, false);
  return { text, highlights: mergeRanges(highlights, text.length) };
}

// Absolute character offset of (node, offset) within `el`, counting <br> as one
// character so it lines up with the "\n" readLineText produces.
function offsetInElement(el, node, offset) {
  let count = 0, found = false;
  const walk = (parent) => {
    for (const child of parent.childNodes) {
      if (found) return;
      if (child === node && child.nodeType !== Node.TEXT_NODE) {
        // Selection endpoints can address a child index rather than a text node.
        for (let k = 0; k < offset && k < child.childNodes.length; k++) {
          count += (child.childNodes[k].textContent || "").length;
        }
        found = true;
        return;
      }
      if (child.nodeType === Node.TEXT_NODE) {
        if (child === node) { count += offset; found = true; return; }
        count += child.data.length;
      } else if (child.nodeName === "BR") {
        count += 1;
      } else {
        walk(child);
      }
    }
  };
  if (node === el) {
    for (let k = 0; k < offset && k < el.childNodes.length; k++) {
      count += el.childNodes[k].nodeName === "BR"
        ? 1 : (el.childNodes[k].textContent || "").length;
    }
    return count;
  }
  walk(el);
  return count;
}

// Re-select [start, end) after a re-render, so repeated toggles keep working on
// the same text without the user re-dragging.
function selectOffsets(el, start, end) {
  const locate = (target) => {
    let count = 0, hit = null;
    const walk = (parent) => {
      for (const child of parent.childNodes) {
        if (hit) return;
        if (child.nodeType === Node.TEXT_NODE) {
          if (count + child.data.length >= target) { hit = [child, target - count]; return; }
          count += child.data.length;
        } else if (child.nodeName === "BR") {
          if (count + 1 > target) { hit = [child.parentNode, 0]; return; }
          count += 1;
        } else {
          walk(child);
        }
      }
    };
    walk(el);
    return hit || [el, el.childNodes.length];
  };
  const [startNode, startOff] = locate(start);
  const [endNode, endOff] = locate(end);
  const range = document.createRange();
  try {
    range.setStart(startNode, startOff);
    range.setEnd(endNode, endOff);
  } catch { return; }
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

// Treat a click that ends a text selection as "the user was selecting text",
// not "the user wants to open the link" — lets path/title stay copyable.
function isSelectingText() {
  const sel = window.getSelection();
  return sel && !sel.isCollapsed && sel.toString().length > 0;
}

function defaultItemState() {
  return {
    selected: false,
    expanded: false,
    selectedSource: null,       // chosen version's source, or null = preferred (versions[0])
    winStart: null,
    winEnd: null,
    winComputed: false,
    editedStart: false,
    editedEnd: false,
    extraCues: [],              // [{cueIndex, start, end, text, origText, highlights}]
    removedCueIndices: new Set(),
    targetText: null,           // edited target text, or null when unchanged
    targetHighlights: [],       // [[start, end), …] offsets into the target text
    videoOverride: null,        // {path, status} | null
  };
}

// A result may bundle several versions (BD/DVD/streaming). Return the version
// dict currently in use for this row (the preferred one unless the user picked
// another). All version-specific reads — timing, text, media, episode_id —
// should go through here, not lastResults[i] directly.
function versions(i) {
  const m = lastResults[i];
  if (m.versions && m.versions.length) return m.versions;
  // Fallback (non-grouped payloads): synthesize one version from the top level.
  return [{
    source: m.source, episode_id: m.episode_id, cue_index: m.cue_index,
    start: m.start, end: m.end, text: m.text, srt_path: m.srt_path,
    media_path: m.media_path, media_status: m.media_status, has_video: m.has_video,
    season: m.season, episodes: m.episodes,
  }];
}

function activeVersion(i) {
  const vs = versions(i);
  const st = itemState[i];
  if (st && st.selectedSource != null) {
    const found = vs.find((v) => (v.source || "") === st.selectedSource);
    if (found) return found;
  }
  // Default to the most-preferred version (list is preference-ordered) that
  // actually has a local video, so a linked release is used even if a
  // higher-preference one isn't on disk. Fall back to the top preference.
  return vs.find((v) => v.has_video) || vs[0];
}

function targetTextOf(i) {
  const st = itemState[i];
  return st && st.targetText != null ? st.targetText : activeVersion(i).text;
}

function effectiveMedia(i) {
  const ov = itemState[i] && itemState[i].videoOverride;
  if (ov) return { path: ov.path, status: ov.status, hasVideo: ov.status === "video" };
  const av = activeVersion(i);
  return { path: av.media_path, status: av.media_status, hasVideo: av.has_video };
}

// --- search -----------------------------------------------------------------

async function runSearch(ev) {
  ev.preventDefault();
  const btn = $("search-btn");
  btn.disabled = true;
  setStatus("Searching…");
  try {
    const resp = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: $("query").value,
        regex: $("regex").checked,
        require_media: $("require-media").checked,
        ai: $("ai").checked,
        only_shows: csv("only-shows"),
        exclude_shows: csv("exclude-shows"),
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "search failed");
    lastResults = data.results;   // server already orders linked results first
    lastQuery = $("query").value;
    lastRegex = $("regex").checked;
    itemState = {};
    currentPage = 0;
    seedItemStates();
    renderPage();
    setStatus(`${data.count} match(es)` + (data.media_root ? "" : "  (no media_root configured — video generation unavailable)"));
    await applyRememberedSeries();  // fills in series linked earlier this session
  } catch (e) {
    setStatus(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

// Create per-result state up front and re-apply any links remembered this session.
function seedItemStates() {
  lastResults.forEach((m, i) => {
    const st = defaultItemState();
    itemState[i] = st;
    const remembered = rememberedVideo[activeVersion(i).episode_id];
    if (remembered) st.videoOverride = { ...remembered };
    st.selected = effectiveMedia(i).hasVideo;
  });
}

// For any series whose folder was chosen earlier, re-pair episodes we haven't
// already remembered individually (e.g. new episodes surfaced by this search).
async function applyRememberedSeries() {
  const bySlug = {};
  lastResults.forEach((m, i) => {
    if (!rememberedSeriesDir[m.show_slug]) return;
    if (rememberedVideo[activeVersion(i).episode_id]) return;   // already handled per-episode
    (bySlug[m.show_slug] = bySlug[m.show_slug] || []).push(i);
  });
  for (const slug of Object.keys(bySlug)) {
    const directory = rememberedSeriesDir[slug];
    const indices = bySlug[slug];
    const items = indices.map((i) => relinkItem(i));
    try {
      const resp = await fetch("/api/relink-series", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items, directory }),
      });
      const data = await resp.json();
      const mapping = data.mapping || {};
      for (const i of indices) {
        const path = mapping[activeVersion(i).episode_id];
        if (path) {
          const status = await probePath(path);
          rememberedVideo[activeVersion(i).episode_id] = { path, status };
          setVideoOverride(i, path, status);
        }
      }
    } catch { /* trust the user; leave unmatched episodes as-is */ }
  }
}

function relinkItem(i) {
  const m = lastResults[i];
  const av = activeVersion(i);
  return { episode_id: av.episode_id, season: m.season, episodes: m.episodes, source: av.source };
}

// --- pagination -------------------------------------------------------------

function pageCount() {
  return Math.max(1, Math.ceil(lastResults.length / perPage));
}

function pageIndices() {
  const start = currentPage * perPage;
  const end = Math.min(start + perPage, lastResults.length);
  const idx = [];
  for (let i = start; i < end; i++) idx.push(i);
  return idx;
}

function renderPage() {
  const list = $("results");
  const tpl = $("result-tpl");
  list.innerHTML = "";
  rowRefs = {};

  const hasResults = lastResults.length > 0;
  $("gen-bar").hidden = !hasResults;
  $("results-bar").hidden = !hasResults;
  if (!hasResults) return;

  if (currentPage >= pageCount()) currentPage = pageCount() - 1;
  for (const i of pageIndices()) {
    const node = tpl.content.cloneNode(true);
    buildRow(node, i);
    list.appendChild(node);
  }
  updatePageBar();
}

function updatePageBar() {
  const total = lastResults.length;
  const pages = pageCount();
  const start = total ? currentPage * perPage + 1 : 0;
  const end = Math.min((currentPage + 1) * perPage, total);
  $("page-info").textContent = `${start}–${end} of ${total} · page ${currentPage + 1}/${pages}`;
  $("prev-page").disabled = currentPage <= 0;
  $("next-page").disabled = currentPage >= pages - 1;
  syncSelectPageCheckbox();
  syncSelectAllCheckbox();
  updateSelCount();
}

function updateSelCount() {
  const n = selectedIndices().length;
  $("sel-count").textContent = n ? `${n} selected` : "";
}

// --- row rendering ----------------------------------------------------------

function buildRow(node, i) {
  const m = lastResults[i];
  const li = node.querySelector(".result");
  node.querySelector(".ep").textContent =
    m.episodes && m.episodes.length ? `S${m.season ?? "?"} E${m.episodes.join(",")}` : "movie";

  const refs = {
    li,
    include: node.querySelector(".include"),
    textEl: node.querySelector(".text"),
    epEl: node.querySelector(".ep"),
    epSep: node.querySelector(".sep-ep"),
    timeEl: node.querySelector(".time"),
    versionSel: node.querySelector(".version"),
    badge: node.querySelector(".badge"),
    editedMark: node.querySelector(".edited-mark"),
    showBtn: node.querySelector(".show"),
    pathBtn: node.querySelector(".path"),
    previewBtn: node.querySelector(".preview-btn"),
    expandBtn: node.querySelector(".expand-btn"),
    video: node.querySelector("video"),
    editor: node.querySelector(".editor"),
    winStartInput: node.querySelector(".win-start"),
    winEndInput: node.querySelector(".win-end"),
    startDot: node.querySelector(".start-dot"),
    endDot: node.querySelector(".end-dot"),
    warnEl: node.querySelector(".timing-warn"),
    linesList: node.querySelector(".lines"),
  };
  rowRefs[i] = refs;

  // Custom single-file / bookmarks datasets label a row by its filename instead
  // of show + episode: show the filename, drop the episode span, and make the
  // label a plain (non-relink) text node.
  if (m.display_name) {
    refs.showBtn.textContent = m.display_name;
    refs.showBtn.classList.remove("link-text");
    refs.showBtn.classList.add("filename");
    refs.showBtn.removeAttribute("role");
    refs.showBtn.removeAttribute("tabindex");
    refs.showBtn.title = "";
    refs.epEl.hidden = true;
    refs.epSep.hidden = true;
  } else {
    refs.showBtn.textContent = m.show_title;
    refs.showBtn.addEventListener("click", () => { if (!isSelectingText()) onShowClick(i); });
    refs.showBtn.addEventListener("keydown", (e) => { if (e.key === "Enter") onShowClick(i); });
  }
  refs.pathBtn.addEventListener("click", () => { if (!isSelectingText()) onPathClick(i); });
  refs.pathBtn.addEventListener("keydown", (e) => { if (e.key === "Enter") onPathClick(i); });

  buildVersionSelect(i);

  refs.include.addEventListener("change", () => {
    itemState[i].selected = refs.include.checked;
    syncSelectPageCheckbox();
    syncSelectAllCheckbox();
    updateSelCount();
  });
  refs.previewBtn.addEventListener("click", () => onPreviewClick(i));
  refs.expandBtn.addEventListener("click", () => toggleExpand(i));
  for (const [inp, which] of [[refs.winStartInput, "start"], [refs.winEndInput, "end"]]) {
    inp.addEventListener("keydown", (e) => onClockKeydown(e, inp));
    inp.addEventListener("focus", () => ensureClockTemplate(inp));
    // Paste/autofill go through the input event, not our keydown handler; keep
    // the strict template and mark the field dirty so blur commits the change.
    inp.addEventListener("input", () => { ensureClockTemplate(inp); inp.dataset.dirty = "1"; });
    // We mutate the value in JS, so the native "change" never fires — commit on
    // blur, but only when the user actually edited the field.
    inp.addEventListener("blur", () => {
      if (inp.dataset.dirty) { delete inp.dataset.dirty; onWinInput(i, which); }
    });
    const label = inp.closest("label");
    label.querySelector(".nudge-up").addEventListener("click", () => nudgeWin(i, which, 1));
    label.querySelector(".nudge-down").addEventListener("click", () => nudgeWin(i, which, -1));
  }

  refs.timeEl.textContent = fmtTime(activeVersion(i).start);
  renderHeaderText(i);
  updateRowMediaDisplay(i);

  // Restore expanded editor if the user had it open before paging away.
  const st = itemState[i];
  if (st.expanded) {
    refs.editor.hidden = false;
    refs.expandBtn.textContent = "Hide timing ▴";
    renderTiming(i);
  }
}

// Populate the per-result version dropdown (BD/DVD/streaming). Hidden unless the
// line exists in more than one version.
function buildVersionSelect(i) {
  const refs = rowRefs[i];
  const sel = refs.versionSel;
  const vs = versions(i);
  sel.innerHTML = "";
  if (vs.length <= 1) { sel.hidden = true; return; }
  sel.hidden = false;
  const active = activeVersion(i);
  vs.forEach((v) => {
    const opt = document.createElement("option");
    const src = v.source || "";
    opt.value = src;
    opt.textContent = (v.source || "unknown") + (v.has_video ? "" : " (no video)");
    if (v === active) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.addEventListener("change", () => onVersionChange(i, sel.value));
}

// Switching version resets this row's per-line editing (timing/extras/text are
// version-specific). A manual file override is deliberately KEPT: the user
// picked that video file for this item, and it should survive a version switch
// so they don't have to re-choose it every time (only the subtitle timing/text
// come from the version).
function onVersionChange(i, source) {
  const st = itemState[i];
  st.selectedSource = source;
  st.winStart = null;
  st.winEnd = null;
  st.winComputed = false;
  st.editedStart = false;
  st.editedEnd = false;
  st.extraCues = [];
  st.removedCueIndices = new Set();
  st.targetText = null;
  st.targetHighlights = [];
  st.selected = effectiveMedia(i).hasVideo;

  const refs = rowRefs[i];
  refs.timeEl.textContent = fmtTime(activeVersion(i).start);
  renderHeaderText(i);
  updateRowMediaDisplay(i);
  if (st.expanded) { ensureWindow(i).then(() => renderTiming(i)); }
  syncSelectPageCheckbox();
  syncSelectAllCheckbox();
  updateSelCount();
}

function renderHeaderText(i) {
  const refs = rowRefs[i];
  if (!refs) return;
  const st = itemState[i];
  if (st && st.targetText != null) {
    refs.textEl.textContent = st.targetText;
  } else {
    refs.textEl.innerHTML = highlight(activeVersion(i).text, lastQuery, lastRegex);
  }
}

function updateRowMediaDisplay(i) {
  const refs = rowRefs[i];
  if (!refs) return;
  const media = effectiveMedia(i);
  refs.badge.textContent = media.status;
  refs.badge.className = "badge " + media.status;
  refs.pathBtn.textContent = media.path || "(no video linked — click to choose a file)";
  refs.include.disabled = !media.hasVideo;
  refs.include.checked = !!(itemState[i] && itemState[i].selected) && media.hasVideo;
  refs.previewBtn.disabled = !media.hasVideo;
}

function selectedIndices() {
  const picked = [];
  lastResults.forEach((_, i) => {
    const st = itemState[i];
    if (st && st.selected && effectiveMedia(i).hasVideo) picked.push(i);
  });
  return picked;
}

// --- select-all (page + all results) ----------------------------------------

function selectablePageIndices() {
  return pageIndices().filter((i) => effectiveMedia(i).hasVideo);
}

function selectableAllIndices() {
  const out = [];
  lastResults.forEach((_, i) => { if (effectiveMedia(i).hasVideo) out.push(i); });
  return out;
}

function syncSelectPageCheckbox() {
  const sel = selectablePageIndices();
  const cb = $("select-page");
  cb.checked = sel.length > 0 && sel.every((i) => itemState[i].selected);
  cb.disabled = sel.length === 0;
}

function syncSelectAllCheckbox() {
  const sel = selectableAllIndices();
  const cb = $("select-all");
  cb.checked = sel.length > 0 && sel.every((i) => itemState[i].selected);
  cb.disabled = sel.length === 0;
}

function onSelectPageToggle() {
  const target = $("select-page").checked;
  for (const i of selectablePageIndices()) {
    itemState[i].selected = target;
    if (rowRefs[i]) rowRefs[i].include.checked = target;
  }
  syncSelectAllCheckbox();
  updateSelCount();
}

function onSelectAllToggle() {
  const target = $("select-all").checked;
  for (const i of selectableAllIndices()) {
    itemState[i].selected = target;
    if (rowRefs[i]) rowRefs[i].include.checked = target;
  }
  syncSelectPageCheckbox();
  updateSelCount();
}

// --- preview ----------------------------------------------------------------

async function onPreviewClick(i) {
  const media = effectiveMedia(i);
  if (!media.hasVideo) return;
  const av = activeVersion(i);
  const state = itemState[i];
  const refs = rowRefs[i];

  // The preview always overlays subtitles (the target line plus any auto/added
  // neighbors), so resolve the window + extra lines first — the same lines
  // generation would use. Styling follows the burn-in options when set.
  await ensureWindow(i);

  let winStart, winEnd;
  const params = new URLSearchParams({ episode_id: av.episode_id });
  if (state.winStart != null) {
    winStart = state.winStart; winEnd = state.winEnd;
    params.set("win_start", winStart);
    params.set("win_end", winEnd);
  } else {
    winStart = Math.max(0, av.start - currentPad());
    winEnd = av.end + currentPad();
    params.set("start", av.start);
    params.set("end", av.end);
    params.set("pad", currentPad());
  }
  if (state.videoOverride && state.videoOverride.path) {
    params.set("video_override", state.videoOverride.path);
  }

  clearPreviewTrack(refs);
  refs.video.src = `/api/preview?${params.toString()}`;
  attachPreviewTrack(i, refs, winStart, winEnd);
  refs.video.hidden = false;
  refs.previewBtn.disabled = true;
  refs.previewBtn.textContent = "loading…";
  refs.video.addEventListener("loadeddata", () => {
    refs.previewBtn.textContent = "▶ Preview";
    refs.previewBtn.disabled = !effectiveMedia(i).hasVideo;
  }, { once: true });
  refs.video.play().catch(() => {});
}

// Build the subtitle cues shown over a burn-in preview: the target line plus
// any auto/added extras, clamped to the preview window and offset to its start.
function previewCues(i, winStart, winEnd) {
  const av = activeVersion(i);
  const state = itemState[i];
  const cues = [{ start: av.start, end: av.end, text: targetTextOf(i),
                  highlights: state.targetHighlights }];
  for (const e of state.extraCues) {
    cues.push({ start: e.start, end: e.end, text: e.text, highlights: e.highlights });
  }
  return cues
    .filter((c) => c.end > winStart + EPS && c.start < winEnd - EPS && c.text)
    .map((c) => ({
      start: Math.max(0, c.start - winStart),
      end: Math.min(winEnd - winStart, c.end - winStart),
      text: c.text,
      highlights: c.highlights || [],
    }))
    .filter((c) => c.end > c.start)
    .sort((a, b) => a.start - b.start);
}

// A WebVTT blob whose embedded STYLE block mirrors the burn-in options, so the
// preview is styled without re-encoding the video.
function previewVttUrl(i, winStart, winEnd) {
  const cues = previewCues(i, winStart, winEnd);
  const color = $("burn-color").value || "#ffffff";
  const font = $("burn-font").value || "Arial";
  const outline = parseInt($("burn-outline").value, 10) || 0;
  const size = parseInt($("burn-size").value, 10) || 28;
  const shadow = [];
  for (let dx = -outline; dx <= outline; dx++) {
    for (let dy = -outline; dy <= outline; dy++) {
      if (dx || dy) shadow.push(`${dx}px ${dy}px 0 #000`);
    }
  }
  const cueStyle =
    `::cue { color: ${color}; font-family: "${font.replace(/"/g, "")}", sans-serif;` +
    ` font-size: ${Math.max(50, Math.round((size / 28) * 100))}%; background: transparent;` +
    (shadow.length ? ` text-shadow: ${shadow.join(", ")};` : "") + " }";
  // Highlighted spans get the same yellow the SRT/burn-in will use.
  const hlStyle = `::cue(.hl) { color: ${HIGHLIGHT_COLOR}; }`;
  let vtt = "WEBVTT\n\nSTYLE\n" + cueStyle + "\n\nSTYLE\n" + hlStyle + "\n";
  cues.forEach((c, n) => {
    vtt += `\n${n + 1}\n${fmtClock(c.start)} --> ${fmtClock(c.end)}\n${vttCueText(c)}\n`;
  });
  return URL.createObjectURL(new Blob([vtt], { type: "text/vtt" }));
}

// Wrap the cue's highlight ranges in WebVTT class spans so the preview matches
// what the burn-in will render.
function vttCueText(cue) {
  const spans = mergeRanges(cue.highlights, cue.text.length);
  if (!spans.length) return cue.text;
  let out = "", cursor = 0;
  for (const [s, e] of spans) {
    out += cue.text.slice(cursor, s) + "<c.hl>" + cue.text.slice(s, e) + "</c>";
    cursor = e;
  }
  return out + cue.text.slice(cursor);
}

function clearPreviewTrack(refs) {
  if (refs.previewTrackUrl) { URL.revokeObjectURL(refs.previewTrackUrl); refs.previewTrackUrl = null; }
  refs.video.querySelectorAll("track").forEach((t) => t.remove());
}

function attachPreviewTrack(i, refs, winStart, winEnd) {
  const url = previewVttUrl(i, winStart, winEnd);
  refs.previewTrackUrl = url;
  const track = document.createElement("track");
  track.kind = "subtitles";
  track.label = "Burn-in preview";
  track.default = true;
  track.src = url;
  refs.video.appendChild(track);
  // Force the track visible once it loads (default alone isn't always honored).
  const show = () => { if (refs.video.textTracks[0]) refs.video.textTracks[0].mode = "showing"; };
  refs.video.addEventListener("loadedmetadata", show, { once: true });
  track.addEventListener("load", show, { once: true });
}

// --- item timing / extras ---------------------------------------------------

async function ensureEpisodeCues(episodeId) {
  if (episodeCues[episodeId]) return episodeCues[episodeId];
  try {
    const r = await fetch(`/api/cues?episode_id=${encodeURIComponent(episodeId)}`);
    const d = await r.json();
    episodeCues[episodeId] = d.cues || [];
  } catch {
    episodeCues[episodeId] = [];
  }
  return episodeCues[episodeId];
}

// Compute the default cut window and auto-included lines for a result that the
// user hasn't hand-edited. Runs once (lazily) per result: on render, on expand,
// and before generation. The default is [start-pad, end+pad], but if that start
// lands in the MIDDLE of a neighboring subtitle, the window is extended back to
// that line's start so the line is shown whole (not counted as a manual edit).
async function ensureWindow(i) {
  const state = itemState[i];
  if (state.winComputed || state.winStart != null) { state.winComputed = true; return; }
  const av = activeVersion(i);
  const cues = await ensureEpisodeCues(av.episode_id);
  const pad = currentPad();
  let winStart = Math.max(0, av.start - pad);
  const winEnd = av.end + pad;
  for (const c of cues) {
    if (c.cue_index === av.cue_index) continue;
    // straddles the padded start → pull the window back to include it whole
    if (c.start < winStart - EPS && c.end > winStart + EPS) {
      if (c.start < winStart) winStart = c.start;
    }
  }
  state.winStart = winStart;
  state.winEnd = winEnd;
  recomputeExtras(i, { forward: false });
  state.winComputed = true;
}

function recomputeExtras(i, { forward }) {
  const state = itemState[i];
  const av = activeVersion(i);
  const cues = episodeCues[av.episode_id] || [];

  for (const c of cues) {
    if (c.cue_index === av.cue_index) continue;
    if (state.removedCueIndices.has(c.cue_index)) continue;
    const contained = c.start >= state.winStart - EPS && c.end <= state.winEnd + EPS;
    if (!contained) continue;
    const isBackward = c.end <= av.start + EPS;
    if (!isBackward && !forward) continue;
    if (!state.extraCues.some((e) => e.cueIndex === c.cue_index)) {
      state.extraCues.push({ cueIndex: c.cue_index, start: c.start, end: c.end,
                             text: c.text, origText: c.text, highlights: [] });
    }
  }
  // drop extras that no longer fit the (possibly shrunk) window
  state.extraCues = state.extraCues.filter(
    (e) => e.start >= state.winStart - EPS && e.end <= state.winEnd + EPS
  );
  state.extraCues.sort((a, b) => a.start - b.start);
}

async function toggleExpand(i) {
  const refs = rowRefs[i];
  const state = itemState[i];
  state.expanded = !state.expanded;
  refs.editor.hidden = !state.expanded;
  refs.expandBtn.textContent = state.expanded ? "Hide timing ▴" : "Edit timing ▾";
  if (!state.expanded) { hideHighlightButton(); return; }
  await ensureWindow(i);
  renderTiming(i);
}

function renderTiming(i) {
  const refs = rowRefs[i];
  const state = itemState[i];
  if (!refs || state.winStart == null) return;
  refs.winStartInput.value = fmtClock(state.winStart);
  refs.winEndInput.value = fmtClock(state.winEnd);
  refs.startDot.hidden = !state.editedStart;
  refs.endDot.hidden = !state.editedEnd;
  refs.editedMark.hidden = !(state.editedStart || state.editedEnd);
  renderLines(i);
}

function renderLines(i) {
  const refs = rowRefs[i];
  const state = itemState[i];
  const av = activeVersion(i);
  const tpl = $("line-tpl");
  refs.linesList.innerHTML = "";

  // Target line first (editable, resettable, not removable), then extras.
  const rows = [{ target: true, start: av.start, end: av.end }].concat(
    state.extraCues.map((e) => ({ target: false, cue: e, start: e.start, end: e.end }))
  ).sort((a, b) => a.start - b.start);

  for (const row of rows) {
    const node = tpl.content.cloneNode(true);
    const li = node.querySelector(".line");
    li.classList.toggle("target", row.target);
    node.querySelector(".line-time").textContent = `${fmtClock(row.start)} – ${fmtClock(row.end)}`;
    const input = node.querySelector(".line-text");
    const resetBtn = node.querySelector(".line-reset");
    const removeBtn = node.querySelector(".line-remove");
    node.querySelector(".line-tag").textContent = row.target ? "target" : "";

    const orig = row.target ? av.text : row.cue.origText;
    const cur = row.target ? targetTextOf(i) : row.cue.text;
    // The editor owns highlight state for its line; hlOwner lets the floating
    // Highlight button find and write it back without knowing about rows.
    input._hlOwner = {
      resultIndex: i,
      getHighlights: () => (row.target ? state.targetHighlights : row.cue.highlights) || [],
      setHighlights: (ranges) => {
        if (row.target) state.targetHighlights = ranges;
        else row.cue.highlights = ranges;
      },
    };
    renderLineText(input, cur, input._hlOwner.getHighlights());

    const markEdited = () => {
      const { text, highlights } = readLineText(input);
      const edited = text !== orig || highlights.length > 0;
      input.classList.toggle("edited", edited);
      resetBtn.hidden = !edited;
    };
    markEdited();

    // Read the DOM back into state on every keystroke, but never re-render it
    // here — replacing innerHTML mid-typing would destroy the caret.
    input.addEventListener("input", () => {
      const { text, highlights } = readLineText(input);
      input._hlOwner.setHighlights(highlights);
      if (row.target) {
        state.targetText = text === av.text ? null : text;
        renderHeaderText(i);
      } else {
        row.cue.text = text;
      }
      markEdited();
    });
    // Enter would otherwise insert a <div>/<p>; a line break matches the SRT.
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        document.execCommand("insertLineBreak");
      }
    });
    // Paste plain text only, so no foreign markup enters the editor.
    input.addEventListener("paste", (e) => {
      e.preventDefault();
      const text = (e.clipboardData || window.clipboardData).getData("text");
      document.execCommand("insertText", false, text.replace(/\r\n?/g, "\n"));
    });
    resetBtn.addEventListener("click", () => {
      input._hlOwner.setHighlights([]);
      renderLineText(input, orig, []);
      if (row.target) { state.targetText = null; renderHeaderText(i); }
      else { row.cue.text = orig; }
      hideHighlightButton();
      markEdited();
    });
    input._hlOwner.markEdited = markEdited;

    if (row.target) {
      removeBtn.hidden = true;
    } else {
      removeBtn.addEventListener("click", () => onExtraRemove(i, row.cue.cueIndex));
    }
    refs.linesList.appendChild(node);
  }
}

// --- floating highlight button ----------------------------------------------
// Emphasis works like bold: select text, press the button, press it again on the
// same text to clear it. The button follows the selection instead of sitting in
// a fixed spot, so it's always next to what it acts on.

// The .line-text the current selection lives in, or null. Both endpoints must be
// in the same editor — a selection spanning two lines has no single owner.
function selectedLineEditor() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  const owner = (node) => {
    let el = node && node.nodeType === Node.TEXT_NODE ? node.parentNode : node;
    while (el && el !== document.body) {
      if (el.classList && el.classList.contains("line-text")) return el;
      el = el.parentNode;
    }
    return null;
  };
  const a = owner(sel.anchorNode);
  return a && a === owner(sel.focusNode) ? a : null;
}

// Selection as [start, end) offsets into the editor's flat text.
function selectionOffsets(el) {
  const sel = window.getSelection();
  const a = offsetInElement(el, sel.anchorNode, sel.anchorOffset);
  const b = offsetInElement(el, sel.focusNode, sel.focusOffset);
  return [Math.min(a, b), Math.max(a, b)];
}

function hideHighlightButton() {
  $("hl-float").hidden = true;
}

function syncHighlightButton() {
  const btn = $("hl-float");
  const el = selectedLineEditor();
  if (!el) { hideHighlightButton(); return; }
  const [start, end] = selectionOffsets(el);
  if (end <= start) { hideHighlightButton(); return; }

  const covered = rangesCover(el._hlOwner ? el._hlOwner.getHighlights() : [], start, end);
  btn.textContent = covered ? "🖍 Remove highlight" : "🖍 Highlight";
  btn.hidden = false;
  // Measure only after unhiding, or offsetWidth is 0 and the button sits left.
  const rect = window.getSelection().getRangeAt(0).getBoundingClientRect();
  const left = rect.left + window.scrollX + rect.width / 2 - btn.offsetWidth / 2;
  btn.style.left = `${Math.max(4, Math.min(left, window.innerWidth - btn.offsetWidth - 4))}px`;
  btn.style.top = `${rect.top + window.scrollY - btn.offsetHeight - 6}px`;
}

function toggleHighlight() {
  const el = selectedLineEditor();
  if (!el || !el._hlOwner) return;
  const [start, end] = selectionOffsets(el);
  if (end <= start) return;

  const { text } = readLineText(el);
  const current = el._hlOwner.getHighlights();
  const next = rangesCover(current, start, end)
    ? removeRange(current, start, end, text.length)
    : addRange(current, start, end, text.length);
  el._hlOwner.setHighlights(next);
  renderLineText(el, text, next);
  selectOffsets(el, start, end);        // keep the selection so toggling repeats
  if (el._hlOwner.markEdited) el._hlOwner.markEdited();
  syncHighlightButton();
}

document.addEventListener("selectionchange", syncHighlightButton);
// mousedown would collapse the selection before click fires.
$("hl-float").addEventListener("mousedown", (e) => e.preventDefault());
$("hl-float").addEventListener("click", toggleHighlight);
window.addEventListener("scroll", hideHighlightButton, { passive: true });
window.addEventListener("resize", hideHighlightButton);

function onWinInput(i, which) {
  const refs = rowRefs[i];
  const state = itemState[i];
  const av = activeVersion(i);
  const input = which === "start" ? refs.winStartInput : refs.winEndInput;
  let value = parseClock(input.value);
  if (Number.isNaN(value)) value = which === "start" ? state.winStart : state.winEnd;

  let warn = "";
  if (which === "start") {
    if (value > av.start) { value = av.start; warn = `Start can't be later than this line's own start (${fmtClock(av.start)}).`; }
    if (value < 0) value = 0;
    state.winStart = value;
    state.editedStart = true;
  } else {
    if (value < av.end) { value = av.end; warn = `End can't be earlier than this line's own end (${fmtClock(av.end)}).`; }
    state.winEnd = value;
    state.editedEnd = true;
  }
  // A manual timing edit re-includes lines the user may have removed by mistake:
  // clear the sticky removals so everything inside the window comes back.
  state.removedCueIndices = new Set();
  recomputeExtras(i, { forward: true });
  renderTiming(i);
  refs.warnEl.hidden = !warn;
  refs.warnEl.textContent = warn;
}

// Nudge a window edge by `delta` seconds, then run it through onWinInput so the
// same clamping / re-inclusion / render happens as a manual edit.
function nudgeWin(i, which, delta) {
  const refs = rowRefs[i];
  const state = itemState[i];
  const input = which === "start" ? refs.winStartInput : refs.winEndInput;
  const base = which === "start" ? state.winStart : state.winEnd;
  input.value = fmtClock(Math.max(0, base + delta));
  onWinInput(i, which);
}

function onExtraRemove(i, cueIndex) {
  const state = itemState[i];
  state.removedCueIndices.add(cueIndex);
  state.extraCues = state.extraCues.filter((e) => e.cueIndex !== cueIndex);
  renderTiming(i);
}

// --- custom linking (browse modal + relink) ---------------------------------

async function probePath(path) {
  try {
    const r = await fetch(`/api/probe?path=${encodeURIComponent(path)}`);
    const d = await r.json();
    return d.status || "unknown";
  } catch {
    return "unknown";
  }
}

function setVideoOverride(i, path, status) {
  itemState[i].videoOverride = { path, status };
  if (status === "video") itemState[i].selected = true;
  updateRowMediaDisplay(i);
  syncSelectPageCheckbox();
  syncSelectAllCheckbox();
  updateSelCount();
}

function clearVideoOverrideAsUnlinked(i) {
  itemState[i].videoOverride = { path: null, status: "unlinked" };
  itemState[i].selected = false;
  updateRowMediaDisplay(i);
  syncSelectPageCheckbox();
  syncSelectAllCheckbox();
  updateSelCount();
}

function onPathClick(i) {
  const av = activeVersion(i);
  openBrowse({
    mode: "file",
    onChoose: async (path) => {
      const status = await probePath(path);
      rememberedVideo[av.episode_id] = { path, status };
      setVideoOverride(i, path, status);
      const others = lastResults
        .map((_, idx) => idx)
        .filter((idx) => idx !== i && activeVersion(idx).episode_id === av.episode_id);
      if (others.length) {
        openConfirm(
          `Apply this video to ${others.length} other result(s) from the same episode?`,
          () => others.forEach((idx) => setVideoOverride(idx, path, status))
        );
      }
    },
  });
}

function onShowClick(i) {
  const m = lastResults[i];
  openBrowse({
    mode: "dir",
    onChoose: async (directory) => {
      rememberedSeriesDir[m.show_slug] = directory;
      const seriesIdx = lastResults
        .map((_, idx) => idx)
        .filter((idx) => lastResults[idx].show_slug === m.show_slug);
      const items = seriesIdx.map((idx) => relinkItem(idx));
      const resp = await fetch("/api/relink-series", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items, directory }),
      });
      const data = await resp.json();
      const mapping = data.mapping || {};
      for (const idx of seriesIdx) {
        const path = mapping[activeVersion(idx).episode_id];
        if (path) {
          const status = await probePath(path);
          rememberedVideo[activeVersion(idx).episode_id] = { path, status };
          setVideoOverride(idx, path, status);
        } else {
          clearVideoOverrideAsUnlinked(idx);
        }
      }
    },
  });
}

// --- browse / confirm modals ------------------------------------------------

let browseState = null;

function openBrowse({ mode, onChoose, exts, title }) {
  browseState = { mode, path: "", onChoose, exts: exts || null };
  $("browse-title").textContent = title || (mode === "dir" ? "Choose a folder" : "Choose a video file");
  $("browse-choose-dir").hidden = mode !== "dir";
  $("browse-modal").hidden = false;
  browseLoad("");
}

async function browseLoad(path) {
  const params = new URLSearchParams();
  if (browseState.exts) {
    params.set("exts", browseState.exts.join(","));   // filter files by suffix
  } else {
    params.set("videos_only", browseState.mode === "file" ? "1" : "0");
  }
  if (path) params.set("path", path);
  const r = await fetch(`/api/browse?${params.toString()}`);
  const data = await r.json();
  if (!r.ok) {
    $("browse-path").textContent = data.error || "could not list directory";
    return;
  }
  browseState.path = data.path;
  $("browse-path").textContent = data.path;
  const ul = $("browse-list");
  ul.innerHTML = "";
  data.entries.forEach((entry) => {
    const li = document.createElement("li");
    li.textContent = entry.name;
    li.className = entry.is_dir ? "dir" : "file";
    li.addEventListener("click", () => {
      if (entry.is_dir) {
        browseLoad(entry.path);
      } else {
        const cb = browseState.onChoose;
        closeBrowse();
        cb(entry.path);
      }
    });
    ul.appendChild(li);
  });
}

function closeBrowse() {
  $("browse-modal").hidden = true;
  browseState = null;
}

$("browse-cancel").addEventListener("click", closeBrowse);
$("browse-choose-dir").addEventListener("click", () => {
  const cb = browseState.onChoose;
  const path = browseState.path;
  closeBrowse();
  cb(path);
});

let confirmState = null;

function openConfirm(title, onYes) {
  confirmState = { onYes };
  $("confirm-title").textContent = title;
  $("confirm-modal").hidden = false;
}

function closeConfirm() {
  $("confirm-modal").hidden = true;
  confirmState = null;
}

$("confirm-yes").addEventListener("click", () => {
  const cb = confirmState && confirmState.onYes;
  closeConfirm();
  if (cb) cb();
});
$("confirm-no").addEventListener("click", closeConfirm);

// --- burn-in style ----------------------------------------------------------

function hexToAssColor(hex) {
  hex = (hex || "#ffffff").replace("#", "");
  const r = hex.slice(0, 2), g = hex.slice(2, 4), b = hex.slice(4, 6);
  return `&H00${b}${g}${r}`.toUpperCase();
}

$("burn-in").addEventListener("change", () => {
  $("burn-opts").hidden = !$("burn-in").checked;
});

// Bundling is only meaningful once there's more than one file.
$("separate-files").addEventListener("change", () => {
  document.querySelector(".zip-toggle").hidden = !$("separate-files").checked;
});

// --- generate ---------------------------------------------------------------

function buildPayload(i) {
  const state = itemState[i];
  const av = activeVersion(i);
  // Flatten the ACTIVE version into the match dict so the server cuts/labels the
  // chosen release. Keep version-independent fields from the top-level result.
  const m = lastResults[i];
  const payload = {
    ...m,
    episode_id: av.episode_id,
    cue_index: av.cue_index,
    start: av.start,
    end: av.end,
    text: av.text,
    srt_path: av.srt_path,
    media_path: av.media_path,
    media_status: av.media_status,
    has_video: av.has_video,
    source: av.source,
    season: av.season != null ? av.season : m.season,
    episodes: av.episodes || m.episodes,
  };
  if (!state) return payload;
  if (state.targetText != null) payload.text = state.targetText;
  if (state.targetHighlights && state.targetHighlights.length) {
    payload.highlights = state.targetHighlights;
  }
  if (state.videoOverride && state.videoOverride.path) {
    payload.video_override = state.videoOverride.path;
  }
  if (state.winStart != null) {
    payload.win_start = state.winStart;
    payload.win_end = state.winEnd;
  }
  if (state.extraCues.length) {
    payload.extra_cues = state.extraCues.map((e) => ({
      start: e.start, end: e.end, text: e.text, highlights: e.highlights || [],
    }));
  }
  return payload;
}

function downloadUrl(name) {
  return `/api/download/${encodeURIComponent(name)}`;
}

// One link per generated file (they all live in the clips/ output dir), plus a
// "Download all" shortcut when there are several. Browsers throttle a burst of
// programmatic clicks, hence the stagger; the first one may prompt to allow
// multiple downloads.
function renderDownloads(names) {
  if (!names.length) return;
  const status = $("gen-status");
  if (names.length > 1) {
    const all = document.createElement("button");
    all.type = "button";
    all.id = "download-all";
    all.textContent = `↓ Download all (${names.length})`;
    all.addEventListener("click", () => {
      names.forEach((name, n) => setTimeout(() => {
        const a = document.createElement("a");
        a.href = downloadUrl(name);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
      }, n * 300));
    });
    status.append(all);
  }
  const list = document.createElement("ul");
  list.className = "download-list";
  for (const name of names) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = downloadUrl(name);
    a.textContent = name;
    li.append(a);
    list.append(li);
  }
  status.append(list);
}

async function runGenerate() {
  const indices = selectedIndices();
  if (indices.length === 0) { $("gen-status").textContent = "Select at least one result with linked video."; return; }
  const btn = $("generate-btn");
  btn.disabled = true;
  $("gen-status").textContent = `Generating from ${indices.length} clip(s)… this can take a while.`;
  try {
    // Make sure default windows/extras are computed even for selected rows the
    // user never expanded or paged to.
    for (const i of indices) await ensureWindow(i);
    const body = {
      matches: indices.map((i) => buildPayload(i)),
      pad: currentPad(),
      name: $("out-name").value,
      separate: $("separate-files").checked,
      zip: $("zip-bundle").checked,
    };
    if ($("burn-in").checked) {
      body.burn_in = {
        font: $("burn-font").value || "Arial",
        size: parseInt($("burn-size").value, 10) || 28,
        primary_color: hexToAssColor($("burn-color").value),
        outline_color: "&H00000000",
        outline: parseInt($("burn-outline").value, 10) || 2,
      };
    }
    const resp = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "generation failed");
    $("gen-status").innerHTML = "";
    $("gen-status").append(document.createTextNode(`Done: ${data.generated} clip(s).`));
    renderDownloads(data.downloads || (data.download ? [data.download] : []));
    if (data.skipped && data.skipped.length) {
      const div = document.createElement("div");
      div.style.color = "var(--warn)";
      div.textContent = "Skipped: " + data.skipped.map((s) => `${s.label} (${s.reason})`).join("; ");
      $("gen-status").append(div);
    }
  } catch (e) {
    $("gen-status").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function checkHealth() {
  try {
    const r = await fetch("/api/health");
    const d = await r.json();
    if (!d.ffmpeg) $("ffmpeg-warn").hidden = false;
  } catch { /* ignore */ }
}

// --- settings menu ----------------------------------------------------------

function setSettingsStatus(msg, isError = false) {
  const el = $("settings-status");
  el.textContent = msg;
  el.style.color = isError ? "var(--err)" : "var(--muted)";
}

function applySettingsToForm(d) {
  $("set-subtitle-root").value = d.subtitle_root || "";
  $("set-media-root").value = d.media_root || "";
  if (d.resolution) $("set-resolution").value = d.resolution;
  if (d.quality) $("set-quality").value = d.quality;
  if (d.container) {
    $("set-container").value = d.container;
    // The output-name box holds a stem only; the container supplies the suffix.
    $("out-ext").textContent = "." + d.container;
  }
  $("set-subtitle-hint").textContent = d.effective_subtitle_root
    ? "Currently searching: " + d.effective_subtitle_root
    : "No subtitle root set — configure config/corpus.toml or point to a folder above.";
  $("set-media-hint").textContent = d.effective_media_root
    ? "Currently using: " + d.effective_media_root
    : "No media root set — video linking is unavailable until one is set.";
}

async function loadSettings() {
  try {
    const r = await fetch("/api/settings");
    applySettingsToForm(await r.json());
  } catch { /* keep form defaults */ }
}

function toggleSettings(open) {
  const menu = $("settings-menu");
  const show = open != null ? open : menu.hidden;
  menu.hidden = !show;
  $("settings-btn").setAttribute("aria-expanded", show ? "true" : "false");
}

// Re-run the current query so new media/output settings take effect immediately.
function rerunSearch() {
  if ($("query").value.trim()) $("search-form").requestSubmit();
}

async function saveSettings() {
  const btn = $("settings-save");
  btn.disabled = true;
  setSettingsStatus("Saving…");
  try {
    const r = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subtitle_root: $("set-subtitle-root").value,
        media_root: $("set-media-root").value,
        resolution: $("set-resolution").value,
        quality: $("set-quality").value,
        container: $("set-container").value,
      }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || "save failed");
    applySettingsToForm(d);
    setSettingsStatus("Saved to " + (d.path || "settings file") + ".");
    rerunSearch();
  } catch (e) {
    setSettingsStatus(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function refreshSubtitleData() {
  const btn = $("settings-refresh");
  btn.disabled = true;
  setSettingsStatus("Rebuilding subtitle data…");
  try {
    const r = await fetch("/api/refresh", { method: "POST" });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || "refresh failed");
    setSettingsStatus(`Rebuilt — ${d.count} episode(s) indexed.`);
    await loadSettings();
    rerunSearch();
  } catch (e) {
    setSettingsStatus(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

$("settings-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleSettings(); });
$("settings-menu").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => toggleSettings(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleSettings(false); });
$("set-subtitle-browse").addEventListener("click", () => {
  openBrowse({ mode: "dir", onChoose: (path) => { $("set-subtitle-root").value = path; } });
});
$("set-media-browse").addEventListener("click", () => {
  openBrowse({ mode: "dir", onChoose: (path) => { $("set-media-root").value = path; } });
});
$("settings-save").addEventListener("click", saveSettings);
$("settings-refresh").addEventListener("click", refreshSubtitleData);
loadSettings();

// --- custom dataset bar -----------------------------------------------------

function renderDatasetBar(info) {
  const active = !!(info && info.active);
  $("dataset-current").textContent = active
    ? `Custom — ${info.label || ""}`
    : "Master";
  $("dataset-load").hidden = active;
  $("ds-back").hidden = !active;
}

async function loadDatasetInfo() {
  try {
    const r = await fetch("/api/dataset");
    renderDatasetBar(await r.json());
  } catch { /* leave the default (Master) bar */ }
}

async function setDataset(mode, path) {
  $("dataset-status").textContent = "Loading…";
  try {
    const r = await fetch("/api/dataset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, path }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || "could not load dataset");
    renderDatasetBar({ active: true, label: d.label, count: d.count });
    $("dataset-status").textContent = `${d.count} entr${d.count === 1 ? "y" : "ies"} loaded.`;
    // Session links from the master corpus don't apply to a custom dataset.
    rememberedVideo = {};
    rememberedSeriesDir = {};
    $("search-form").requestSubmit();   // empty query lists the whole dataset
  } catch (e) {
    $("dataset-status").textContent = e.message;
  }
}

async function backToMaster() {
  $("dataset-status").textContent = "";
  try {
    await fetch("/api/dataset/master", { method: "POST" });
  } catch { /* ignore */ }
  renderDatasetBar({ active: false });
  rememberedVideo = {};
  rememberedSeriesDir = {};
  $("search-form").requestSubmit();
}

$("ds-srt").addEventListener("click", () => openBrowse({
  mode: "file", exts: [".srt"], title: "Choose an SRT file",
  onChoose: (p) => setDataset("srt", p),
}));
$("ds-dir").addEventListener("click", () => openBrowse({
  mode: "dir", title: "Choose a folder of SRTs",
  onChoose: (p) => setDataset("dir", p),
}));
$("ds-bookmarks").addEventListener("click", () => openBrowse({
  mode: "file", exts: [".bookmarks"], title: "Choose a .SE.bookmarks file",
  onChoose: (p) => setDataset("bookmarks", p),
}));
$("ds-back").addEventListener("click", backToMaster);
loadDatasetInfo();

// --- wiring -----------------------------------------------------------------

$("search-form").addEventListener("submit", runSearch);
$("generate-btn").addEventListener("click", runGenerate);
$("select-page").addEventListener("change", onSelectPageToggle);
$("select-all").addEventListener("change", onSelectAllToggle);
$("prev-page").addEventListener("click", () => { if (currentPage > 0) { currentPage--; renderPage(); } });
$("next-page").addEventListener("click", () => { if (currentPage < pageCount() - 1) { currentPage++; renderPage(); } });
$("per-page").addEventListener("change", () => {
  perPage = parseInt($("per-page").value, 10) || 25;
  currentPage = 0;
  renderPage();
});
checkHealth();
