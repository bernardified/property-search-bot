/* Single-property map view (Phase 1).
 *
 * Flow: /api/property answers fast (URA cache + OneMap pin) → render panel +
 * property marker immediately, then /api/amenities (slow: Google Places /
 * Distance Matrix) fills the amenity pins. The street — never the project
 * name — is what gets geocoded server-side, same convention as the bot.
 */

const SG_CENTER = [1.3521, 103.8198];

const map = L.map("map").setView(SG_CENTER, 12);
L.tileLayer("https://www.onemap.gov.sg/maps/tiles/Default/{z}/{x}/{y}.png", {
  minZoom: 11,
  maxZoom: 19,
  attribution:
    '<img src="https://www.onemap.gov.sg/web-assets/images/logo/om_logo.png" style="height:16px;width:16px;vertical-align:middle;"> ' +
    '<a href="https://www.onemap.gov.sg/" target="_blank">OneMap</a> &copy; contributors &verbar; ' +
    '<a href="https://www.sla.gov.sg/" target="_blank">Singapore Land Authority</a>',
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
let amenityAbort = null; // cancels a stale amenity fetch when a new search starts

const fmtMoney = (n) => (n == null ? "–" : "S$" + Math.round(n).toLocaleString("en-SG"));
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function setStatus(text, isError = false) {
  statusBox.hidden = !text;
  statusBox.textContent = text || "";
  statusBox.classList.toggle("error", isError);
}

function resetMap() {
  if (amenityAbort) amenityAbort.abort();
  markerLayer.clearLayers();
  propertyMarker = null;
  el("legend").hidden = true;
  el("map-loading").hidden = true;
}

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
  input.value = q;
  searchBtn.disabled = true;
  setStatus("Searching…");
  resultsBox.hidden = true;
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
  if (d.total_units) metaBits.push(`${d.total_units} units`);
  if (d.expected_top) metaBits.push(`Expected TOP: ${esc(d.expected_top)}`);
  if (d.under_construction) metaBits.push("Under construction");
  if (d.overall_avg_psf)
    metaBits.push(`12-mo avg: ${fmtMoney(d.overall_avg_psf)} psf (${d.overall_psf_count} txns)`);

  let html = `<h2>${esc(d.development)}</h2><p class="street">${esc(d.street)}</p>`;
  if (metaBits.length) html += `<p class="meta">${metaBits.join("<br>")}</p>`;

  html += "<h3>Latest transactions by size</h3>";
  html +=
    "<table><tr><th>Band</th><th class='num'>Price</th><th class='num'>PSF</th><th class='num'>12-mo avg PSF</th><th>Date</th></tr>";
  for (const [band, txn] of Object.entries(d.bands || {})) {
    const avg = (d.band_avg_psf || {})[band];
    html +=
      `<tr><td>${esc(band)}<br><span class="popup-line">${esc(txn.floor_range)} flr · ${txn.area_sqft} sqft</span></td>` +
      `<td class="num">${fmtMoney(txn.price)}</td>` +
      `<td class="num">${txn.psf ? fmtMoney(txn.psf) : "–"}</td>` +
      `<td class="num">${avg ? fmtMoney(avg.avg_psf) + `<br><span class="popup-line">${avg.count} txns</span>` : "–"}</td>` +
      `<td>${esc(txn.contract_date_display)}</td></tr>`;
  }
  html += "</table>";

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
    const r = await fetch("/api/amenities?street=" + encodeURIComponent(d.street), { signal });
    const a = await r.json();
    if (signal.aborted) return;
    el("map-loading").hidden = true;

    if (a.error) {
      setStatus(a.error, true);
      return;
    }

    // Google's street geocode is the origin the distances were measured from —
    // snap the property pin to it if it drifted from the quick OneMap pin.
    if (a.lat != null && a.lng != null) {
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

loadRecent();
