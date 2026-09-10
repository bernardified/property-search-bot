/* Single-property map view.
 *
 * Flow: /api/property answers fast (URA cache + pin) → render panel +
 * property marker immediately, then /api/amenities (slow: Google Places /
 * Distance Matrix) fills the amenity pins and /api/trend fills the price-trend
 * chart. The pin is exact (`exact_coords`) whenever the server had a real
 * coordinate — URA's own x/y for the project, or the resolved postal address —
 * and it is then fed to /api/amenities so pin and distances agree. Only
 * projects with no x/y fall back to a street geocode, which gets snapped to
 * the Google origin the distances were measured from.
 */

const SG_CENTER = [1.3521, 103.8198];
const BRAND = "#2563eb";      // single-series hue for both charts
const INK_MUTED = "#5b6472";  // tick/label ink — text never wears series color
const GRID = "#eef0f3";

const map = L.map("map").setView(SG_CENTER, 12);
L.tileLayer("https://www.onemap.gov.sg/maps/tiles/Default/{z}/{x}/{y}.png", {
  minZoom: 11,
  maxZoom: 19,
  attribution:
    '<img src="https://www.onemap.gov.sg/web-assets/images/logo/om_logo.png" style="height:16px;width:16px;vertical-align:middle;"> ' +
    '<a href="https://www.onemap.gov.sg/" target="_blank">OneMap</a> &copy; contributors &verbar; ' +
    '<a href="https://www.sla.gov.sg/" target="_blank">Singapore Land Authority</a> &verbar; ' +
    'Property data &copy; <a href="https://www.ura.gov.sg/" target="_blank">URA</a> ' +
    '(<a href="https://data.gov.sg/open-data-licence" target="_blank">SODL v1.0</a>)',
}).addTo(map);

const AMENITY_STYLES = {
  mrts: { color: "#dc2626", label: "MRT" },
  schools: { color: "#2563eb", label: "School" },
  malls: { color: "#9333ea", label: "Mall" },
  supermarkets: { color: "#16a34a", label: "Supermarket" },
};

// Primary-school admission priority is drawn at 1 km straight-line, which is
// the question a school pin actually raises: is this property inside it?
const SCHOOL_RADIUS_M = 1000;

const el = (id) => document.getElementById(id);
const form = el("search-form");
const input = el("search-input");
const searchBtn = form.querySelector("button");
const statusBox = el("status");
const resultsBox = el("results");

let markerLayer = L.layerGroup().addTo(map);
let propertyMarker = null;
let schoolRing = null;     // {circle, key} — one 1 km ring at a time
let amenityAbort = null;   // cancels stale amenity fetches when a new search starts
let trendAbort = null;
let bandsChart = null;     // Chart.js instances — destroyed on each new search
let trendChart = null;
let amenitiesShown = false;  // did the amenity pins land? (legend state across views)
let currentDev = null;     // resolved project name — the band drill-down re-queries by it
let openBand = null;       // size band whose full transaction list is showing
let bandAbort = null;      // cancels a stale drill-down fetch

const fmtMoney = (n) => (n == null ? "–" : "S$" + Math.round(n).toLocaleString("en-SG"));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const shortBand = (label) =>
  label.replace(" sqft", "").replace("<= ", "≤").replace(" – ", "–").replace("> ", ">");

function setStatus(text, isError = false) {
  statusBox.hidden = !text;
  statusBox.textContent = text || "";
  statusBox.classList.toggle("error", isError);
}

function destroyCharts() {
  if (bandsChart) { bandsChart.destroy(); bandsChart = null; }
  if (trendChart) { trendChart.destroy(); trendChart = null; }
}

function resetMap() {
  if (amenityAbort) amenityAbort.abort();
  if (trendAbort) trendAbort.abort();
  markerLayer.clearLayers();
  if (!map.hasLayer(markerLayer)) map.addLayer(markerLayer);  // nearby mode detaches it
  clearNearbyLayer();
  clearSchoolRing();
  propertyMarker = null;
  amenitiesShown = false;
  el("legend").hidden = true;
  el("map-loading").hidden = true;
}

// ── Drawer toggle ────────────────────────────────────────────────────────────

const appBox = el("app");
const drawerBtn = el("drawer-toggle");
const isMobile = () => window.matchMedia("(max-width: 760px)").matches;

function setDrawerGlyph() {
  const closed = appBox.classList.contains("drawer-closed");
  // On mobile the panel is a bottom sheet: open collapses it downwards, so
  // the arrow points the way the panel is about to move — the opposite of the
  // desktop rail, which slides sideways.
  drawerBtn.textContent = isMobile() ? (closed ? "▲" : "▼") : (closed ? "▶" : "◀");
  drawerBtn.setAttribute("aria-expanded", String(!closed));
  drawerBtn.setAttribute("aria-label", closed ? "Open panel" : "Collapse panel");
}

drawerBtn.addEventListener("click", () => {
  appBox.classList.toggle("drawer-closed");
  setDrawerGlyph();
  map.invalidateSize();  // the map pane just changed size
});
window.addEventListener("resize", setDrawerGlyph);
setDrawerGlyph();

// ── Search ───────────────────────────────────────────────────────────────────

form.addEventListener("submit", (e) => {
  e.preventDefault();
  runSearch(input.value.trim());
});

async function runSearch(q) {
  if (!q) return;
  searchActive = true;
  backBtn.hidden = false;
  if (exploreOn) exitExplore();
  if (nearbyOn) exitNearby(false);   // a new search replaces the view to go back to
  currentProperty = null;
  nearbyBtn.hidden = true;
  input.value = q;
  hideSuggest();          // a picked suggestion never leaves its list open behind the result
  searchBtn.disabled = true;
  setStatus(/^\d{6}$/.test(q) ? "Looking up postal code…" : "Searching…");
  resultsBox.hidden = true;
  closeBandDetail();
  destroyCharts();
  resetMap();

  try {
    const r = await fetch("/api/property?q=" + encodeURIComponent(q));
    const data = await r.json();

    if (data.ambiguous) {
      renderCandidates(data.candidates);
      setStatus("");
      return;
    }
    if (data.error) {
      setStatus(data.error, true);
      return;
    }

    setStatus("");
    currentProperty = data;
    // No coordinate → no origin to search around; the button appears later if
    // the amenity response supplies one (street-geocode fallback).
    nearbyBtn.hidden = data.lat == null;
    renderProperty(data);
    placePropertyPin(data);
    loadAmenities(data);
    loadTrend(data);
  } catch (err) {
    setStatus("Search failed — is the API running? " + err.message, true);
  } finally {
    searchBtn.disabled = false;
  }
}

function renderCandidates(candidates) {
  resultsBox.hidden = false;
  resultsBox.innerHTML =
    "<h3>Did you mean:</h3><div class='candidates'>" +
    candidates
      .map(
        (c) =>
          `<button data-name="${esc(c.project)}"><div>${esc(c.project)}</div>` +
          `<div class="cand-street">${esc(c.street)}</div></button>`
      )
      .join("") +
    "</div>";
  resultsBox.querySelectorAll("button").forEach((b) => {
    b.onclick = () => runSearch(b.dataset.name);
  });
}

// ── Type-ahead suggestions ───────────────────────────────────────────────────
//
// Purely client-side: /api/developments has already put every dot's project +
// street in `allDots` (explore is the landing state), so matching is a scan
// over ~2.4k short strings — no endpoint, no request, no debounce. It follows
// that suggestions cover exactly the *mappable* developments: a project URA
// can search but has no coordinate never got a dot, so it never appears here.
// Typing its name in full still works, and the server's fuzzy "Did you mean"
// still catches typos on submit — the dropdown is a shortcut, not the search.

const suggestBox = el("suggest");
const SUGGEST_MAX = 8;
const SUGGEST_MIN_CHARS = 2;

let suggestions = [];    // the devs currently listed
let suggestIndex = -1;   // keyboard highlight; -1 = none, Enter submits the raw text

// Name matches rank above street matches, and prefix above mid-word, so
// "the s" leads with THE SAIL rather than a street three screens down.
function matchDevelopments(q) {
  const needle = q.trim().toUpperCase();
  if (needle.length < SUGGEST_MIN_CHARS) return [];
  const starts = [], contains = [], streets = [];
  for (const { dev } of allDots) {
    const i = dev.project.toUpperCase().indexOf(needle);
    if (i === 0) starts.push(dev);
    else if (i > 0) contains.push(dev);
    else if (String(dev.street || "").toUpperCase().includes(needle)) streets.push(dev);
    if (starts.length >= SUGGEST_MAX) break;   // nothing later can outrank a full page of prefixes
  }
  return starts.concat(contains, streets).slice(0, SUGGEST_MAX);
}

function highlight(text, needle) {
  const s = String(text ?? "");
  const i = s.toUpperCase().indexOf(needle);
  if (i < 0) return esc(s);
  return esc(s.slice(0, i)) + "<b>" + esc(s.slice(i, i + needle.length)) + "</b>" +
         esc(s.slice(i + needle.length));
}

function hideSuggest() {
  suggestions = [];
  suggestIndex = -1;
  suggestBox.hidden = true;
  suggestBox.innerHTML = "";
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
}

function renderSuggest(q) {
  const needle = q.trim().toUpperCase();
  suggestBox.innerHTML = suggestions
    .map((d, i) =>
      `<div class="sug" role="option" id="sug-${i}" aria-selected="false" data-i="${i}">` +
      `<div>${highlight(d.project, needle)}</div>` +
      `<div class="sug-street">${highlight(d.street, needle)}</div></div>`
    )
    .join("");
  suggestBox.hidden = false;
  input.setAttribute("aria-expanded", "true");
}

function moveSuggest(delta) {
  if (!suggestions.length) return;
  suggestIndex = (suggestIndex + delta + suggestions.length) % suggestions.length;
  suggestBox.querySelectorAll(".sug").forEach((row, i) => {
    const on = i === suggestIndex;
    row.classList.toggle("active", on);
    row.setAttribute("aria-selected", String(on));
    if (on) row.scrollIntoView({ block: "nearest" });
  });
  input.setAttribute("aria-activedescendant", "sug-" + suggestIndex);
}

input.addEventListener("input", () => {
  const q = input.value.trim();
  // A run of bare digits is a postal code being typed — there is nothing in
  // the dot list to suggest for it, and "12" would match half the streets.
  if (/^\d+$/.test(q)) return hideSuggest();
  suggestions = matchDevelopments(q);   // empty while the dot list is still loading
  suggestIndex = -1;
  if (!suggestions.length) return hideSuggest();
  renderSuggest(q);
});

input.addEventListener("keydown", (e) => {
  if (suggestBox.hidden) return;
  if (e.key === "ArrowDown") { e.preventDefault(); moveSuggest(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); moveSuggest(-1); }
  else if (e.key === "Escape") { hideSuggest(); }
  else if (e.key === "Enter" && suggestIndex >= 0) {
    e.preventDefault();                 // the form would otherwise submit the raw text
    runSearch(suggestions[suggestIndex].project);
  }
});

// mousedown, not click: the input's blur would tear the list down before a
// click could land on it.
suggestBox.addEventListener("mousedown", (e) => {
  const row = e.target.closest(".sug");
  if (!row) return;
  e.preventDefault();
  runSearch(suggestions[Number(row.dataset.i)].project);
});

input.addEventListener("blur", () => hideSuggest());

// ── Results panel ────────────────────────────────────────────────────────────

function renderProperty(d) {
  const metaBits = [];
  if (d.postal) metaBits.push(`Postal ${esc(d.postal)}`);
  if (d.total_units) metaBits.push(`${d.total_units} units`);
  if (d.expected_top) metaBits.push(`Expected TOP: ${esc(d.expected_top)}`);
  if (d.under_construction) metaBits.push("Under construction");
  if (d.overall_avg_psf)
    metaBits.push(`12-mo avg: ${fmtMoney(d.overall_avg_psf)} psf (${d.overall_psf_count} txns)`);

  let html = `<h2>${esc(d.development)}</h2><p class="street">${esc(d.street)}</p>`;
  if (metaBits.length) html += `<p class="meta">${metaBits.join("<br>")}</p>`;

  // PSF by size band — chart first, full transaction table behind a toggle
  html += "<h3>PSF by size band</h3>";
  html += `<div class="chart-box"><canvas id="bands-chart" height="${40 + Object.keys(d.bands || {}).length * 34}"></canvas></div>`;
  html += `<p class="hint">Tap a band for every transaction in it.</p>`;
  html += `<div id="band-detail" hidden></div>`;
  // The table below is the LATEST sale per band, not the full history — that
  // lives behind a band tap, so the summary label has to say which it is.
  html += "<details><summary>Latest sale in each band</summary>";
  html += "<table><tr><th>Band</th><th class='num'>Price</th><th class='num'>PSF</th><th>Date</th></tr>";
  for (const [band, txn] of Object.entries(d.bands || {})) {
    html +=
      `<tr><td><button type="button" class="band-cell" data-band="${esc(band)}">${esc(band)}</button>` +
      `<br><span class="popup-line">${esc(txn.floor_range)} flr · ${txn.area_sqft} sqft · ${esc(txn.type_of_sale)}</span></td>` +
      `<td class="num">${fmtMoney(txn.price)}</td>` +
      `<td class="num">${txn.psf ? fmtMoney(txn.psf) : "–"}</td>` +
      `<td>${esc(txn.contract_date_display)}</td></tr>`;
  }
  html += "</table></details>";

  // Price trend — filled in async by loadTrend()
  html += "<h3>Price trend</h3><div id='trend-area'><p class='note'>Loading trend…</p></div>";

  html += "<h3>Rental &amp; yield (last 12 months)</h3>";
  const rental = d.rental || {};
  if (rental.unavailable === "under_construction") {
    html += "<p class='note'>Still under construction — no rental contracts yet.</p>";
  } else if (rental.error) {
    html += `<p class='note'>${esc(rental.error)}</p>`;
  } else {
    const bands = rental.bands || rental; // tolerate either shape
    const rows = Object.entries(bands).filter(([, v]) => v && typeof v === "object" && "latest_rent" in v);
    if (!rows.length) {
      html += "<p class='note'>No rental records found.</p>";
    } else {
      html +=
        "<table><tr><th>Band</th><th class='num'>Latest rent</th><th class='num'>Avg rent</th><th class='num'>Yield</th></tr>";
      for (const [band, v] of rows) {
        html +=
          `<tr><td>${esc(band)}<br><span class="popup-line">${v.count} contracts</span></td>` +
          `<td class="num">${fmtMoney(v.latest_rent)}<br><span class="popup-line">${esc(v.latest_date)}</span></td>` +
          `<td class="num">${fmtMoney(v.avg_rent)}</td>` +
          `<td class="num">${v.yield_pct != null ? v.yield_pct + "%" : "–"}</td></tr>`;
      }
      html += "</table>";
    }
  }

  resultsBox.innerHTML = html;
  resultsBox.hidden = false;
  currentDev = d.development;
  closeBandDetail();  // the #band-detail slot above is a fresh, empty element
  renderBandsChart(d);
}

// ── Charts (single series, one hue; text stays in ink tokens) ───────────────

function renderBandsChart(d) {
  const entries = Object.entries(d.bands || {});
  if (!entries.length) return;

  // 12-mo average PSF where it exists, latest transaction PSF otherwise —
  // the tooltip says which one each bar is.
  const rows = entries.map(([band, txn]) => {
    const avg = (d.band_avg_psf || {})[band];
    return {
      band,
      psf: avg ? avg.avg_psf : txn.psf,
      isAvg: Boolean(avg),
      count: avg ? avg.count : null,
      txn,
    };
  }).filter((r) => r.psf != null);
  if (!rows.length) return;

  bandsChart = new Chart(el("bands-chart"), {
    type: "bar",
    data: {
      labels: rows.map((r) => shortBand(r.band)),
      datasets: [{
        data: rows.map((r) => r.psf),
        backgroundColor: BRAND,
        barThickness: 16,
        borderRadius: 4,
        borderSkipped: "start", // rounded data-end only, flat at the baseline
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false }, // single series — the section title names it
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const r = rows[ctx.dataIndex];
              const lines = [
                r.isAvg
                  ? `12-mo avg: ${fmtMoney(r.psf)} psf (${r.count} txns)`
                  : `Latest txn PSF: ${fmtMoney(r.psf)} (no 12-mo data)`,
                `Latest: ${fmtMoney(r.txn.price)} · ${r.txn.area_sqft} sqft`,
                `${r.txn.floor_range} flr · ${r.txn.contract_date_display}`,
              ];
              return lines;
            },
          },
        },
      },
      scales: {
        x: {
          beginAtZero: true, // bars encode magnitude by length
          grid: { color: GRID },
          ticks: { color: INK_MUTED, callback: (v) => "$" + v.toLocaleString("en-SG") },
        },
        y: {
          grid: { display: false },
          ticks: { color: INK_MUTED },
        },
      },
    },
  });

  // Whole-row hit area for the band drill-down. A 16px bar is a poor tap
  // target on a phone and the band name beside it is the obvious one, but
  // Chart.js fires onClick/onHover only inside the PLOT area — the axis
  // gutter the labels live in is filtered out before a handler sees it. So
  // listen on the canvas itself and map the y offset back through the
  // category scale, which makes bar, label and the space between them equal.
  const canvas = el("bands-chart");
  const bandAt = (offsetY) => {
    const y = bandsChart && bandsChart.scales.y;
    if (!y || offsetY < y.top || offsetY > y.bottom) return null;
    return rows[Math.round(y.getValueForPixel(offsetY))] || null;
  };
  canvas.addEventListener("click", (e) => {
    const row = bandAt(e.offsetY);
    if (row) toggleBandDetail(row.band);
  });
  canvas.addEventListener("mousemove", (e) => {
    canvas.style.cursor = bandAt(e.offsetY) ? "pointer" : "default";
  });
}

// ── Band drill-down: every transaction in one size band ─────────────────────
//
// The property payload carries only the latest sale per band, so the full list
// is fetched on demand. One band open at a time — same toggle convention as the
// school ring, and stacked tables in a phone-sized drawer read as a wall.

function closeBandDetail() {
  if (bandAbort) { bandAbort.abort(); bandAbort = null; }
  openBand = null;
  const box = el("band-detail");
  if (box) { box.hidden = true; box.innerHTML = ""; }
  syncBandSelection();
}

// Keep the table's band buttons showing which one is open.
function syncBandSelection() {
  for (const b of resultsBox.querySelectorAll(".band-cell")) {
    b.classList.toggle("on", b.dataset.band === openBand);
  }
}

async function toggleBandDetail(band) {
  if (openBand === band) { closeBandDetail(); return; }  // tap again to close
  const box = el("band-detail");
  if (!box || !currentDev) return;

  if (bandAbort) bandAbort.abort();
  bandAbort = new AbortController();
  const { signal } = bandAbort;

  openBand = band;
  syncBandSelection();
  box.hidden = false;
  box.innerHTML = `<p class="note">Loading ${esc(band)} transactions…</p>`;
  box.scrollIntoView({ block: "start" });

  let t;
  try {
    const r = await fetch(
      `/api/transactions?q=${encodeURIComponent(currentDev)}&band=${encodeURIComponent(band)}`,
      { signal }
    );
    t = await r.json();
  } catch (err) {
    if (err.name === "AbortError") return;
    t = { error: "Transactions failed to load: " + err.message };
  }
  if (signal.aborted || openBand !== band) return;  // a newer tap won

  renderBandDetail(box, band, t);
}

function renderBandDetail(box, band, t) {
  const head =
    `<div class="band-head"><span class="band-title">${esc(band)}</span>` +
    `<button type="button" id="band-close" aria-label="Close">✕</button></div>`;

  const txns = t.transactions || [];
  if (t.error || t.ambiguous || !txns.length) {
    box.innerHTML =
      head +
      `<p class="note">${esc(t.error || "No transactions recorded in this band.")}</p>`;
  } else {
    // Date / price / PSF are what the eye scans down; size, floor and sale type
    // ride along as a sub-line rather than as columns, which six of would clip
    // at phone width. Same shape as the latest-sale table above.
    let html =
      head +
      `<p class="band-count">${txns.length} transaction${txns.length === 1 ? "" : "s"} on record, newest first</p>` +
      `<div class="band-scroll"><table>` +
      "<tr><th>Date</th><th class='num'>Price</th><th class='num'>PSF</th></tr>";
    for (const x of txns) {
      html +=
        `<tr><td>${esc(x.contract_date_display)}` +
        `<br><span class="popup-line">${x.area_sqft.toLocaleString("en-SG")} sqft · ` +
        `${esc(x.floor_range)} flr · ${esc(x.type_of_sale)}</span></td>` +
        `<td class="num">${fmtMoney(x.price)}</td>` +
        `<td class="num">${x.psf ? fmtMoney(x.psf) : "–"}</td></tr>`;
    }
    box.innerHTML = html + "</table></div>";
  }
  el("band-close").onclick = closeBandDetail;
  box.scrollIntoView({ block: "start", behavior: "smooth" });
}

// Band buttons live in HTML rebuilt on every search, so delegate from the panel.
resultsBox.addEventListener("click", (e) => {
  const b = e.target.closest(".band-cell");
  if (b) toggleBandDetail(b.dataset.band);
});

async function loadTrend(d) {
  trendAbort = new AbortController();
  const { signal } = trendAbort;
  let t;
  try {
    const r = await fetch("/api/trend?q=" + encodeURIComponent(d.development), { signal });
    t = await r.json();
  } catch (err) {
    if (err.name === "AbortError") return;
    t = { error: "Trend failed to load: " + err.message };
  }
  const area = el("trend-area");
  if (!area) return; // panel was replaced by a newer search

  const periods = t.periods || [];
  if (t.error || t.ambiguous || periods.length < 2) {
    area.innerHTML = `<p class='note'>${esc(t.error || "Not enough resale history to chart a trend.")}</p>`;
    return;
  }

  // The rate quoted here is the FITTED one, not first-period-vs-last: those
  // two endpoint means are the thinnest points on the chart, and across the
  // cache they disagree with a fit by a median 3.4pp — on the sign of growth
  // 5% of the time. When the fit can't clear its own error bars, say so
  // instead of printing a number the data doesn't support.
  const fit = t.fit;
  const tail = `avg resale PSF, ${esc(t.span_label || "over time")} · ${t.total_txns} txns`;
  let headline;
  if (fit && fit.significant) {
    const up = fit.annual_pct >= 0;
    headline =
      `<span class="${up ? "delta-up" : "delta-down"}">${up ? "▲" : "▼"} ` +
      `${fit.annual_pct > 0 ? "+" : ""}${fit.annual_pct}%/yr</span> ` +
      `<span class="ci">(${fit.annual_low}–${fit.annual_high}%)</span> · ${tail}`;
  } else if (fit) {
    headline = `<span class="delta-flat">No clear trend</span> — prices too scattered · ${tail}`;
  } else {
    headline = `${tail} — too few sales to measure a trend`;
  }

  const showFit = Boolean(fit && fit.significant && fit.values.length === periods.length);
  area.innerHTML =
    `<p class="trend-headline">${headline}</p>` +
    `<div class="chart-box"><canvas id="trend-chart" height="${showFit ? 200 : 180}"></canvas></div>`;

  trendChart = new Chart(el("trend-chart"), {
    type: "line",
    data: {
      labels: periods.map((p) => p.label),
      datasets: [
        {
          label: "Avg PSF",
          data: periods.map((p) => p.avg_psf),
          borderColor: BRAND,
          backgroundColor: BRAND,
          borderWidth: 2,
          pointRadius: 3,
          pointHoverRadius: 6,
          tension: 0.15,
        },
        // Dashed and muted: the fit is the summary, the period line is the
        // data. It is also the only thing that stays straight when the market
        // turns — which is exactly why it never replaces the line.
        ...(showFit ? [{
          label: `Trend ${fit.annual_pct > 0 ? "+" : ""}${fit.annual_pct}%/yr`,
          data: fit.values,
          borderColor: INK_MUTED,
          backgroundColor: INK_MUTED,
          borderWidth: 1.5,
          borderDash: [6, 4],
          pointRadius: 0,
          pointHoverRadius: 0,
          tension: 0,
        }] : []),
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false }, // crosshair-style hover
      plugins: {
        // Two series now, so the dashed line has to be named. One series
        // still means no legend — the section title says what it is.
        legend: showFit
          ? {
              display: true,
              position: "bottom",
              labels: { boxWidth: 18, boxHeight: 2, color: INK_MUTED, font: { size: 11 } },
            }
          : { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              if (ctx.datasetIndex === 1) return `Fitted trend: ${fmtMoney(ctx.parsed.y)} psf`;
              const p = periods[ctx.dataIndex];
              return `${fmtMoney(p.avg_psf)} psf · ${p.count} txns`;
            },
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: INK_MUTED, maxRotation: 0, autoSkip: true } },
        y: {
          grid: { color: GRID },
          ticks: { color: INK_MUTED, callback: (v) => "$" + v.toLocaleString("en-SG") },
        },
      },
    },
  });
}

// ── Camera ───────────────────────────────────────────────────────────────────
//
// The camera settles ONCE, on the property. Amenities land seconds later, and
// re-framing to their bounds at that point is what made the map visibly jump
// under the user. Instead the property keeps the centre it was given and the
// view only ever widens — and only when a pin genuinely sits outside it.

function ensureVisible(center, points) {
  if (!points.length) return;
  const view = map.getBounds();
  if (points.every((p) => view.contains(p))) return;   // already framed — don't move

  // Mirror every stray point through the property so the fit stays centred on
  // it: the pin holds its place and only the zoom changes.
  const box = L.latLngBounds([center, center]);
  for (const p of points) {
    const ll = L.latLng(p);
    box.extend(ll);
    box.extend([2 * center.lat - ll.lat, 2 * center.lng - ll.lng]);
  }
  map.fitBounds(box, { padding: [40, 40] });
}

// ── School catchment ring ────────────────────────────────────────────────────

function clearSchoolRing() {
  if (schoolRing) {
    map.removeLayer(schoolRing.circle);
    schoolRing = null;
  }
}

// Click a school pin to ring its 1 km radius; click the same pin again to drop
// it. Only one ring at a time — overlapping rings read as a blur, not a
// catchment. `interactive: false` keeps the ring from swallowing clicks meant
// for the pins under it, and drawing it never moves the camera (see above).
function toggleSchoolRing(school) {
  const key = `${school.name}|${school.lat}|${school.lng}`;
  if (schoolRing && schoolRing.key === key) {
    clearSchoolRing();
    return;
  }
  clearSchoolRing();
  const circle = L.circle([school.lat, school.lng], {
    radius: SCHOOL_RADIUS_M,
    interactive: false,
    color: AMENITY_STYLES.schools.color,
    weight: 2,
    dashArray: "6 5",
    fillColor: AMENITY_STYLES.schools.color,
    fillOpacity: 0.07,
  }).addTo(map);
  schoolRing = { circle, key };
}

// ── Map pins ─────────────────────────────────────────────────────────────────

function placePropertyPin(d) {
  if (d.lat == null || d.lng == null) return;
  propertyMarker = L.marker([d.lat, d.lng])
    .addTo(markerLayer)
    .bindPopup(`<div class="popup-name">${esc(d.development)}</div><div class="popup-line">${esc(d.street)}</div>`)
    .openPopup();
  map.setView([d.lat, d.lng], 15);
}

async function loadAmenities(d) {
  el("map-loading").hidden = false;
  amenityAbort = new AbortController();
  const { signal } = amenityAbort;
  try {
    // Postal searches carry the exact address coordinate — pass it so
    // distances are measured from the real address (bot convention).
    let url = "/api/amenities?street=" + encodeURIComponent(d.street);
    if (d.exact_coords && d.lat != null && d.lng != null) {
      url += `&lat=${d.lat}&lng=${d.lng}`;
    }
    const r = await fetch(url, { signal });
    const a = await r.json();
    if (signal.aborted) return;
    el("map-loading").hidden = true;

    if (a.error) {
      setStatus(a.error, true);
      return;
    }

    // Street-geocode fallback only (a project with no URA x/y): Google's
    // street geocode is the origin the distances were measured from, so snap
    // the pin to it. An exact pin (URA x/y or a postal address) was already
    // sent to /api/amenities as the origin — leave it where it is.
    if (!d.exact_coords && a.lat != null && a.lng != null) {
      if (propertyMarker) {
        propertyMarker.setLatLng([a.lat, a.lng]);
        if (!nearbyOn) map.setView([a.lat, a.lng], map.getZoom());  // follow the corrected pin
      } else {
        placePropertyPin({ ...d, lat: a.lat, lng: a.lng });
        if (!nearbyOn) nearbyBtn.hidden = false;   // an origin exists after all
      }
    }

    const bounds = [];

    for (const [key, style] of Object.entries(AMENITY_STYLES)) {
      for (const item of a[key] || []) {
        if (item.dest_lat == null || item.dest_lng == null) continue;
        const lines = [`<div class="popup-name">${esc(item.name)}</div>`];
        if (item.distance) lines.push(`<div class="popup-line">🚶 ${esc(item.distance)} · ${esc(item.duration)}</div>`);
        if (item.transit_duration)
          lines.push(`<div class="popup-line">🚌 ${esc(item.transit_duration)} · ${esc(item.transit_distance)}</div>`);
        // `dist` is the straight-line metres the 1 km priority rule is measured
        // in — the walking distance above is always longer and can't answer it.
        if (key === "schools" && item.dist != null) {
          const m = Math.round(item.dist);
          const inside = m <= SCHOOL_RADIUS_M;
          lines.push(
            `<div class="popup-line">📏 ${m.toLocaleString("en-SG")} m straight-line · ` +
            `<span class="${inside ? "in-ring" : "out-ring"}">` +
            `${inside ? "inside" : "outside"} 1 km</span></div>`,
            `<div class="popup-line"><span class="muted">Click the dot to toggle its 1 km ring</span></div>`
          );
        }
        if (item.maps_link)
          lines.push(`<div class="popup-line"><a href="${esc(item.maps_link)}" target="_blank">Directions ↗</a></div>`);
        const marker = L.circleMarker([item.dest_lat, item.dest_lng], {
          radius: 7,
          color: "#fff",
          weight: 1.5,
          fillColor: style.color,
          fillOpacity: 0.95,
        })
          .addTo(markerLayer)
          .bindPopup(lines.join(""));
        if (key === "schools") {
          marker.on("click", () =>
            toggleSchoolRing({ name: item.name, lat: item.dest_lat, lng: item.dest_lng })
          );
        }
        bounds.push([item.dest_lat, item.dest_lng]);
      }
    }

    // Widen the view only for pins that fell outside it; the property keeps
    // the centre it was given when the pin dropped, so the map never jumps.
    amenitiesShown = true;
    // Amenities can land while the nearby view is up (markerLayer is detached
    // then): don't move that camera or show this legend — exitNearby restores
    // both when the user comes back.
    if (!nearbyOn) {
      if (propertyMarker) ensureVisible(propertyMarker.getLatLng(), bounds);
      else if (bounds.length > 1) map.fitBounds(bounds, { padding: [40, 40] });
      el("legend").hidden = false;
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    el("map-loading").hidden = true;
    setStatus("Amenities failed to load: " + err.message, true);
  }
}

// ── Nearby developments ─────────────────────────────────────────────────────
//
// A second view over the SAME dot payload the explore map uses: /api/nearby
// returns explore rows (avg PSF, yield, tenure, nearest MRT, last txn) plus
// distance_m, so a neighbour's popup reads exactly like its explore dot and
// its "View details →" runs the normal search.
//
// Entering the view never destroys the property view behind it: the property
// and amenity pins stay in `markerLayer` (detached from the map, not cleared)
// and the results panel — charts included — stays in the DOM, just hidden. So
// the back button is instant and costs no network call.

const NEARBY_RADIUS_M = 1000;
const NEAR_PIN = "#d97706";          // amber — clear of every amenity hue
const NEAR_PIN_NODATA = "#b9b7b0";   // same "no value is structural" grey as explore dots

const nearbyBtn = el("nearby-btn");
const nearbyView = el("nearby-view");
let nearbyLayer = null;         // origin pin + 1 km ring + neighbour pins
let nearbyMarkers = new Map();  // PROJECT → marker, so a list row can open its popup
let nearbyOn = false;
let currentProperty = null;     // last successful /api/property payload
let savedCamera = null;         // property-view centre/zoom, restored on back

// Teardrop pins (not dots) so neighbouring *developments* never read as
// amenities: same silhouette as the property marker, different fill.
function teardrop(fill) {
  return L.divIcon({
    className: "pin-marker",
    iconSize: [24, 34],
    iconAnchor: [12, 33],
    popupAnchor: [0, -30],
    html:
      '<svg width="24" height="34" viewBox="0 0 24 34" xmlns="http://www.w3.org/2000/svg">' +
      `<path d="M12 33S23 19.4 23 12A11 11 0 1 0 1 12c0 7.4 11 21 11 21z" fill="${fill}" ` +
      'stroke="#fff" stroke-width="2" stroke-linejoin="round"/>' +
      '<circle cx="12" cy="12" r="4" fill="#fff" fill-opacity=".92"/></svg>',
  });
}

function clearNearbyLayer() {
  if (nearbyLayer) {
    map.removeLayer(nearbyLayer);
    nearbyLayer = null;
  }
  nearbyMarkers.clear();
}

nearbyBtn.addEventListener("click", enterNearby);
el("nearby-back").addEventListener("click", () => exitNearby());

async function enterNearby() {
  if (!currentProperty) return;
  const d = currentProperty;
  // Centre on the pin the user is actually looking at — for the street-geocode
  // fallback that is the Google-snapped position, not the payload's guess.
  const p = propertyMarker ? propertyMarker.getLatLng() : L.latLng(d.lat, d.lng);
  if (p.lat == null) return;

  nearbyBtn.disabled = true;
  setStatus("Finding developments within 1 km…");
  try {
    const r = await fetch(
      `/api/nearby?q=${encodeURIComponent(d.development)}` +
      `&lat=${p.lat}&lng=${p.lng}&radius_m=${NEARBY_RADIUS_M}`
    );
    const data = await r.json();
    if (data.error) {
      setStatus(data.error, true);
      return;
    }

    savedCamera = { center: map.getCenter(), zoom: map.getZoom() };
    clearSchoolRing();
    map.removeLayer(markerLayer);      // property view kept intact, just detached
    el("legend").hidden = true;

    nearbyLayer = L.layerGroup().addTo(map);
    // The ring makes "within 1 km" legible instead of implied, and its bounds
    // are the right frame for the view.
    const ring = L.circle(p, {
      radius: NEARBY_RADIUS_M,
      interactive: false,
      color: BRAND,
      weight: 1.5,
      dashArray: "6 5",
      fillColor: BRAND,
      fillOpacity: 0.05,
    }).addTo(nearbyLayer);

    L.marker(p, { icon: teardrop(BRAND), zIndexOffset: 1000 })
      .addTo(nearbyLayer)
      .bindPopup(
        `<div class="popup-name">${esc(d.development)}</div>` +
        `<div class="popup-line">${esc(d.street)}</div>` +
        `<div class="popup-line"><span class="muted">Centre of the 1 km search</span></div>`
      );

    for (const dev of data.results) {
      // No 12-month transaction → no PSF and no yield to show: greyed, the same
      // way explore treats a dot with no value (never a ramp colour).
      const marker = L.marker([dev.lat, dev.lng], {
        icon: teardrop(dev.avg_psf == null ? NEAR_PIN_NODATA : NEAR_PIN),
      })
        .addTo(nearbyLayer)
        .bindPopup(popupHtml(dev));
      nearbyMarkers.set(dev.project, marker);
    }
    renderNearbyList(d, data);
    resultsBox.hidden = true;
    nearbyView.hidden = false;
    nearbyBtn.hidden = true;
    el("nearby-legend").hidden = false;
    nearbyOn = true;
    setStatus("");
    map.invalidateSize();   // the sidebar just changed height (mobile column)
    map.fitBounds(ring.getBounds(), { padding: [30, 30] });
  } catch (err) {
    setStatus("Nearby search failed: " + err.message, true);
  } finally {
    nearbyBtn.disabled = false;
  }
}

function renderNearbyList(d, data) {
  const shown = data.results.length;
  el("nearby-back-name").textContent = d.development;
  el("nearby-title").textContent = "Within 1 km";
  el("nearby-sub").textContent = !shown
    ? "No other developments within 1 km."
    : shown < data.total
      ? `${shown} nearest of ${data.total} developments within 1 km`
      : `${shown} development${shown === 1 ? "" : "s"} within 1 km`;

  el("nearby-list").innerHTML = data.results
    .map((dev) => {
      const psf = dev.avg_psf
        ? `${fmtMoney(dev.avg_psf)} psf`
        : "<span class='muted'>no recent txn</span>";
      return (
        `<button type="button" class="near-row" data-name="${esc(dev.project)}">` +
        `<span class="near-dist">${dev.distance_m.toLocaleString("en-SG")} m</span>` +
        `<span class="near-body"><span class="near-name">${esc(dev.project)}</span>` +
        `<span class="near-meta">D${esc(dev.district)} · ${psf}</span></span></button>`
      );
    })
    .join("");
}

// A row is a shortcut to its pin, not a new search — the popup it opens is the
// same one the teardrop carries, "View details →" included.
el("nearby-list").addEventListener("click", (e) => {
  const row = e.target.closest(".near-row");
  if (!row) return;
  const marker = nearbyMarkers.get(row.dataset.name);
  if (!marker) return;
  map.panTo(marker.getLatLng());
  marker.openPopup();
});

// `restore` false when another view is taking over (a new search, explore):
// the property panel and camera are about to be replaced anyway.
function exitNearby(restore = true) {
  clearNearbyLayer();
  nearbyView.hidden = true;
  el("nearby-legend").hidden = true;
  nearbyOn = false;
  if (!map.hasLayer(markerLayer)) map.addLayer(markerLayer);
  if (!restore) return;
  resultsBox.hidden = false;
  nearbyBtn.hidden = false;
  el("legend").hidden = !amenitiesShown;   // amenities may have landed while away
  map.invalidateSize();
  if (savedCamera) map.setView(savedCamera.center, savedCamera.zoom);
}

// ── Explore mode: every development, clustered, coloured + filtered ────────
//
// Colour is a SEQUENTIAL encoding: one hue per metric, light→dark, binned into
// quintiles. Steps are re-stepped off the reference blue/orange ramps for the
// OneMap tile surface (#eeece6) rather than a near-white chart surface — the
// lightest reference steps sat under the 2:1 floor against real tiles.
// Bin edges are computed ONCE from the full dataset, so filtering never
// repaints the dots that survive.

const EXPLORE_METRICS = {
  psf: {
    field: "avg_psf",
    label: "12-mo avg PSF",
    ramp: ["#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"],
    fmt: (v) => "S$" + Math.round(v).toLocaleString("en-SG"),
    empty: "no transactions in the last 12 months",
    bins: null,
  },
  yield: {
    field: "yield_pct",
    label: "Gross yield",
    ramp: ["#ee7d45", "#e35f26", "#c44e1f", "#a03f14", "#7a2f0c"],
    fmt: (v) => v.toFixed(2) + "%",
    empty: "not enough recent leases to compute a yield",
    bins: null,
  },
};
const NO_DATA = "#b9b7b0";          // dots with no value for the active metric
const CLUSTER_INK = ["#0b0b0b", "#0b0b0b", "#fff", "#fff", "#fff"];  // >=4.18:1 on every step

const backBtn = el("back-to-explore");
const panel = el("explore-panel");
let clusterLayer = null;      // rebuilt on filter change
let allDots = [];             // {dev, marker} built once from /api/developments
let metric = EXPLORE_METRICS.psf;
let exploreOn = false;
let districtSel = new Set();
// The dot list loads unprompted at boot and the search box is live throughout,
// so a search can land mid-flight. This says who owns the screen when it does.
let searchActive = false;

const dotValue = (dev) => dev[metric.field];

function computeBins(devs, m) {
  const vals = devs.map((d) => d[m.field]).filter((v) => v != null).sort((a, b) => a - b);
  if (!vals.length) return [0, 0, 0, 0];
  return [0.2, 0.4, 0.6, 0.8].map((q) => vals[Math.floor(vals.length * q)]);
}

function binOf(v, m) {
  let i = 0;
  while (i < m.bins.length && v >= m.bins[i]) i++;
  return i;
}

const colorOf = (v, m) => (v == null ? NO_DATA : m.ramp[binOf(v, m)]);

// Dots with no value are structurally different, not just another ramp step:
// smaller, grey, semi-transparent. 35% of developments have no recent
// transaction, and colouring them like a real value would invent one.
function dotStyle(dev) {
  const v = dotValue(dev);
  return v == null
    ? { radius: 4, color: "#fff", weight: 1, fillColor: NO_DATA, fillOpacity: 0.55 }
    : { radius: 6.5, color: "#fff", weight: 2, fillColor: colorOf(v, metric), fillOpacity: 0.95 };
}

function popupHtml(dev) {
  const line = (label, val) => `<div class="popup-line">${label}: ${val}</div>`;
  const psf = dev.avg_psf
    ? `${fmtMoney(dev.avg_psf)} psf · ${dev.txns_12mo} txn${dev.txns_12mo === 1 ? "" : "s"}`
    : "<span class='muted'>none in last 12 mo</span>";
  const yld = dev.yield_pct ? dev.yield_pct.toFixed(2) + "%" : "<span class='muted'>–</span>";
  return (
    `<div class="popup-name">${esc(dev.project)}</div>` +
    `<div class="popup-line">${esc(dev.street)} (D${esc(dev.district)})</div>` +
    (dev.distance_m != null
      ? line("Distance", `${dev.distance_m.toLocaleString("en-SG")} m away`) : "") +
    line("12-mo avg", psf) +
    line("Gross yield", yld) +
    line("Tenure", dev.tenure ? esc(dev.tenure) : "–") +
    line("Nearest MRT", dev.mrt_m != null ? `${dev.mrt_m.toLocaleString("en-SG")} m` : "–") +
    line("Last transaction", dev.last_txn ? esc(dev.last_txn) : "–") +
    `<div class="popup-line"><a href="#" class="popup-view" data-name="${esc(dev.project)}">View details →</a></div>`
  );
}

// Clusters carry the mean of their children — without this the colour encoding
// is invisible at the default zoom, where almost every dot is inside a cluster.
function clusterIcon(cluster) {
  const vals = cluster.getAllChildMarkers()
    .map((mk) => dotValue(mk.dev)).filter((v) => v != null);
  const mean = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  const n = cluster.getChildCount();
  const size = n < 20 ? 32 : n < 100 ? 40 : 48;
  const ink = mean == null ? "#0b0b0b" : CLUSTER_INK[binOf(mean, metric)];
  return L.divIcon({
    className: "cluster-wrap",
    iconSize: [size, size],
    html:
      `<div class="cluster" style="background:${colorOf(mean, metric)};color:${ink};` +
      `width:${size}px;height:${size}px;line-height:${size}px">${n}</div>`,
  });
}

function newClusterLayer() {
  return L.markerClusterGroup({
    maxClusterRadius: 60,
    showCoverageOnHover: false,
    spiderfyOnMaxZoom: true,
    iconCreateFunction: clusterIcon,
  });
}

// ── Filters (all client-side — the full list is already in the browser) ──────

function readFilters() {
  const num = (id) => {
    const v = parseFloat(el(id).value);
    return Number.isFinite(v) ? v : null;
  };
  return {
    activeOnly: el("f-active").checked,
    mrt: parseInt(el("f-mrt").value, 10) || null,
    tenure: el("f-tenure").value,
    psfMin: num("f-psf-min"),
    psfMax: num("f-psf-max"),
    districts: districtSel,
  };
}

function matches(dev, f) {
  if (f.activeOnly && !dev.txns_12mo) return false;
  if (f.mrt && (dev.mrt_m == null || dev.mrt_m > f.mrt)) return false;
  if (f.tenure && dev.tenure !== f.tenure) return false;
  if (f.psfMin != null && (dev.avg_psf == null || dev.avg_psf < f.psfMin)) return false;
  if (f.psfMax != null && (dev.avg_psf == null || dev.avg_psf > f.psfMax)) return false;
  if (f.districts.size && !f.districts.has(dev.district)) return false;
  return true;
}

function applyFilters() {
  if (!clusterLayer) return;
  const f = readFilters();
  const keep = allDots.filter((d) => matches(d.dev, f));
  clusterLayer.clearLayers();
  clusterLayer.addLayers(keep.map((d) => d.marker));   // bulk add: one reflow
  // A blank map is never left unexplained — without this, over-narrow filters
  // look identical to a broken layer. The message sits on the filter row
  // itself, beside the Reset button: the status box is below the fold here.
  const total = allDots.length.toLocaleString("en-SG");
  el("filter-count").textContent = keep.length
    ? `${keep.length.toLocaleString("en-SG")} of ${total} shown`
    : `No matches — widen filters, or tap`;
  el("filter-count").classList.toggle("empty", keep.length === 0);
}

function renderLegend() {
  const edges = metric.bins;
  const swatch = (c, text) => `<span class="lg"><i style="background:${c}"></i>${text}</span>`;
  const cells = metric.ramp.map((c, i) => {
    const lo = i === 0 ? null : edges[i - 1];
    const hi = i === metric.ramp.length - 1 ? null : edges[i];
    const text =
      lo == null ? `< ${metric.fmt(hi)}`
      : hi == null ? `${metric.fmt(lo)} +`
      : `${metric.fmt(lo)}–${metric.fmt(hi)}`;
    return swatch(c, text);
  });
  el("ramp-legend").innerHTML =
    `<div class="lg-title">${metric.label}</div>` +
    cells.join("") + swatch(NO_DATA, "no data");
}

function setMetric(name) {
  metric = EXPLORE_METRICS[name];
  for (const b of el("metric-toggle").querySelectorAll("button")) {
    const on = b.dataset.metric === name;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", String(on));
  }
  for (const { dev, marker } of allDots) marker.setStyle(dotStyle(dev));
  if (clusterLayer) clusterLayer.refreshClusters();   // recolour cluster icons
  renderLegend();
}

// mrt_m comes from the cached station coords; if that cache is empty (no Mongo,
// no OneMap token) every dot has mrt_m = null and each distance option would
// match nothing — blanking the map. Offer the filter only when it can work.
function syncMrtAvailability() {
  const sel = el("f-mrt");
  const usable = allDots.some((d) => d.dev.mrt_m != null);
  sel.disabled = !usable;
  if (!usable) {
    sel.value = "";
    sel.title = "Nearest-MRT data is unavailable right now";
  }
  el("mrt-label").classList.toggle("disabled", !usable);
}

function buildDistrictChips() {
  const seen = [...new Set(allDots.map((d) => d.dev.district).filter(Boolean))]
    .sort((a, b) => parseInt(a, 10) - parseInt(b, 10));
  el("f-districts").innerHTML = seen
    .map((d) => `<button type="button" class="chip" data-district="${esc(d)}">D${parseInt(d, 10)}</button>`)
    .join("");
}

// ── Enter / exit ────────────────────────────────────────────────────────────

backBtn.addEventListener("click", () => enterExplore());

// Build the dot layer once. Split out from enterExplore so the boot load and a
// later return from a search share it — the second one has nothing to fetch.
async function loadDevelopments() {
  if (allDots.length) return;
  const r = await fetch("/api/developments");
  const devs = (await r.json()).developments || [];
  for (const m of Object.values(EXPLORE_METRICS)) m.bins = computeBins(devs, m);
  allDots = devs.map((dev) => {
    const marker = L.circleMarker([dev.lat, dev.lng], dotStyle(dev)).bindPopup(popupHtml(dev));
    marker.dev = dev;          // clusters read this to average their children
    return { dev, marker };
  });
  buildDistrictChips();
  syncMrtAvailability();
  renderLegend();
}

async function enterExplore() {
  searchActive = false;
  backBtn.hidden = true;
  setStatus("Loading all developments…");
  try {
    await loadDevelopments();
    // ~2.4k developments take a moment and the search box works the whole
    // time, so a result can already be on screen by now. It wins: the dots
    // are built and waiting, but showing them here would wipe the panel and
    // yank the camera back to the middle of Singapore under the user.
    if (searchActive) {
      setStatus("");
      return;
    }
    if (!clusterLayer) clusterLayer = newClusterLayer();
    if (nearbyOn) exitNearby(false);
    closeBandDetail();
    destroyCharts();
    resetMap();
    resultsBox.hidden = true;
    // The property view is gone (resetMap + destroyCharts), so there is
    // nothing for the nearby button to search around or go back to.
    currentProperty = null;
    nearbyBtn.hidden = true;
    panel.hidden = false;
    map.addLayer(clusterLayer);
    applyFilters();
    map.setView(SG_CENTER, 12);
    exploreOn = true;
    setStatus("Click a dot (or cluster) for details.");
  } catch (err) {
    // Search still works without the dot layer, so say what broke and stop
    // short of implying the whole app is down.
    setStatus("The development map failed to load: " + err.message +
              "\nSearch by name or postal code still works.", true);
  }
}

function exitExplore(clearStatus = false) {
  if (clusterLayer) map.removeLayer(clusterLayer);
  panel.hidden = true;
  exploreOn = false;
  if (clearStatus) setStatus("");
}

// ── Control wiring ──────────────────────────────────────────────────────────

el("metric-toggle").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-metric]");
  if (b) setMetric(b.dataset.metric);
});

for (const id of ["f-active", "f-mrt", "f-tenure", "f-psf-min", "f-psf-max"]) {
  el(id).addEventListener("input", applyFilters);
}

el("f-districts").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  const d = chip.dataset.district;
  districtSel.has(d) ? districtSel.delete(d) : districtSel.add(d);
  chip.classList.toggle("on", districtSel.has(d));
  applyFilters();
});

el("reset-filters").addEventListener("click", () => {
  el("f-active").checked = false;
  el("f-mrt").value = "";
  el("f-tenure").value = "";
  el("f-psf-min").value = "";
  el("f-psf-max").value = "";
  districtSel = new Set();
  for (const c of el("f-districts").querySelectorAll(".chip")) c.classList.remove("on");
  applyFilters();
});

// "View details →" inside explore popups (popup DOM is created by Leaflet,
// so delegate from the document).
document.addEventListener("click", (e) => {
  const link = e.target.closest(".popup-view");
  if (!link) return;
  e.preventDefault();
  runSearch(link.dataset.name);
});

el("access-date").textContent = new Date().toLocaleDateString("en-SG", {
  day: "numeric", month: "short", year: "numeric",
});

// Explore is the landing state — the map opens full of developments rather
// than empty behind a button.
enterExplore();

