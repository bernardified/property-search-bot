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

const el = (id) => document.getElementById(id);
const form = el("search-form");
const input = el("search-input");
const searchBtn = form.querySelector("button");
const statusBox = el("status");
const resultsBox = el("results");

let markerLayer = L.layerGroup().addTo(map);
let propertyMarker = null;
let amenityAbort = null;   // cancels stale amenity fetches when a new search starts
let trendAbort = null;
let bandsChart = null;     // Chart.js instances — destroyed on each new search
let trendChart = null;

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
  propertyMarker = null;
  el("legend").hidden = true;
  el("map-loading").hidden = true;
}

// ── Drawer toggle ────────────────────────────────────────────────────────────

const appBox = el("app");
const drawerBtn = el("drawer-toggle");
const isMobile = () => window.matchMedia("(max-width: 760px)").matches;

function setDrawerGlyph() {
  const closed = appBox.classList.contains("drawer-closed");
  drawerBtn.textContent = isMobile() ? (closed ? "▼" : "▲") : (closed ? "▶" : "◀");
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

// ── Recent searches ──────────────────────────────────────────────────────────

async function loadRecent() {
  try {
    const r = await fetch("/api/list");
    const data = await r.json();
    const box = el("recent");
    box.innerHTML = "";
    (data.searches || []).forEach((s) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = s.name;
      chip.onclick = () => runSearch(s.name);
      box.appendChild(chip);
    });
  } catch {
    /* recent list is decorative — ignore failures */
  }
}

// ── Search ───────────────────────────────────────────────────────────────────

form.addEventListener("submit", (e) => {
  e.preventDefault();
  runSearch(input.value.trim());
});

async function runSearch(q) {
  if (!q) return;
  if (exploreOn) exitExplore();
  input.value = q;
  searchBtn.disabled = true;
  setStatus(/^\d{6}$/.test(q) ? "Looking up postal code…" : "Searching…");
  resultsBox.hidden = true;
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
  html += "<details><summary>All transaction details</summary>";
  html += "<table><tr><th>Band</th><th class='num'>Price</th><th class='num'>PSF</th><th>Date</th></tr>";
  for (const [band, txn] of Object.entries(d.bands || {})) {
    html +=
      `<tr><td>${esc(band)}<br><span class="popup-line">${esc(txn.floor_range)} flr · ${txn.area_sqft} sqft · ${esc(txn.type_of_sale)}</span></td>` +
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
}

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

  let headline = `Avg resale PSF, ${esc(t.span_label || "over time")} · ${t.total_txns} txns`;
  if (t.pct_change != null) {
    const cls = t.pct_change >= 0 ? "delta-up" : "delta-down";
    const arrow = t.pct_change >= 0 ? "▲" : "▼";
    headline = `<span class="${cls}">${arrow} ${t.pct_change > 0 ? "+" : ""}${t.pct_change}%</span> ${headline}`;
  }
  area.innerHTML =
    `<p class="trend-headline">${headline}</p>` +
    `<div class="chart-box"><canvas id="trend-chart" height="180"></canvas></div>`;

  trendChart = new Chart(el("trend-chart"), {
    type: "line",
    data: {
      labels: periods.map((p) => p.label),
      datasets: [{
        data: periods.map((p) => p.avg_psf),
        borderColor: BRAND,
        backgroundColor: BRAND,
        borderWidth: 2,
        pointRadius: 3,
        pointHoverRadius: 6,
        tension: 0.15,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false }, // crosshair-style hover
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
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
      if (propertyMarker) propertyMarker.setLatLng([a.lat, a.lng]);
      else placePropertyPin({ ...d, lat: a.lat, lng: a.lng });
    }

    const bounds = [];
    if (propertyMarker) bounds.push(propertyMarker.getLatLng());

    for (const [key, style] of Object.entries(AMENITY_STYLES)) {
      for (const item of a[key] || []) {
        if (item.dest_lat == null || item.dest_lng == null) continue;
        const lines = [`<div class="popup-name">${esc(item.name)}</div>`];
        if (item.distance) lines.push(`<div class="popup-line">🚶 ${esc(item.distance)} · ${esc(item.duration)}</div>`);
        if (item.transit_duration)
          lines.push(`<div class="popup-line">🚌 ${esc(item.transit_duration)} · ${esc(item.transit_distance)}</div>`);
        if (item.maps_link)
          lines.push(`<div class="popup-line"><a href="${esc(item.maps_link)}" target="_blank">Directions ↗</a></div>`);
        L.circleMarker([item.dest_lat, item.dest_lng], {
          radius: 7,
          color: "#fff",
          weight: 1.5,
          fillColor: style.color,
          fillOpacity: 0.95,
        })
          .addTo(markerLayer)
          .bindPopup(lines.join(""));
        bounds.push([item.dest_lat, item.dest_lng]);
      }
    }

    if (bounds.length > 1) map.fitBounds(bounds, { padding: [40, 40] });
    el("legend").hidden = false;
  } catch (err) {
    if (err.name === "AbortError") return;
    el("map-loading").hidden = true;
    setStatus("Amenities failed to load: " + err.message, true);
  }
}

// ── Explore mode: every development, clustered ──────────────────────────────

const exploreBtn = el("explore-btn");
let exploreLayer = null;  // built once from /api/developments, then reused
let exploreCount = 0;
let exploreOn = false;

exploreBtn.addEventListener("click", () => (exploreOn ? exitExplore(true) : enterExplore()));

async function enterExplore() {
  exploreBtn.disabled = true;
  setStatus("Loading all developments…");
  try {
    if (!exploreLayer) {
      const r = await fetch("/api/developments");
      const d = await r.json();
      const devs = d.developments || [];
      exploreCount = devs.length;
      exploreLayer = L.markerClusterGroup({
        maxClusterRadius: 60,
        showCoverageOnHover: false,
        spiderfyOnMaxZoom: true,
      });
      for (const dev of devs) {
        const psfLine = dev.avg_psf
          ? `12-mo avg: ${fmtMoney(dev.avg_psf)} psf · ${dev.txns_12mo} txns`
          : "No transactions in the last 12 months";
        exploreLayer.addLayer(
          L.circleMarker([dev.lat, dev.lng], {
            radius: 6,
            color: "#fff",
            weight: 1.5,
            fillColor: BRAND,
            fillOpacity: 0.9,
          }).bindPopup(
            `<div class="popup-name">${esc(dev.project)}</div>` +
            `<div class="popup-line">${esc(dev.street)} (D${esc(dev.district)})</div>` +
            `<div class="popup-line">${psfLine}</div>` +
            `<div class="popup-line"><a href="#" class="popup-view" data-name="${esc(dev.project)}">View details →</a></div>`
          )
        );
      }
    }
    // Clear any single-property state, then show the explore layer.
    destroyCharts();
    resetMap();
    resultsBox.hidden = true;
    map.addLayer(exploreLayer);
    map.setView(SG_CENTER, 12);
    exploreOn = true;
    exploreBtn.textContent = "✕ Exit explore";
    setStatus(`${exploreCount} developments plotted — click a dot (or cluster) for details.`);
  } catch (err) {
    setStatus("Explore failed to load: " + err.message, true);
  } finally {
    exploreBtn.disabled = false;
  }
}

function exitExplore(clearStatus = false) {
  if (exploreLayer) map.removeLayer(exploreLayer);
  exploreOn = false;
  exploreBtn.textContent = "🗺 Explore all developments";
  if (clearStatus) setStatus("");
}

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

loadRecent();
