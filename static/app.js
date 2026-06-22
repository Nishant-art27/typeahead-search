/* ============================================================
   Search Typeahead — frontend logic
   - debounced suggestions (cache -> index on the server)
   - keyboard navigation, prefix highlighting, trending badges
   - search submission (dummy API) + live system dashboard
   ============================================================ */
"use strict";

const $ = (sel) => document.querySelector(sel);

const els = {
  form: $("#searchForm"),
  box: $("#searchBox"),
  input: $("#searchInput"),
  list: $("#suggestions"),
  spinner: $("#spinner"),
  toast: $("#toast"),
  sourcePill: $("#sourcePill"),
  segBtns: document.querySelectorAll(".seg-btn"),
  trendingChips: $("#trendingChips"),
  // dashboard
  dashToggle: $("#dashToggle"),
  dashboard: $("#dashboard"),
  healthDot: $("#healthDot"),
  mIndexed: $("#mIndexed"),
  mHit: $("#mHit"),
  mP95: $("#mP95"),
  mP50: $("#mP50"),
  mSearches: $("#mSearches"),
  mWrites: $("#mWrites"),
  mReduction: $("#mReduction"),
  mBuffer: $("#mBuffer"),
  cacheNodes: $("#cacheNodes"),
  debugInput: $("#debugInput"),
  debugBtn: $("#debugBtn"),
  debugOut: $("#debugOut"),
};

const state = {
  recency: true,        // ranking mode
  items: [],            // current suggestions
  activeIndex: -1,      // keyboard-highlighted item
  seq: 0,               // request token to drop stale responses
  toastTimer: null,
};

/* ---------- helpers ---------- */
function formatCount(n) {
  if (n === null || n === undefined) return "";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(n >= 100_000 ? 0 : 1) + "K";
  return String(n);
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function normalize(s) {
  return (s || "").toLowerCase().replace(/\s+/g, " ").trim();
}

/* highlight: show typed prefix muted, completion bold */
function highlight(query, prefix) {
  const q = escapeHtml(query);
  const plen = prefix.length;
  if (plen && query.toLowerCase().startsWith(prefix)) {
    return `<span class="typed">${escapeHtml(query.slice(0, plen))}</span><b>${escapeHtml(query.slice(plen))}</b>`;
  }
  return `<b>${q}</b>`;
}

/* ---------- suggestions ---------- */
let debounceTimer = null;
function onInput() {
  const raw = els.input.value;
  clearTimeout(debounceTimer);
  if (!raw.trim()) { closeSuggestions(); return; }
  // Debounce so we don't hit the backend on every keystroke.
  debounceTimer = setTimeout(() => fetchSuggestions(raw), 160);
}

async function fetchSuggestions(raw) {
  const token = ++state.seq;
  const prefix = normalize(raw);
  if (!prefix) { closeSuggestions(); return; }
  els.spinner.hidden = false;
  try {
    const url = `/suggest?q=${encodeURIComponent(prefix)}&recency=${state.recency}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error("suggest failed");
    const data = await res.json();
    if (token !== state.seq) return; // a newer keystroke won — drop this result
    renderSuggestions(data, prefix);
  } catch (e) {
    if (token === state.seq) showToast("Could not load suggestions", true);
  } finally {
    if (token === state.seq) els.spinner.hidden = true;
  }
}

function renderSuggestions(data, prefix) {
  const items = data.suggestions || [];
  state.items = items;
  state.activeIndex = -1;
  els.sourcePill.textContent = `${data.source} · ${state.recency ? "recency" : "popularity"}`;

  if (!items.length) {
    els.list.innerHTML = `<li class="sugg-empty">No suggestions for “${escapeHtml(prefix)}”.</li>`;
    openSuggestions();
    return;
  }

  els.list.innerHTML = items.map((it, i) => {
    const flame = it.trending ? `<span class="s-flame" title="recently searched">🔥</span>` : "";
    return `
      <li class="sugg-item" role="option" id="opt-${i}" data-index="${i}">
        <span class="s-ico">⌕</span>
        <span class="sugg-text">${highlight(it.query, prefix)}</span>
        ${flame}
        <span class="s-count">${formatCount(it.count)}</span>
      </li>`;
  }).join("");

  els.list.querySelectorAll(".sugg-item").forEach((li) => {
    li.addEventListener("mousedown", (ev) => {
      ev.preventDefault(); // keep focus in the input
      const idx = Number(li.dataset.index);
      submitSearch(state.items[idx].query);
    });
    li.addEventListener("mouseenter", () => setActive(Number(li.dataset.index)));
  });
  openSuggestions();
}

function openSuggestions() {
  els.list.hidden = false;
  els.input.setAttribute("aria-expanded", "true");
}
function closeSuggestions() {
  els.list.hidden = true;
  els.input.setAttribute("aria-expanded", "false");
  state.activeIndex = -1;
}

function setActive(idx) {
  const lis = els.list.querySelectorAll(".sugg-item");
  lis.forEach((li) => li.classList.remove("active"));
  state.activeIndex = idx;
  if (idx >= 0 && lis[idx]) {
    lis[idx].classList.add("active");
    lis[idx].scrollIntoView({ block: "nearest" });
    els.input.setAttribute("aria-activedescendant", `opt-${idx}`);
  } else {
    els.input.removeAttribute("aria-activedescendant");
  }
}

function onKeyDown(e) {
  const n = state.items.length;
  if (els.list.hidden || !n) {
    if (e.key === "ArrowDown" && els.input.value.trim()) fetchSuggestions(els.input.value);
    return;
  }
  if (e.key === "ArrowDown") {
    e.preventDefault();
    setActive((state.activeIndex + 1) % n);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    setActive((state.activeIndex - 1 + n) % n);
  } else if (e.key === "Enter") {
    if (state.activeIndex >= 0) {
      e.preventDefault();
      submitSearch(state.items[state.activeIndex].query);
    }
  } else if (e.key === "Escape") {
    closeSuggestions();
  }
}

/* ---------- search submission (dummy API) ---------- */
async function submitSearch(query) {
  query = (query || els.input.value).trim();
  if (!query) return;
  els.input.value = query;
  closeSuggestions();
  try {
    const res = await fetch("/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    const data = await res.json();
    showToast(`${data.message}: “${query}”`); // dummy "Searched" response
    // Reflect the new activity quickly.
    setTimeout(loadTrending, 150);
    setTimeout(loadStats, 250);
  } catch (e) {
    showToast("Search failed", true);
  }
}

function showToast(msg, isErr) {
  clearTimeout(state.toastTimer);
  els.toast.className = "toast" + (isErr ? " err" : "");
  els.toast.innerHTML = `<span class="t-tag">${isErr ? "ERROR" : "API"}</span><span>${escapeHtml(msg)}</span>`;
  els.toast.hidden = false;
  state.toastTimer = setTimeout(() => { els.toast.hidden = true; }, 2600);
}

/* ---------- trending ---------- */
async function loadTrending() {
  try {
    const res = await fetch("/trending");
    const data = await res.json();
    const items = data.trending || [];
    if (!items.length) {
      els.trendingChips.innerHTML = `<span class="empty">Search a few queries to see trends appear.</span>`;
      return;
    }
    els.trendingChips.innerHTML = items.map((it, i) => `
      <span class="chip" data-q="${escapeHtml(it.query)}">
        <span class="rank">${i + 1}</span>
        <span>${escapeHtml(it.query)}</span>
        <span class="c-score">▲ ${it.recent_score.toFixed(1)}</span>
      </span>`).join("");
    els.trendingChips.querySelectorAll(".chip").forEach((c) => {
      c.addEventListener("click", () => submitSearch(c.dataset.q));
    });
  } catch (e) { /* dashboard is best-effort */ }
}

/* ---------- stats dashboard ---------- */
async function loadStats() {
  try {
    const res = await fetch("/stats");
    const s = await res.json();
    els.healthDot.classList.add("ok"); els.healthDot.classList.remove("bad");

    const lat = s.requests_and_latency;
    const bw = s.batch_writer;
    els.mIndexed.textContent = (s.index.queries_indexed || 0).toLocaleString();
    els.mHit.textContent = (lat.cache_hit_rate * 100).toFixed(1) + "%";
    els.mP95.textContent = lat.latency_ms.p95.toFixed(2) + " ms";
    els.mP50.textContent = lat.latency_ms.p50.toFixed(2) + " ms";
    els.mSearches.textContent = (bw.search_events_received || 0).toLocaleString();
    els.mWrites.textContent = (bw.db_rows_written || 0).toLocaleString();
    els.mReduction.textContent = bw.write_reduction_ratio ? bw.write_reduction_ratio.toFixed(2) + "×" : "—";
    els.mBuffer.textContent = (bw.current_buffer_size || 0).toLocaleString();

    renderCacheNodes(s.cache.nodes || []);
  } catch (e) {
    els.healthDot.classList.add("bad"); els.healthDot.classList.remove("ok");
  }
}

function renderCacheNodes(nodes) {
  const maxSize = Math.max(1, ...nodes.map((n) => n.size));
  els.cacheNodes.innerHTML = nodes.map((n) => {
    const pct = Math.round((n.size / maxSize) * 100);
    return `
      <div class="node-row">
        <span class="node-name">${escapeHtml(n.name)}</span>
        <span class="bar"><i style="width:${pct}%"></i></span>
        <span class="node-meta">${n.size} keys · ${(n.hit_rate * 100).toFixed(0)}% hit</span>
      </div>`;
  }).join("");
}

/* ---------- cache debug ---------- */
async function locateKey() {
  const prefix = els.debugInput.value.trim();
  if (!prefix) return;
  try {
    const res = await fetch(`/cache/debug?prefix=${encodeURIComponent(prefix)}&recency=${state.recency}`);
    const d = await res.json();
    const badge = `<span class="badge ${d.status}">${d.status}</span>`;
    els.debugOut.innerHTML = `
      <div class="kv"><span>prefix key</span><b>${escapeHtml(d.mode === "recency" ? "r1:" : "r0:")}${escapeHtml(d.normalized_prefix)}</b></div>
      <div class="kv"><span>owner cache node</span><b>${escapeHtml(d.owner_node || "—")}</b></div>
      <div class="kv"><span>cache status</span>${badge}</div>
      <div class="kv"><span>key ring position</span><b>${d.ring_position_pct}%</b></div>
      <div class="kv"><span>owning vnode position</span><b>${d.owning_vnode_position_pct}%</b></div>
      <div class="kv"><span>ring</span><b>${d.nodes.length} nodes × ${d.vnodes_per_node} vnodes</b></div>`;
  } catch (e) {
    els.debugOut.innerHTML = `<span class="empty">Lookup failed.</span>`;
  }
}

/* ---------- wiring ---------- */
function setMode(recency) {
  state.recency = recency;
  els.segBtns.forEach((b) => b.classList.toggle("active", (b.dataset.recency === "true") === recency));
  if (els.input.value.trim()) fetchSuggestions(els.input.value);
}

function init() {
  els.input.addEventListener("input", onInput);
  els.input.addEventListener("keydown", onKeyDown);
  els.input.addEventListener("focus", () => { if (state.items.length && els.input.value.trim()) openSuggestions(); });
  document.addEventListener("click", (e) => { if (!els.box.contains(e.target)) closeSuggestions(); });

  els.form.addEventListener("submit", (e) => { e.preventDefault(); submitSearch(); });
  els.segBtns.forEach((b) => b.addEventListener("click", () => setMode(b.dataset.recency === "true")));

  els.dashToggle.addEventListener("click", () => {
    const hidden = els.dashboard.classList.toggle("hidden");
    els.dashToggle.setAttribute("aria-expanded", String(!hidden));
  });
  els.debugBtn.addEventListener("click", locateKey);
  els.debugInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); locateKey(); } });

  loadTrending();
  loadStats();
  setInterval(loadStats, 2000);
  setInterval(loadTrending, 4000);

  // Deep-link: /?q=iphone prefills the box and opens suggestions.
  const preset = new URLSearchParams(location.search).get("q");
  if (preset) {
    els.input.value = preset;
    fetchSuggestions(preset);
  }
  els.input.focus();
}

document.addEventListener("DOMContentLoaded", init);
