/* MF Analytics web UI: plain JS, no build step. Talks to the same-origin JSON API. */
"use strict";

// ------------------------------------------------------------------ utilities
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const view = $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const fmt = (v, d = 2) => (isNum(v) ? v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }) : "—");
const pct = (v, d = 2) => (isNum(v) ? `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${v > 0 ? "+" : ""}${fmt(v, d)}%</span>` : `<span class="muted">—</span>`);
const plainPct = (v, d = 2) => (isNum(v) ? `${fmt(v, d)}%` : "—");
const inr = (v) => (isNum(v) ? (v < 0 ? "-₹" : "₹") + Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 }) : "—");
const dateFmt = (iso) => (iso ? new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "—");
const COLORS = Array.from({ length: 10 }, (_, i) => `var(--c${i + 1})`);
let seq = 0; // render token: async results from an abandoned view are dropped

class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

let inflight = 0, busyT;
function busy(delta) {
  inflight = Math.max(0, inflight + delta);
  clearTimeout(busyT);
  const el = $("#busy");
  if (inflight) busyT = setTimeout(() => el.classList.add("on"), 250);   // no flash for fast calls
  else el.classList.toggle("on", false);
}

async function api(path, params = {}) {
  busy(1);
  try { return await apiCall(path, params); } finally { busy(-1); }
}

async function apiCall(path, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") qs.set(k, v);
  const url = path + (qs.toString() ? `?${qs}` : "");
  let res;
  try {
    res = await fetch(url, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError("Network error: the API could not be reached.", 0);
  }
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON error page */ }
  if (!res.ok) {
    let msg = body && body.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    if (res.status === 429) msg = `Too many requests. Try again in ${res.headers.get("Retry-After") || "a few"} seconds.`;
    throw new ApiError(msg || `Request failed (${res.status})`, res.status);
  }
  return body;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 2600);
}

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

// Compare basket (per browser), max 10 schemes.
const basket = {
  items() { return store.get("mf.compare", []); },
  has(code) { return this.items().some((i) => i.code === code); },
  toggle(code, name) {
    let items = this.items();
    if (items.some((i) => i.code === code)) items = items.filter((i) => i.code !== code);
    else if (items.length >= 10) { toast("You can compare up to 10 schemes."); return false; }
    else items.push({ code, name });
    store.set("mf.compare", items); this.badge(); return true;
  },
  set(items) { store.set("mf.compare", items.slice(0, 10)); this.badge(); },
  badge() {
    const n = this.items().length, el = $("#cmp-count");
    el.hidden = n === 0; el.textContent = n;
  },
};

function hashFor(path, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") qs.set(k, v);
  return "#" + path + (qs.toString() ? `?${qs}` : "");
}

function errorCard(err, retry = true) {
  return `<div class="card empty"><p class="err">${esc(err.message || err)}</p>${retry ? `<button onclick="router()">Try again</button>` : ""}</div>`;
}
const skeleton = (n = 4) => `<div class="card">${Array.from({ length: n }, () => `<div class="skeleton" style="margin:10px 0"></div>`).join("")}</div>`;

function seg(name, options, current) {
  return `<div class="seg" role="group" aria-label="${esc(name)}">${options.map((o) =>
    `<button type="button" data-${name}="${o}" class="${o === current ? "on" : ""}" aria-pressed="${o === current}">${o.toUpperCase()}</button>`).join("")}</div>`;
}

// ------------------------------------------------------------------ SVG line chart
function niceTicks(min, max, count = 5) {
  if (min === max) { min -= 1; max += 1; }
  const span = max - min, step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

function lineChart(el, series, { yFmt = (v) => fmt(v, 2), height = 260, zero = false, tipFmt } = {}) {
  series = series.filter((s) => s.points && s.points.length);
  if (!series.length) { el.innerHTML = `<div class="empty">No data for this range.</div>`; el._drawn = false; return; }
  const draw = () => {
    const W = Math.max(280, el.clientWidth || 600), H = height;
    const m = { l: 56, r: 12, t: 10, b: 26 };
    const data = series.map((s) => ({ ...s, xs: s.points.map((p) => Date.parse(p[0])), ys: s.points.map((p) => p[1]) }));
    const allX = data.flatMap((d) => d.xs), allY = data.flatMap((d) => d.ys.filter(isNum));
    const x0 = Math.min(...allX), x1 = Math.max(...allX);
    let y0 = Math.min(...allY), y1 = Math.max(...allY);
    if (zero) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }
    const pad = (y1 - y0) * 0.06 || 1; y0 -= pad; y1 += pad;
    const X = (x) => m.l + ((x - x0) / (x1 - x0 || 1)) * (W - m.l - m.r);
    const Y = (y) => m.t + (1 - (y - y0) / (y1 - y0)) * (H - m.t - m.b);
    const yt = niceTicks(y0, y1, 5).filter((v) => v >= y0 && v <= y1);
    const spanDays = (x1 - x0) / 864e5;
    // x ticks on calendar boundaries: 1 Jan of every k-th year for long spans, else the 1st of every k-th month
    const maxTicks = Math.max(2, Math.floor((W - m.l - m.r) / 70));
    const yearly = spanDays > 730;
    const xt = [];
    {
      const a = new Date(x0), months = (x1 - x0) / (864e5 * 30.44);
      const units = yearly ? months / 12 : months;
      const k = [1, 2, 3, 6, 12, 24, 60].find((s) => units / s <= maxTicks) || Math.ceil(units / maxTicks);
      let d = yearly ? new Date(a.getFullYear() + 1, 0, 1) : new Date(a.getFullYear(), a.getMonth() + 1, 1);
      if (yearly) while (d.getFullYear() % k) d = new Date(d.getFullYear() + 1, 0, 1);
      else while (d.getMonth() % k) d = new Date(d.getFullYear(), d.getMonth() + 1, 1);
      for (; d.getTime() <= x1; d = yearly ? new Date(d.getFullYear() + k, 0, 1) : new Date(d.getFullYear(), d.getMonth() + k, 1)) xt.push(d.getTime());
    }
    const nx = xt.length;
    const xl = (x) => new Date(x).toLocaleDateString("en-IN", yearly ? { year: "numeric" } : { month: "short", year: "2-digit" });
    const paths = data.map((d) => {
      let p = "", pen = false;
      d.xs.forEach((x, i) => { const y = d.ys[i]; if (!isNum(y)) { pen = false; return; } p += `${pen ? "L" : "M"}${X(x).toFixed(1)},${Y(y).toFixed(1)}`; pen = true; });
      const line = `<path class="line" d="${p}" fill="none" stroke="${d.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
      if (series.length > 1 || zero || !p) return line;
      const gid = `g${Math.random().toString(36).slice(2, 8)}`;   // gradient area under a single price series
      const area = `${p}L${X(d.xs[d.xs.length - 1]).toFixed(1)},${H - m.b}L${X(d.xs[0]).toFixed(1)},${H - m.b}Z`;
      return `<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${d.color}" stop-opacity=".22"/><stop offset="1" stop-color="${d.color}" stop-opacity="0"/></linearGradient></defs>
        <path class="area" d="${area}" fill="url(#${gid})"/>${line}`;
    }).join("");
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(series.map((s) => s.name).join(", "))} chart">
      ${yt.map((v) => `<line class="grid-line" x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}"/><text class="axis" x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end">${esc(yFmt(v))}</text>`).join("")}
      ${zero && y0 < 0 && y1 > 0 ? `<line class="zero" x1="${m.l}" x2="${W - m.r}" y1="${Y(0)}" y2="${Y(0)}"/>` : ""}
      ${xt.map((x) => `<line class="grid-line" x1="${X(x)}" x2="${X(x)}" y1="${H - m.b}" y2="${H - m.b + 4}"/><text class="axis" x="${X(x)}" y="${H - 6}" text-anchor="${X(x) > W - 30 ? "end" : "middle"}">${xl(x)}</text>`).join("")}
      ${paths}
      <line class="cross" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" visibility="hidden"/>
      <g class="dots"></g>
      <rect x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/>
    </svg>${series.length > 1 ? `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : ""}`;
    $$("path.line", el).forEach((pl) => pl.style.setProperty("--len", Math.ceil(pl.getTotalLength())));
    el.classList.toggle("draw", !el._drawn);   // draw in on the first render only
    el._drawn = true;
    const svg = $("svg", el), cross = $(".cross", el), dots = $(".dots", el);
    let tip = $(".tip", el);
    const hide = () => { cross.setAttribute("visibility", "hidden"); dots.innerHTML = ""; if (tip) tip.remove(); tip = null; };
    const move = (ev) => {
      const r = svg.getBoundingClientRect(), sx = W / r.width;
      const px = (ev.clientX - r.left) * sx;
      const tx = x0 + ((px - m.l) / (W - m.l - m.r)) * (x1 - x0);
      const hits = data.map((d) => {
        let lo = 0, hi = d.xs.length - 1;
        while (lo < hi) { const mid = (lo + hi) >> 1; if (d.xs[mid] < tx) lo = mid + 1; else hi = mid; }
        const i = lo > 0 && Math.abs(d.xs[lo - 1] - tx) < Math.abs(d.xs[lo] - tx) ? lo - 1 : lo;
        return { d, i };
      }).filter((h) => isNum(h.d.ys[h.i]));
      if (!hits.length) return hide();
      const cx = X(hits[0].d.xs[hits[0].i]);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
      dots.innerHTML = hits.map((h) => `<circle cx="${X(h.d.xs[h.i])}" cy="${Y(h.d.ys[h.i])}" r="3.5" fill="${h.d.color}" stroke="var(--surface)" stroke-width="1.5"/>`).join("");
      if (!tip) { tip = document.createElement("div"); tip.className = "tip"; el.appendChild(tip); }
      tip.innerHTML = `<div class="d">${dateFmt(hits[0].d.points[hits[0].i][0])}</div>` + hits.map((h) =>
        `<div>${series.length > 1 ? `<i class="sw" style="background:${h.d.color}"></i>${esc(h.d.name)}: ` : ""}<b>${esc((tipFmt || yFmt)(h.d.ys[h.i]))}</b></div>`).join("");
      const w = tip.offsetWidth;
      let left = cx / sx + 12;                       // flip to the left of the crosshair near the right edge
      if (left + w > r.width) left = cx / sx - w - 12;
      tip.style.left = `${Math.max(0, left)}px`;
      tip.style.top = `${m.t / (H / r.height)}px`;
    };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerleave", hide);
  };
  draw();
  el._redraw = draw;
}
let resizeT;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => $$(".chart").forEach((c) => c._redraw && c._redraw()), 150); });

// ------------------------------------------------------------------ health footer
async function loadHealth() {
  const el = $("#health");
  try {
    const h = await api("/health");
    const stale = h.metrics && h.metrics.stale ? " · screening metrics are out of date (run the metrics job)" : "";
    el.innerHTML = `<span class="status-dot${h.metrics && h.metrics.stale ? " bad" : ""}"></span>`;
    el.append(h.schemes ? `Data through ${dateFmt(h.latest_nav_date)} · ${h.schemes.toLocaleString("en-IN")} schemes${stale}` : "No data loaded yet: run the loader.");
  } catch (e) {
    el.innerHTML = `<span class="status-dot bad"></span>`;
    el.append(e.status === 503 ? "Database unavailable" : "API unreachable");
    el.classList.add("err");
  }
}

// ------------------------------------------------------------------ screener
let categoryCache = null;
async function categoryNames() {
  if (!categoryCache) categoryCache = api("/categories", { page_size: 500 }).then((r) => r.items.map((c) => c.category)).catch(() => []);
  return categoryCache;
}
const SCREEN_COLS = [
  ["ret_1y", "1Y"], ["ret_3y", "3Y CAGR"], ["ret_5y", "5Y CAGR"], ["vol_3y", "Vol 3Y"], ["sharpe_3y", "Sharpe 3Y"], ["mdd_3y", "Max DD 3Y"],
];

async function screenerView(params, tok) {
  const p = Object.fromEntries(params);
  const page = +p.page || 1, sort = p.sort_by || "ret_3y", order = p.order || "desc";
  const cats = await categoryNames();
  if (tok !== seq) return;
  const opt = (v, label, cur) => `<option value="${esc(v)}" ${v === (cur || "") ? "selected" : ""}>${esc(label)}</option>`;
  view.innerHTML = `
    <div class="head"><div><div class="eyebrow">Screener</div><h1>Find the funds that fit your brief</h1><div class="muted">Filter and rank schemes on cached return and risk metrics.</div></div></div>
    <form class="card filters" id="filters">
      <label class="f">Search name<input name="q" type="search" placeholder="e.g. bluechip" value="${esc(p.q || "")}"></label>
      <label class="f">Category<select name="category">${opt("", "All categories", p.category)}${cats.map((c) => opt(c, c, p.category)).join("")}</select></label>
      <label class="f">Plan<select name="plan">${opt("", "Any", p.plan)}${opt("direct", "Direct", p.plan)}${opt("regular", "Regular", p.plan)}</select></label>
      <label class="f">Option<select name="option">${opt("", "Any", p.option)}${opt("growth", "Growth", p.option)}${opt("idcw", "IDCW", p.option)}${opt("bonus", "Bonus", p.option)}</select></label>
      <label class="f">Status<select name="active">${opt("", "Any", p.active)}${opt("true", "Active", p.active)}${opt("false", "Inactive", p.active)}</select></label>
      <label class="f">Min 1Y return %<input name="min_ret_1y" type="number" step="any" value="${esc(p.min_ret_1y || "")}"></label>
      <label class="f">Min 3Y CAGR %<input name="min_ret_3y" type="number" step="any" value="${esc(p.min_ret_3y || "")}"></label>
      <label class="f">Max 3Y volatility %<input name="max_vol_3y" type="number" step="any" min="0" value="${esc(p.max_vol_3y || "")}"></label>
      <label class="f">Min 3Y Sharpe<input name="min_sharpe_3y" type="number" step="any" value="${esc(p.min_sharpe_3y || "")}"></label>
      <div class="row"><button class="primary" type="submit">Apply</button><button type="button" id="reset">Reset</button></div>
    </form>
    <div class="card" id="results">${skeleton(6).replace('class="card"', "")}</div>`;
  $("#filters").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    location.hash = hashFor("/", { ...f, sort_by: sort, order });
  });
  $("#reset").addEventListener("click", () => (location.hash = "#/"));

  let res;
  try {
    res = await api("/schemes", { ...p, page, page_size: 25, sort_by: sort, order });
  } catch (e) {
    if (tok === seq) $("#results").outerHTML = errorCard(e);
    return;
  }
  if (tok !== seq) return;
  const sortTh = (key, label, cls = "num") => {
    const s = key === sort ? (order === "asc" ? "ascending" : "descending") : "none";
    return `<th class="sort ${cls}" data-sort="${key}" aria-sort="${s}" tabindex="0">${label}</th>`;
  };
  const rows = res.items.map((s) => {
    const m = s.metrics || {};
    return `<tr class="click" data-code="${s.scheme_code}">
      <td class="chk-cell"><input class="chk" type="checkbox" aria-label="Add to compare" data-cmp="${s.scheme_code}" data-name="${esc(s.name)}" ${basket.has(s.scheme_code) ? "checked" : ""}></td>
      <td class="name"><a href="#/scheme/${s.scheme_code}">${esc(s.name)}</a><div class="sub">${esc([s.fund_house, s.category].filter(Boolean).join(" · "))}${s.is_active ? "" : " · inactive"}</div></td>
      <td class="num">${pct(m.ret_1y)}</td><td class="num">${pct(m.ret_3y)}</td><td class="num">${pct(m.ret_5y)}</td>
      <td class="num">${plainPct(m.vol_3y)}</td><td class="num">${fmt(m.sharpe_3y)}</td><td class="num">${pct(m.mdd_3y)}</td></tr>`;
  }).join("");
  $("#results").innerHTML = res.total === 0
    ? `<div class="empty">No schemes match these filters.</div>`
    : `<div class="row spread" style="margin-bottom:8px"><span class="count">${res.total.toLocaleString("en-IN")} schemes</span>
         <a href="#/compare" class="muted">Tick schemes to compare them →</a></div>
       <div class="table-wrap"><table><thead><tr><th style="width:28px"></th>${sortTh("name", "Scheme", "")}${SCREEN_COLS.map(([k, l]) => sortTh(k, l)).join("")}</tr></thead>
       <tbody>${rows}</tbody></table></div>
       <div class="pager"><span class="muted">Page ${res.page} of ${Math.max(1, res.pages)}</span>
         <div class="row"><button id="prev" ${page <= 1 ? "disabled" : ""}>← Previous</button><button id="next" ${page >= res.pages ? "disabled" : ""}>Next →</button></div></div>`;
  const go = (extra) => (location.hash = hashFor("/", { ...p, sort_by: sort, order, page, ...extra }));
  $$("th.sort", view).forEach((th) => {
    const act = () => { const k = th.dataset.sort; go({ sort_by: k, order: k === sort && order === "desc" ? "asc" : "desc", page: 1 }); };
    th.addEventListener("click", act);
    th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); act(); } });
  });
  $("#prev") && $("#prev").addEventListener("click", () => go({ page: page - 1 }));
  $("#next") && $("#next").addEventListener("click", () => go({ page: page + 1 }));
  $$("tr.click", view).forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest("input,a")) return;
    location.hash = `#/scheme/${tr.dataset.code}`;
  }));
  $$("[data-cmp]", view).forEach((cb) => cb.addEventListener("change", () => {
    if (!basket.toggle(+cb.dataset.cmp, cb.dataset.name)) cb.checked = false;
  }));
}

// ------------------------------------------------------------------ scheme detail
async function schemeView(code, params, tok) {
  view.innerHTML = skeleton(8);
  const rf = params.get("rf") || "6.5";
  let s, a, dq;
  try {
    [s, a, dq] = await Promise.all([api(`/schemes/${code}`), api(`/schemes/${code}/analytics`, { rf }), api(`/schemes/${code}/data-quality`)]);
  } catch (e) {
    if (tok === seq) view.innerHTML = errorCard(e, e.status !== 404) + (e.status === 404 ? `<p><a href="#/">← Back to screener</a></p>` : "");
    return;
  }
  if (tok !== seq) return;
  const r = a.returns, r3 = a.risk["3y"];
  const kpi = (label, value) => `<div class="kpi spot"><div class="label">${label}</div><div class="value">${value}</div></div>`;
  const riskRow = (label, f) => `<tr><td>${label}</td>${["1y", "3y", "5y"].map((p) => `<td class="num">${a.risk[p].available ? f(a.risk[p]) : `<span class="muted" title="${esc(a.risk[p].reason)}">n/a</span>`}</td>`).join("")}</tr>`;
  const inBasket = basket.has(code);
  view.innerHTML = `
    <a class="back" href="#/" onclick="if (history.length > 1) { history.back(); return false; }">${ICON.back} Back</a>
    <div class="head">
      <div style="min-width:0"><h1>${esc(s.name)}</h1>
        <div class="badges">
          ${s.fund_house ? `<span class="badge">${esc(s.fund_house)}</span>` : ""}
          ${s.category ? `<a class="badge accent" href="${hashFor("/category", { name: s.category })}">${esc(s.category)}</a>` : ""}
          <span class="badge">${esc(s.plan)}</span>${s.option ? `<span class="badge">${esc(s.option.toUpperCase())}</span>` : ""}
          ${s.isin ? `<span class="badge">ISIN ${esc(s.isin)}</span>` : ""}<span class="badge">Code ${s.scheme_code}</span>
        </div></div>
      <button id="cmp-btn" class="${inBasket ? "" : "primary"}">${inBasket ? "✓ In compare" : "+ Add to compare"}</button>
    </div>
    ${s.is_active ? "" : `<div class="notice" role="note">${ICON.alert}<span>This scheme has no recent NAV. It may be closed or merged; metrics reflect its last available data.</span></div>`}
    ${s.option === "idcw" ? `<div class="notice" role="note">${ICON.alert}<span>IDCW plan: returns are NAV price returns and exclude payouts. Use the Growth option to compare performance.</span></div>` : ""}
    <div class="kpis">
      ${kpi("NAV · " + dateFmt(a.as_of), `<span id="last-nav">—</span>`)}
      ${kpi("1Y return", pct(r["1y"]))}${kpi("3Y CAGR", pct(r["3y"]))}${kpi("5Y CAGR", pct(r["5y"]))}
      ${kpi("Volatility 3Y", r3.available ? plainPct(r3.volatility_pct) : "—")}
      ${kpi("Sharpe 3Y", r3.available ? fmt(r3.sharpe_ratio) : "—")}
      ${kpi("Max drawdown 3Y", r3.available ? pct(r3.max_drawdown.max_drawdown_pct) : "—")}
    </div>
    <section class="card"><div class="row spread" style="margin-bottom:10px"><h2 style="margin:0">NAV history</h2>${seg("range", ["1y", "3y", "5y", "10y", "max"], "5y")}</div>
      <div class="chart" id="nav-chart"><div class="skeleton" style="height:240px"></div></div></section>
    <div class="grid grid-2">
      <section class="card"><h2>Point-to-point returns</h2><div class="table-wrap"><table><tbody>
        ${[["1w", "1 week"], ["1m", "1 month"], ["3m", "3 months"], ["6m", "6 months"], ["ytd", "Year to date"], ["1y", "1 year"], ["3y", "3 years (CAGR)"], ["5y", "5 years (CAGR)"], ["10y", "10 years (CAGR)"], ["since_inception", "Since inception"]]
          .map(([k, l]) => `<tr><td>${l}</td><td class="num">${pct(r[k])}</td></tr>`).join("")}
      </tbody></table></div></section>
      <section class="card"><div class="row spread" style="margin-bottom:12px"><h2 style="margin:0">Risk</h2>
        <form id="rf-form" class="row"><label class="f" style="flex-direction:row;align-items:center;gap:6px">Risk-free %<input name="rf" type="number" step="0.1" min="0" max="50" value="${esc(rf)}" style="width:80px"></label><button>Update</button></form></div>
        <div class="table-wrap"><table><thead><tr><th></th><th class="num">1Y</th><th class="num">3Y</th><th class="num">5Y</th></tr></thead><tbody>
          ${riskRow("Volatility (ann.)", (x) => plainPct(x.volatility_pct))}
          ${riskRow("Sharpe ratio", (x) => fmt(x.sharpe_ratio))}
          ${riskRow("Max drawdown", (x) => pct(x.max_drawdown.max_drawdown_pct))}
          ${riskRow("Drawdown trough", (x) => dateFmt(x.max_drawdown.trough_date))}
          ${riskRow("Recovered on", (x) => x.max_drawdown.trough_date ? (x.max_drawdown.recovery_date ? dateFmt(x.max_drawdown.recovery_date) : "not yet") : "—")}
        </tbody></table></div></section>
    </div>
    <section class="card"><div class="row spread" style="margin-bottom:10px"><h2 style="margin:0">Rolling returns</h2>${seg("window", ["1y", "3y", "5y"], "1y")}</div>
      <div id="roll-summary" class="kpis" style="margin-bottom:8px"></div>
      <div class="chart" id="roll-chart"><div class="skeleton" style="height:220px"></div></div></section>
    <div class="grid grid-2">
      <section class="card"><h2>SIP calculator</h2>
        <form id="sip-form" class="filters" style="grid-template-columns:repeat(auto-fill,minmax(140px,1fr))">
          <label class="f">Monthly amount (₹)<input name="amount" type="number" min="1" step="1" value="10000" required></label>
          <label class="f">Day of month<input name="day" type="number" min="1" max="28" value="1" required></label>
          <label class="f">Start date<input name="start" type="date"></label>
          <label class="f">Valuation date<input name="end" type="date"></label>
          <div><button class="primary">Calculate</button></div>
        </form><div id="sip-out" style="margin-top:14px"></div></section>
      <section class="card"><h2>Data quality</h2>${dq.available ? `<dl class="kv">
          <dt>History</dt><dd>${dateFmt(dq.first_date)} – ${dateFmt(dq.last_date)}</dd>
          <dt>NAV records</dt><dd>${dq.nav_count.toLocaleString("en-IN")}</dd>
          <dt>Coverage (weekday est.)</dt><dd>${plainPct(dq.coverage_pct, 1)}</dd>
          <dt>Largest gap</dt><dd>${dq.largest_gap_days} days</dd>
          <dt>Gaps over ${dq.gaps_over_threshold.threshold_days} days</dt><dd>${dq.gaps_over_threshold.count}</dd>
          <dt>Rows rejected at ingest</dt><dd>${dq.rejected_rows_at_ingest}</dd></dl>
          ${dq.gaps_over_threshold.worst.length ? `<p class="muted" style="margin-bottom:0">Worst gaps: ${dq.gaps_over_threshold.worst.map((g) => `${dateFmt(g.from)} → ${dateFmt(g.to)} (${g.days}d)`).join("; ")}</p>` : ""}`
        : `<p class="muted">${esc(dq.reason)}</p>`}</section>
    </div>`;

  $("#cmp-btn").addEventListener("click", (e) => {
    if (basket.toggle(code, s.name)) {
      const on = basket.has(code);
      e.target.textContent = on ? "✓ In compare" : "+ Add to compare";
      e.target.classList.toggle("primary", !on);
      toast(on ? "Added to compare" : "Removed from compare");
    }
  });
  $("#rf-form").addEventListener("submit", (e) => {
    e.preventDefault();
    location.hash = hashFor(`/scheme/${code}`, { rf: new FormData(e.target).get("rf") });
  });

  let range = "5y", windowSel = "1y";
  const drawCharts = async () => {
    let c;
    try { c = await api(`/schemes/${code}/chart`, { range, window: windowSel }); } catch (e) {
      if (tok === seq) $("#nav-chart").innerHTML = `<p class="err">${esc(e.message)}</p>`;
      return;
    }
    if (tok !== seq) return;
    if (c.nav.length) $("#last-nav").textContent = "₹" + fmt(c.nav[c.nav.length - 1][1], 4);
    lineChart($("#nav-chart"), [{ name: "NAV", color: COLORS[0], points: c.nav }], { yFmt: (v) => fmt(v, v < 100 ? 2 : 0), tipFmt: (v) => "₹" + fmt(v, 4) });
    lineChart($("#roll-chart"), [{ name: `${windowSel.toUpperCase()} rolling return`, color: COLORS[1], points: c.rolling }], { yFmt: (v) => fmt(v, 0) + "%", tipFmt: (v) => fmt(v, 2) + "%", zero: true, height: 220 });
    const sm = a.rolling_returns_summary[windowSel] || { count: 0 };
    $("#roll-summary").innerHTML = sm.count
      ? [["Median", pct(sm.median)], ["Min", pct(sm.min)], ["Max", pct(sm.max)], ["% periods positive", plainPct(sm.pct_positive, 1)], ["Observations", sm.count.toLocaleString("en-IN")]]
          .map(([l, v]) => kpi(l, v)).join("")
      : `<p class="muted">Not enough history for ${windowSel} rolling returns.</p>`;
  };
  $$("[data-range]", view).forEach((b) => b.addEventListener("click", () => {
    range = b.dataset.range; $$("[data-range]", view).forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); }); drawCharts();
  }));
  $$("[data-window]", view).forEach((b) => b.addEventListener("click", () => {
    windowSel = b.dataset.window; $$("[data-window]", view).forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); }); drawCharts();
  }));
  drawCharts();

  $("#sip-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = $("#sip-out"); out.innerHTML = `<div class="skeleton"></div>`;
    try {
      const x = await api(`/schemes/${code}/sip`, Object.fromEntries(new FormData(e.target)));
      if (!x.available) { out.innerHTML = `<p class="muted">${esc(x.reason)}</p>`; return; }
      out.innerHTML = `<div class="kpis" style="margin-bottom:8px">${kpi("Invested", inr(x.total_invested))}${kpi("Value", inr(x.current_value))}${kpi("Gain", `<span class="${x.gain >= 0 ? "pos" : "neg"}">${inr(x.gain)}</span>`)}${kpi("XIRR", pct(x.xirr_pct))}</div>
        <p class="muted" style="margin:0">${x.instalments} instalments from ${dateFmt(x.first_instalment)} valued on ${dateFmt(x.valuation_date)} at ₹${fmt(x.valuation_nav, 4)}${x.skipped_instalments ? `; ${x.skipped_instalments} skipped (no NAV)` : ""}. ${esc(x.note)}</p>`;
    } catch (err) { out.innerHTML = `<p class="err">${esc(err.message)}</p>`; }
  });
  $("#sip-form").requestSubmit();
}

// ------------------------------------------------------------------ compare
async function compareView(params, tok) {
  if (params.get("codes")) {   // shared link: adopt its schemes into the basket
    const codes = params.get("codes").split(",").map(Number).filter(Boolean);
    const known = new Map(basket.items().map((i) => [i.code, i.name]));
    basket.set(codes.map((c) => ({ code: c, name: known.get(c) || `Scheme ${c}` })));
  }
  const items = basket.items();
  const range = params.get("range") || "3y";
  view.innerHTML = `
    <div class="head"><div><div class="eyebrow">Compare</div><h1>Compare schemes side by side</h1><div class="muted">Up to 10 schemes, evaluated on a common as-of date.</div></div>
      ${items.length >= 2 ? `<button id="share">Copy link</button>` : ""}</div>
    <div class="card"><div class="chips" id="chips">${items.map((i) => `<span class="chip"><span title="${esc(i.name)}">${esc(i.name)}</span><button aria-label="Remove ${esc(i.name)}" data-rm="${i.code}">×</button></span>`).join("") || `<span class="muted">No schemes selected yet.</span>`}</div>
      <form id="add-form" class="row" style="margin-top:12px"><input id="add-q" list="add-list" placeholder="Search a scheme by name to add…" autocomplete="off" style="flex:1;min-width:200px"><datalist id="add-list"></datalist><button class="primary">Add</button></form></div>
    <div id="cmp-body">${items.length < 2 ? `<div class="card empty">Add at least two schemes, here or by ticking them in the <a href="#/">screener</a>.</div>` : skeleton(6)}</div>`;
  $$("[data-rm]", view).forEach((b) => b.addEventListener("click", () => {
    basket.set(basket.items().filter((i) => i.code !== +b.dataset.rm)); location.hash = hashFor("/compare", { range }); router();
  }));
  $("#share") && $("#share").addEventListener("click", async () => {
    const url = location.origin + location.pathname + hashFor("/compare", { codes: items.map((i) => i.code).join(","), range });
    try { await navigator.clipboard.writeText(url); toast("Link copied"); } catch { prompt("Copy this link:", url); }
  });
  let found = [];
  let qT;
  $("#add-q").addEventListener("input", (e) => {
    clearTimeout(qT);
    const q = e.target.value.trim();
    if (q.length < 2) return;
    qT = setTimeout(async () => {
      try {
        found = (await api("/schemes", { q, page_size: 15, sort_by: "name" })).items;
        $("#add-list").innerHTML = found.map((s) => `<option value="${esc(s.name)}">${s.scheme_code}</option>`).join("");
      } catch { /* ignore while typing */ }
    }, 250);
  });
  $("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = $("#add-q").value.trim();
    if (!q) return;
    let hit = found.find((s) => s.name === q) || (/^\d+$/.test(q) ? { scheme_code: +q, name: `Scheme ${q}` } : null);
    if (!hit) {
      try { hit = (await api("/schemes", { q, page_size: 1 })).items[0]; } catch { /* handled below */ }
    }
    if (!hit) return toast("No matching scheme found.");
    if (basket.has(hit.scheme_code)) return toast("Already in compare.");
    if (basket.toggle(hit.scheme_code, hit.name)) { location.hash = hashFor("/compare", { range }); router(); }
  });
  if (items.length < 2) return;

  const codes = items.map((i) => i.code);
  let cmp, charts;
  try {
    [cmp, charts] = await Promise.all([
      api("/compare", { codes: codes.join(",") }),
      Promise.all(codes.map((c) => api(`/schemes/${c}/chart`, { range, max_points: 400 }).catch(() => null))),
    ]);
  } catch (e) {
    if (tok === seq) $("#cmp-body").innerHTML = errorCard(e);
    return;
  }
  if (tok !== seq) return;
  basket.set(cmp.items.map((i) => ({ code: i.scheme_code, name: i.name })));   // refresh placeholder names
  $$("#chips .chip span").forEach((sp, i) => { if (cmp.items[i]) { sp.textContent = cmp.items[i].name; sp.title = cmp.items[i].name; } });

  // Growth of ₹100 from the latest common start date.
  const valid = charts.map((c, i) => (c && c.nav.length ? { c, i } : null)).filter(Boolean);
  const start = valid.length ? valid.map(({ c }) => c.nav[0][0]).sort().pop() : null;
  const series = valid.map(({ c, i }) => {
    const pts = c.nav.filter((p) => p[0] >= start);
    const base = pts.length ? pts[0][1] : null;
    return { name: cmp.items[i].name, color: COLORS[i % 10], points: base ? pts.map((p) => [p[0], (p[1] / base) * 100]) : [] };
  });
  const rows = [["1w", "1 week"], ["1m", "1 month"], ["3m", "3 months"], ["6m", "6 months"], ["ytd", "YTD"], ["1y", "1 year"], ["3y", "3Y CAGR"], ["5y", "5Y CAGR"], ["10y", "10Y CAGR"], ["since_inception", "Since inception"]];
  const best = (vals, hi = true) => { const n = vals.filter(isNum); return n.length > 1 ? (hi ? Math.max(...n) : Math.min(...n)) : null; };
  const cell = (v, b, f) => `<td class="num" ${isNum(v) && v === b ? 'style="font-weight:700"' : ""}>${f(v)}</td>`;
  const retRows = rows.map(([k, l]) => {
    const vals = cmp.items.map((it) => it.returns && it.returns[k]);
    const b = best(vals);
    return `<tr><td>${l}</td>${vals.map((v) => cell(v, b, pct)).join("")}</tr>`;
  }).join("");
  const riskRow = (label, get, f, hi) => {
    const vals = cmp.items.map((it) => (it.risk_3y.available ? get(it.risk_3y) : null));
    const b = best(vals, hi);
    return `<tr><td>${label}</td>${vals.map((v) => cell(v, b, f)).join("")}</tr>`;
  };
  $("#cmp-body").innerHTML = `
    ${cmp.warnings.map((w) => `<div class="notice" role="note">${ICON.alert}<span>${esc(w[0].toUpperCase() + w.slice(1))}.</span></div>`).join("")}
    <section class="card"><div class="row spread" style="margin-bottom:10px"><h2 style="margin:0">Growth of ₹100${start ? ` since ${dateFmt(start)}` : ""}</h2>${seg("range", ["1y", "3y", "5y", "10y", "max"], range)}</div>
      <div class="chart" id="cmp-chart"></div></section>
    <section class="card"><h2>Returns and risk as of ${dateFmt(cmp.as_of)}</h2><div class="table-wrap"><table>
      <thead><tr><th></th>${cmp.items.map((it, i) => `<th class="num" style="white-space:normal;min-width:140px"><i class="sw" style="background:${COLORS[i % 10]}"></i><a href="#/scheme/${it.scheme_code}">${esc(it.name)}</a></th>`).join("")}</tr></thead>
      <tbody>${retRows}
        ${riskRow("Volatility 3Y", (r) => r.volatility_pct, plainPct, false)}
        ${riskRow("Sharpe 3Y", (r) => r.sharpe_ratio, (v) => fmt(v), true)}
        ${riskRow("Max drawdown 3Y", (r) => r.max_drawdown_pct, pct, true)}
        <tr><td>Category</td>${cmp.items.map((it) => `<td class="num muted" style="white-space:normal">${esc(it.category || "—")}</td>`).join("")}</tr>
        <tr><td>Plan / option</td>${cmp.items.map((it) => `<td class="num muted">${esc(it.plan)} / ${esc(it.option || "—")}</td>`).join("")}</tr>
      </tbody></table></div><p class="muted" style="margin:10px 0 0">Bold marks the best value in each row. Risk-free rate ${cmp.risk_free_rate_pct}%.</p></section>`;
  lineChart($("#cmp-chart"), series, { yFmt: (v) => fmt(v, 0), tipFmt: (v) => "₹" + fmt(v, 2), height: 300 });
  $$("[data-range]", view).forEach((b) => b.addEventListener("click", () => (location.hash = hashFor("/compare", { range: b.dataset.range }))));
}

// ------------------------------------------------------------------ categories
async function categoriesView(params, tok) {
  view.innerHTML = `<div class="head"><div><div class="eyebrow">Categories</div><h1>How each fund category has performed</h1><div class="muted">Equal-weighted averages of each category's active schemes.</div></div>
    <input id="cat-q" type="search" placeholder="Filter categories…" style="max-width:280px"></div><div id="cat-body">${skeleton(8)}</div>`;
  let res;
  try { res = await api("/categories", { page_size: 500 }); } catch (e) {
    if (tok === seq) $("#cat-body").innerHTML = errorCard(e);
    return;
  }
  if (tok !== seq) return;
  const render = (q) => {
    const items = res.items.filter((c) => c.category.toLowerCase().includes(q.toLowerCase()));
    $("#cat-body").innerHTML = items.length ? `<div class="card"><div class="table-wrap"><table>
      <thead><tr><th>Category</th><th class="num">Schemes</th><th class="num">Active</th><th class="num">Avg 1Y</th><th class="num">Avg 3Y</th><th class="num">Avg 5Y</th><th class="num">Avg vol 3Y</th><th class="num">Avg max DD 3Y</th><th class="num">Avg Sharpe 3Y</th></tr></thead>
      <tbody>${items.map((c) => { const a = c.averages; return `<tr class="click" data-cat="${esc(c.category)}">
        <td class="name"><a href="${hashFor("/category", { name: c.category })}">${esc(c.category)}</a></td>
        <td class="num">${c.scheme_count}</td><td class="num">${c.active_count}</td>
        <td class="num">${pct(a.avg_ret_1y)}</td><td class="num">${pct(a.avg_ret_3y)}</td><td class="num">${pct(a.avg_ret_5y)}</td>
        <td class="num">${plainPct(a.avg_vol_3y)}</td><td class="num">${pct(a.avg_mdd_3y)}</td><td class="num">${fmt(a.avg_sharpe_3y)}</td></tr>`; }).join("")}</tbody></table></div></div>`
      : `<div class="card empty">No categories${q ? " match this filter" : " loaded yet"}.</div>`;
    $$("tr.click", view).forEach((tr) => tr.addEventListener("click", (e) => {
      if (!e.target.closest("a")) location.hash = hashFor("/category", { name: tr.dataset.cat });
    }));
  };
  render("");
  $("#cat-q").addEventListener("input", (e) => render(e.target.value));
}

async function categoryView(params, tok) {
  const name = params.get("name") || "";
  view.innerHTML = skeleton(8);
  let a;
  try { a = await api("/categories/analytics", { category: name }); } catch (e) {
    if (tok === seq) view.innerHTML = errorCard(e, e.status !== 404) + `<p><a href="#/categories">← All categories</a></p>`;
    return;
  }
  if (tok !== seq) return;
  const r = a.returns, r3 = a.risk["3y"];
  const kpi = (label, value) => `<div class="kpi spot"><div class="label">${label}</div><div class="value">${value}</div></div>`;
  const d = a.scheme_dispersion;
  view.innerHTML = `
    <a class="back" href="#/categories">${ICON.back} All categories</a>
    <div class="head"><div><h1>${esc(a.category)}</h1><div class="muted">Category index of ${a.constituents} active schemes · as of ${dateFmt(a.as_of)}</div></div>
      <a href="${hashFor("/", { category: a.category, active: "true", sort_by: "ret_3y", order: "desc" })}"><button class="primary">View schemes</button></a></div>
    <div class="kpis">${kpi("1Y", pct(r["1y"]))}${kpi("3Y CAGR", pct(r["3y"]))}${kpi("5Y CAGR", pct(r["5y"]))}
      ${kpi("Volatility 3Y", r3.available ? plainPct(r3.volatility_pct) : "—")}${kpi("Sharpe 3Y", r3.available ? fmt(r3.sharpe_ratio) : "—")}
      ${kpi("Max drawdown 3Y", r3.available ? pct(r3.max_drawdown.max_drawdown_pct) : "—")}</div>
    <section class="card"><div class="row spread" style="margin-bottom:10px"><h2 style="margin:0">Category index <span class="muted" style="font-weight:400">(100 at start of history)</span></h2>${seg("range", ["1y", "3y", "5y", "10y", "max"], "5y")}</div>
      <div class="chart" id="idx-chart"><div class="skeleton" style="height:240px"></div></div></section>
    <div class="grid grid-2">
      <section class="card"><div class="row spread" style="margin-bottom:10px"><h2 style="margin:0">Rolling returns</h2>${seg("window", ["1y", "3y", "5y"], "1y")}</div>
        <div class="chart" id="idx-roll"></div></section>
      <section class="card"><h2>Spread across schemes</h2><div class="table-wrap"><table>
        <thead><tr><th></th><th class="num">Schemes</th><th class="num">Min</th><th class="num">Median</th><th class="num">Max</th></tr></thead><tbody>
        ${[["ret_1y", "1Y return", pct], ["ret_3y", "3Y CAGR", pct], ["vol_3y", "Volatility 3Y", plainPct], ["sharpe_3y", "Sharpe 3Y", (v) => fmt(v)]].map(([k, l, f]) =>
          `<tr><td>${l}</td><td class="num">${d[k].count}</td><td class="num">${f(d[k].min)}</td><td class="num">${f(d[k].median)}</td><td class="num">${f(d[k].max)}</td></tr>`).join("")}
        </tbody></table></div></section>
    </div>
    <p class="muted">${esc(a.methodology)}</p>`;
  let range = "5y", windowSel = "1y";
  const draw = async () => {
    let c;
    try { c = await api("/categories/chart", { category: name, range, window: windowSel }); } catch (e) {
      if (tok === seq) $("#idx-chart").innerHTML = `<p class="err">${esc(e.message)}</p>`;
      return;
    }
    if (tok !== seq) return;
    lineChart($("#idx-chart"), [{ name: "Index", color: COLORS[0], points: c.nav }], { yFmt: (v) => fmt(v, 0) });
    lineChart($("#idx-roll"), [{ name: "Rolling", color: COLORS[1], points: c.rolling }], { yFmt: (v) => fmt(v, 0) + "%", tipFmt: (v) => fmt(v, 2) + "%", zero: true, height: 220 });
  };
  const bind = (attr, set) => $$(`[data-${attr}]`, view).forEach((b) => b.addEventListener("click", () => {
    set(b.dataset[attr]); $$(`[data-${attr}]`, view).forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); }); draw();
  }));
  bind("range", (v) => (range = v)); bind("window", (v) => (windowSel = v));
  draw();
}

// ------------------------------------------------------------------ design layer
// Applied to every render through one MutationObserver, so the views stay plain template code.
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");
const svgIcon = (d) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
const ICON = {
  back: svgIcon('<path d="M15 18l-6-6 6-6"/>'),
  alert: svgIcon('<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'),
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
};

// CountUp (react-bits TextAnimations/CountUp, "count" mode, easeOut): animates the first number in a KPI value
// from 0, keeping its sign, prefix/suffix, decimals and Indian digit grouping. Final text is in the DOM from
// the start for screen readers and no-JS; reduced motion shows it at once.
const easeOut = (t) => 1 - Math.pow(1 - t, 3);
function countUp(el, duration = 900) {
  if (el._counted) return;
  el._counted = true;
  if (REDUCED.matches) return;
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const m = node.nodeValue.match(/^(.*?)([+-]?)([\d,]*\d)(\.\d+)?(.*)$/s);
    if (!m) continue;
    const [, pre, sign, int, frac = "", post] = m;
    const target = parseFloat(int.replace(/,/g, "") + frac);
    if (!Number.isFinite(target) || target === 0) return;
    const decimals = frac ? frac.length - 1 : 0;
    const text = node;
    const final = text.nodeValue;
    el.setAttribute("aria-label", el.textContent);       // announce the final value, not the frames
    const t0 = performance.now();
    const frame = (now) => {
      const p = Math.min(1, (now - t0) / duration);
      if (!text.isConnected) return;
      if (p >= 1) { text.nodeValue = final; return; }
      const v = (target * easeOut(p)).toLocaleString("en-IN", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
      text.nodeValue = `${pre}${sign}${v}${post}`;
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
    return;
  }
}

// SpotlightCard (react-bits Components/SpotlightCard): pointer-following light plus border glow, as CSS vars.
document.addEventListener("pointermove", (e) => {
  const card = e.target.closest && e.target.closest(".spot");
  const prev = document.querySelector(".spot.lit");
  if (prev && prev !== card) prev.classList.remove("lit");
  if (!card || e.pointerType === "touch") return;
  const r = card.getBoundingClientRect();
  card.style.setProperty("--mx", `${e.clientX - r.left}px`);
  card.style.setProperty("--my", `${e.clientY - r.top}px`);
  card.classList.add("lit");
}, { passive: true });
document.addEventListener("pointerleave", () => { const c = document.querySelector(".spot.lit"); if (c) c.classList.remove("lit"); });

// Segmented control indicator slides to the pressed option (react-bits Micro/RubberSegment, without the rubber).
function placeSeg(seg) {
  const on = seg.querySelector("button.on");
  if (!on) return;
  seg.style.setProperty("--seg-x", `${on.offsetLeft}px`);
  seg.style.setProperty("--seg-w", `${on.offsetWidth}px`);
}
document.addEventListener("click", (e) => {
  const seg = e.target.closest && e.target.closest(".seg");
  if (seg) requestAnimationFrame(() => placeSeg(seg));
});

// Lottie (lottie-web, SVG light build): hand-authored busy indicator and empty-state animation.
const lottieReady = () => typeof window.lottie !== "undefined";
function mountLottie(el, name, loop = true) {
  if (el._anim || !lottieReady()) return;
  el._anim = window.lottie.loadAnimation({
    container: el, renderer: "svg", loop, autoplay: !REDUCED.matches, path: `/static/anim/${name}.json`,
    rendererSettings: { preserveAspectRatio: "xMidYMid meet", progressiveLoad: true },
  });
  if (REDUCED.matches) el._anim.addEventListener("DOMLoaded", () => el._anim.goToAndStop(100, true));
  // stop the loop once the element leaves the page
  const gone = new MutationObserver(() => { if (!el.isConnected) { el._anim.destroy(); gone.disconnect(); } });
  gone.observe(document.body, { childList: true, subtree: true });
}

function enhance(root) {
  $$(".kpi .value", root).forEach((v) => countUp(v));
  $$(".seg", root).forEach(placeSeg);
  $$(".empty", root).forEach((box) => {
    if (box.querySelector(".lottie")) return;
    const a = document.createElement("div");
    a.className = "lottie"; a.setAttribute("aria-hidden", "true");
    box.prepend(a);
    mountLottie(a, "empty");
  });
}
new MutationObserver(() => enhance(view)).observe(view, { childList: true, subtree: true });
window.addEventListener("resize", () => $$(".seg").forEach(placeSeg));

// Theme: dark by default (fintech dashboards read best dark), light on request, remembered per browser.
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const b = $("#theme"), dark = theme === "dark";
  b.innerHTML = svgIcon(dark ? ICON.sun : ICON.moon);
  b.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  b.setAttribute("aria-pressed", String(!dark));
  $('meta[name="theme-color"]').setAttribute("content", dark ? "#020617" : "#f5f7fa");
  $$(".chart").forEach((c) => c._redraw && c._redraw());
}
$("#theme").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  try { localStorage.setItem("mf.theme", next); } catch { /* storage unavailable */ }
  applyTheme(next);
});

// Quick search in the header: type a name, arrow keys to pick, Enter to open. "/" focuses it from anywhere.
(function quickSearch() {
  const input = $("#qs-input"), list = $("#qs-list");
  let items = [], sel = -1, timer, reqId = 0;
  const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); sel = -1; };
  const paint = () => {
    list.innerHTML = items.length
      ? items.map((s, i) => `<li id="qs-${i}" role="option" aria-selected="${i === sel}" data-code="${s.scheme_code}">${esc(s.name)}<small>${esc([s.fund_house, s.category].filter(Boolean).join(" · "))}</small></li>`).join("")
      : `<li class="none" role="option" aria-disabled="true">No matching schemes</li>`;
    list.hidden = false; input.setAttribute("aria-expanded", "true");
    if (sel >= 0) input.setAttribute("aria-activedescendant", `qs-${sel}`); else input.removeAttribute("aria-activedescendant");
  };
  const open = (code) => { close(); input.value = ""; input.blur(); location.hash = `#/scheme/${code}`; };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) return close();
    timer = setTimeout(async () => {
      const id = ++reqId;
      try {
        const r = await api("/schemes", { q, page_size: 8, sort_by: "name" });
        if (id !== reqId) return;
        items = r.items; sel = items.length ? 0 : -1; paint();
      } catch { close(); }
    }, 200);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { close(); input.blur(); return; }
    if (list.hidden || !items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sel = (sel + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length; paint();
    } else if (e.key === "Enter" && sel >= 0) { e.preventDefault(); open(items[sel].scheme_code); }
  });
  list.addEventListener("pointerdown", (e) => { const li = e.target.closest("li[data-code]"); if (li) { e.preventDefault(); open(+li.dataset.code); } });
  input.addEventListener("blur", () => setTimeout(close, 120));
  document.addEventListener("keydown", (e) => {
    const typing = e.target instanceof Element && e.target.closest("input, select, textarea, [contenteditable]");
    if (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); input.focus(); }
  });
})();

function bootDesign() {
  applyTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark");
  const b = $("#busy");
  if (b && lottieReady()) mountLottie(b, "busy");
  if (document.fonts) document.fonts.ready.then(() => $$(".seg").forEach(placeSeg));   // widths change once Fira loads
}

// ------------------------------------------------------------------ router
function router() {
  const tok = ++seq;
  const raw = location.hash.slice(1) || "/";
  const [path, qs] = raw.split("?");
  const params = new URLSearchParams(qs || "");
  const m = path.match(/^\/scheme\/(\d+)$/);
  const tab = m ? "" : path === "/compare" ? "compare" : path.startsWith("/categor") ? "categories" : "screener";
  $$(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  window.scrollTo(0, 0);
  const done = (p) => p.catch((e) => { if (tok === seq) view.innerHTML = errorCard(e); });
  if (m) return done(schemeView(+m[1], params, tok));
  if (path === "/compare") return done(compareView(params, tok));
  if (path === "/categories") return done(categoriesView(params, tok));
  if (path === "/category") return done(categoryView(params, tok));
  return done(screenerView(params, tok));
}

window.addEventListener("hashchange", router);
bootDesign();
basket.badge();
loadHealth();
router();
