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

// ── Market routing ───────────────────────────────────────────────────────────
//
// One search box serves both markets — the explore map's Private/HDB toggle
// does not steer it, and typing a block while the private layer is up still
// finds the block. A 6-digit postal code is decided server-side (/api/property owns that call, against the
// authoritative HDB block dataset). Free text is decided here, against the list
// of real HDB street names from /api/hdb/streets.
//
// It has to be the real list. Query *shape* is not a reliable signal ("8 SAINT
// THOMAS" is a condo opening with a number; "BISHAN ST 22" is an HDB street
// that doesn't), and asking private first and falling back on failure does not
// work either: a fuzzy private search answers an HDB street name with condos
// sharing one word, so the fallback never fires. Asking both markets in
// parallel would be exact, but an HDB request pulls the whole resale window
// from Mongo, so it would put ~10s on every private search too.
//
// Until the list arrives (one small fetch at boot) routing falls back to the
// leading-block-token shape, which is right for the block queries most likely
// to be typed early.
const HDB_BLOCK_RE = /^(\d+[a-z]?)\s+(\S.*)$/i;

let hdbStreets = null;   // [{s: as stored, c: spelled out, b: [blocks]}] — /api/hdb/streets
let streetsReady = null; // the in-flight fetch, so a search can wait for it

function loadHdbStreets() {
  streetsReady = (async () => {
    try {
      const r = await fetch("/api/hdb/streets");
      hdbStreets = (await r.json()).streets || [];
    } catch {
      hdbStreets = [];   // routing degrades to the shape heuristic, never breaks
    }
  })();
  return streetsReady;
}

// Every query token must appear in the street, so "bishan st 22" matches
// BISHAN ST 22 but "bishan" alone does not commit a bare town name to HDB.
function looksLikeHdb(q) {
  const blockMatch = q.match(HDB_BLOCK_RE);
  if (!hdbStreets || !hdbStreets.length) return Boolean(blockMatch);

  const streetPart = (blockMatch ? blockMatch[2] : q).toUpperCase().trim();
  const tokens = streetPart.split(/\s+/).filter(Boolean);
  if (!tokens.length) return false;
  return hdbStreets.some(({ s, c }) => {
    const hay = s + " " + c;
    return tokens.every((t) => hay.includes(t));
  });
}

// `market` is set only when the caller already knows it — a picked type-ahead
// row, which was matched against one market's own list. Everything else is
// routed below.
async function runSearch(q, market) {
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
    // Wait for the routing table rather than route without it. It is normally
    // long since loaded; the exception is a search in the first seconds after
    // a cold server start, and being briefly slow there beats confidently
    // answering an HDB street with a list of condos.
    if (streetsReady) await streetsReady;

    // Postal codes are market-agnostic and decided server-side; everything
    // else is routed by the street list above, unless the caller already knew.
    const hdb = /^\d{6}$/.test(q) ? false
              : market ? market === "hdb"
              : looksLikeHdb(q);
    const url = hdb
      ? "/api/hdb?q=" + encodeURIComponent(q)
      : "/api/property?q=" + encodeURIComponent(q);
    const data = await (await fetch(url)).json();

    if (data.ambiguous) {
      if (data.market === "hdb") renderHdbCandidates(data.candidates, data.block);
      else renderCandidates(data.candidates);
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

    if (data.market === "hdb") {
      renderHdbResult(data);
      placePropertyPin(data);
      // A street is an aggregate over blocks spread along it, so it has no
      // coordinate and nothing to measure amenity distances from.
      if (data.lat != null) loadAmenities(data);
    } else {
      renderProperty(data);
      placePropertyPin(data);
      loadAmenities(data);
    }
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
// Purely client-side, over two lists the browser already holds: the private
// dot list from /api/developments (explore is the landing state) and the HDB
// street+block table from /api/hdb/streets (fetched at boot for market
// routing). So matching is a scan over short strings — no endpoint, no
// request, no debounce.
//
// It follows that private suggestions cover exactly the *mappable*
// developments: a project URA can search but has no coordinate never got a
// dot, so it never appears here. Typing its name in full still works, and the
// server's fuzzy "Did you mean" still catches typos on submit — the dropdown
// is a shortcut, not the search.
//
// An HDB address is a block on a street, so completing only the street stops
// one token short of the answer. Blocks are therefore listed once the query
// names one ("406 ang mo"); a query with no block token suggests streets
// alone, because "ANG MO KIO AVE 10" would otherwise unfold into its hundred
// blocks. Picking either passes the market to runSearch, so a picked row is
// never re-guessed by looksLikeHdb.

const suggestBox = el("suggest");
const SUGGEST_MAX = 8;
const SUGGEST_MIN_CHARS = 2;

let suggestions = [];    // the rows currently listed
let suggestIndex = -1;   // keyboard highlight; -1 = none, Enter submits the raw text

// Name matches rank above street matches, and prefix above mid-word, so
// "the s" leads with THE SAIL rather than a street three screens down.
function matchDevelopments(needle) {
  const starts = [], contains = [], streets = [];
  for (const { dev } of MARKETS.private.dots) {
    const i = dev.project.toUpperCase().indexOf(needle);
    const row = {
      market: "private", query: dev.project,
      title: dev.project, sub: dev.street, hl: needle, hlSub: needle,
    };
    if (i === 0) starts.push(row);
    else if (i > 0) contains.push(row);
    else if (String(dev.street || "").toUpperCase().includes(needle)) streets.push(row);
    if (starts.length >= SUGGEST_MAX) break;   // nothing later can outrank a full page of prefixes
  }
  return { starts, contains, streets };
}

// A street matches when every token of the query appears in it — the same
// rule looksLikeHdb routes by, so what the dropdown offers and what the box
// would have routed to can never disagree.
function streetMatches(tokens) {
  if (!hdbStreets || !tokens.length) return [];
  return hdbStreets.filter(({ s, c }) => {
    const hay = s + " " + c;
    return tokens.every((t) => hay.includes(t));
  });
}

function hdbRow(street, block) {
  const name = titleCase(street.s);
  // The expanded spelling is worth a second line only where it differs —
  // "ADMIRALTY LINK" expands to itself, and repeating it says nothing.
  const expanded = street.c !== street.s ? titleCase(street.c) : "";
  return block
    ? { market: "hdb", query: `${block} ${street.s}`, title: `${block} ${name}`,
        sub: expanded || "HDB block", hl: block }
    : { market: "hdb", query: street.s, title: name,
        sub: expanded || "HDB street", hl: "" };
}

function matchHdb(needle) {
  const blockMatch = needle.match(HDB_BLOCK_RE);
  const streetPart = (blockMatch ? blockMatch[2] : needle).trim();
  const matched = streetMatches(streetPart.split(/\s+/).filter(Boolean));

  if (!blockMatch) return { blocks: [], streets: matched.map((s) => hdbRow(s, null)) };

  // A typed block number is a prefix over that street's blocks, so "40" finds
  // 406 and 40 alike, and "406a" finds only 406A.
  const blocks = [];
  for (const street of matched) {
    for (const b of street.b) {
      if (b.startsWith(blockMatch[1])) blocks.push(hdbRow(street, b));
      if (blocks.length >= SUGGEST_MAX) return { blocks, streets: [] };
    }
  }
  // The streets themselves stay out: the query named a block, and a street
  // that has no such block is not what was asked for.
  return { blocks, streets: [] };
}

// A name match ranks above anything matched only by a street, and among the
// street-matched rows a block is more specific than the street it sits on.
function matchSuggestions(q) {
  const needle = q.trim().toUpperCase();
  if (needle.length < SUGGEST_MIN_CHARS) return [];
  const p = matchDevelopments(needle);
  const h = matchHdb(needle);
  return p.starts
    .concat(p.contains, h.blocks, h.streets, p.streets)
    .slice(0, SUGGEST_MAX);
}

function highlight(text, needle) {
  const s = String(text ?? "");
  if (!needle) return esc(s);
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

function renderSuggest() {
  suggestBox.innerHTML = suggestions
    .map((d, i) =>
      `<div class="sug" role="option" id="sug-${i}" aria-selected="false" data-i="${i}">` +
      `<div><span class="sug-badge" aria-hidden="true">${d.market === "hdb" ? "🏠" : "🏢"}</span>` +
      `${highlight(d.title, d.hl)}</div>` +
      `<div class="sug-street">${highlight(d.sub, d.hlSub || "")}</div></div>`
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

function pickSuggest(i) {
  const s = suggestions[i];
  if (s) runSearch(s.query, s.market);
}

input.addEventListener("input", () => {
  const q = input.value.trim();
  // A run of bare digits is a postal code being typed, or a block with no
  // street yet: there is no one answer to offer for either, and "12" would
  // match half the streets.
  if (/^\d+$/.test(q)) return hideSuggest();
  suggestions = matchSuggestions(q);   // empty while the dot list is still loading
  suggestIndex = -1;
  if (!suggestions.length) return hideSuggest();
  renderSuggest();
});

input.addEventListener("keydown", (e) => {
  if (suggestBox.hidden) return;
  if (e.key === "ArrowDown") { e.preventDefault(); moveSuggest(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); moveSuggest(-1); }
  else if (e.key === "Escape") { hideSuggest(); }
  else if (e.key === "Enter" && suggestIndex >= 0) {
    e.preventDefault();                 // the form would otherwise submit the raw text
    pickSuggest(suggestIndex);
  }
});

// mousedown, not click: the input's blur would tear the list down before a
// click could land on it.
suggestBox.addEventListener("mousedown", (e) => {
  const row = e.target.closest(".sug");
  if (!row) return;
  e.preventDefault();
  pickSuggest(Number(row.dataset.i));
});

input.addEventListener("blur", () => hideSuggest());

// HDB ambiguity is a list of street names, not {project, street} objects. When
// the query named a block, it is carried back so picking a street re-asks for
// that same block on it rather than dropping the user at street level.
function renderHdbCandidates(candidates, block) {
  resultsBox.hidden = false;
  resultsBox.innerHTML =
    `<h3>Did you mean:</h3><div class='candidates'>` +
    candidates
      .map((street) => {
        const q = block ? `${block} ${street}` : street;
        return `<button data-name="${esc(q)}"><div>${esc(street.toString().replace(/\b\w/g, (c) => c.toUpperCase()))}</div>` +
               `<div class="cand-street">${block ? "Block " + esc(block) : "HDB resale"}</div></button>`;
      })
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

// ── HDB results ──────────────────────────────────────────────────────────────
//
// A parallel renderer rather than a reuse of renderProperty: HDB groups by flat
// type where private groups by size band, and its signals are different ones —
// remaining lease (the thing that decides an HDB flat's value over time) and
// PSF, with no rental, yield, tenure or TOP to show. Bending one shape into the
// other's slots would misreport both. The charts and the trend area are shared,
// because those genuinely are the same thing.

function renderHdbResult(d) {
  const isBlock = d.kind === "block";
  const metaBits = [];
  if (d.postal) metaBits.push(`Postal ${esc(d.postal)}`);
  if (d.town) metaBits.push(esc(d.town.replace(/\b\w/g, (c) => c.toUpperCase())));
  metaBits.push(`${d.total_txns} resale transaction${d.total_txns === 1 ? "" : "s"}`);

  let html = `<h2>${esc(d.development)}</h2>`;
  html += `<p class="street">${esc(d.street)} · <span class="market-tag">HDB resale</span></p>`;
  html += `<p class="meta">${metaBits.join("<br>")}</p>`;

  if (!isBlock) {
    html += "<p class='note'>Street-level summary — pick a block below for its own " +
            "prices, trend and nearby amenities.</p>";
  }

  html += "<h3>PSF by flat type</h3>";
  const types = Object.entries(d.flat_types || {});
  html += `<div class="chart-box"><canvas id="flats-chart" height="${40 + types.length * 34}"></canvas></div>`;

  html += "<details open><summary>Latest sale in each flat type</summary>";
  html += "<table><tr><th>Flat type</th><th class='num'>Median</th><th class='num'>PSF</th><th class='num'>Lease left</th></tr>";
  for (const [ft, v] of types) {
    const latest = v.latest || {};
    html +=
      `<tr><td>${esc(ft)}<br><span class="popup-line">${v.count} sold` +
      (latest.month ? ` · latest ${esc(latest.month)}` : "") + `</span></td>` +
      `<td class="num">${fmtMoney(v.median_price)}` +
      (latest.price ? `<br><span class="popup-line">last ${fmtMoney(latest.price)}</span>` : "") + `</td>` +
      `<td class="num">${v.avg_psf ? fmtMoney(v.avg_psf) : "–"}</td>` +
      `<td class="num">${v.typical_lease != null ? v.typical_lease + " yrs" : "–"}</td></tr>`;
  }
  html += "</table></details>";

  // Blocks on a street double as the way into block detail — the street view's
  // whole purpose, since only a block has a coordinate and a trend of its own.
  if (!isBlock && (d.blocks || []).length) {
    html += `<h3>Blocks on this street</h3><div class="candidates">`;
    for (const b of d.blocks) {
      html +=
        `<button class="hdb-block" data-name="${esc(b.block + " " + d.street)}">` +
        `<div>Block ${esc(b.block)}</div>` +
        `<div class="cand-street">${b.count} sold recently</div></button>`;
    }
    html += "</div>";
  }

  html += `<h3>Price trend (5 yr)</h3><div id='trend-area'><p class='note'>Loading trend…</p></div>`;

  // Named rather than left out: someone arriving from a private result will
  // look for the rental block, and its absence is a data limit, not an omission.
  html += "<h3>Rental &amp; yield</h3>";
  html += "<p class='note'>Not available for HDB — the rental data behind the " +
          "private figures is URA's, which covers private housing only.</p>";

  resultsBox.innerHTML = html;
  resultsBox.hidden = false;
  currentDev = d.development;
  resultsBox.querySelectorAll(".hdb-block").forEach((b) => {
    b.onclick = () => runSearch(b.dataset.name);
  });
  renderFlatTypesChart(d);
}

// ── Charts (single series, one hue; text stays in ink tokens) ───────────────

function renderFlatTypesChart(d) {
  const entries = Object.entries(d.flat_types || {}).filter(([, v]) => v.avg_psf);
  const canvas = el("flats-chart");
  if (!canvas) return;
  if (!entries.length) {
    canvas.closest(".chart-box").innerHTML = "<p class='note'>No PSF available for these sales.</p>";
    return;
  }
  bandsChart = new Chart(canvas, {
    type: "bar",
    data: {
      labels: entries.map(([ft]) => ft),
      datasets: [{
        label: "Avg PSF",
        data: entries.map(([, v]) => v.avg_psf),
        backgroundColor: BRAND,
        borderWidth: 0,
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => {
              const [, v] = entries[c.dataIndex];
              return `${fmtMoney(v.avg_psf)} psf · ${v.count} sold`;
            },
          },
        },
      },
      scales: {
        x: { ticks: { color: INK_MUTED }, grid: { color: GRID } },
        y: { ticks: { color: INK_MUTED }, grid: { display: false } },
      },
    },
  });
}

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

// Private trends are keyed by development name, HDB by block + street (a block
// number means nothing without its street). The *responses* are the same shape
// — hdb.price_trend returns ura.price_trend's dict — so only the URL differs
// and everything below this line is shared.
function trendUrl(d) {
  if (d.market !== "hdb") return "/api/trend?q=" + encodeURIComponent(d.development);
  let url = "/api/hdb/trend?street=" + encodeURIComponent(d.street);
  if (d.block) url += "&block=" + encodeURIComponent(d.block);
  return url;
}

async function loadTrend(d) {
  trendAbort = new AbortController();
  const { signal } = trendAbort;
  let t;
  try {
    const r = await fetch(trendUrl(d), { signal });
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
        // Nearby rows are private developments whatever the origin was (an
        // HDB block included), so they wear the private market's popup.
        .bindPopup(MARKETS.private.popup(dev));
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
        `<span class="near-meta" title="${esc(districtLabel(dev.district))}">` +
        `D${parseInt(dev.district, 10)} · ${psf}</span></span></button>`
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
// TWO markets, one at a time. Private developments and HDB blocks are never
// plotted together: a cluster bubble averaging a condo's PSF with a flat's
// says nothing about either, and the two have almost no attributes in common
// (tenure and yield against lease decay and flat type). So each market owns
// its dots, its cluster layer, its colour metrics and its filters, and the
// toggle swaps which one is on the map.
//
// Colour is a SEQUENTIAL encoding: one hue per metric, light→dark, binned into
// quintiles. Steps are re-stepped off the reference ramps for the OneMap tile
// surface (#eeece6) rather than a near-white chart surface — the lightest
// reference steps sat under the 2:1 floor against real tiles.
// Bin edges are computed ONCE per market from its full dataset, so filtering
// never repaints the dots that survive. They are per market as well as per
// metric because the same metric spans different worlds: private PSF runs to
// S$4k and HDB PSF tops out near S$1.4k, so one shared scale would paint
// every flat with the lightest step.

const RAMP_BLUE = ["#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"];
const RAMP_ORANGE = ["#ee7d45", "#e35f26", "#c44e1f", "#a03f14", "#7a2f0c"];
const RAMP_TEAL = ["#5cb3a6", "#2f9d8d", "#1f7d70", "#165d54", "#0d3d38"];
const NO_DATA = "#b9b7b0";          // dots with no value for the active metric
const CLUSTER_INK = ["#0b0b0b", "#0b0b0b", "#fff", "#fff", "#fff"];  // >=4.18:1 on every step

const psfFmt = (v) => "S$" + Math.round(v).toLocaleString("en-SG");

// A metric is created per market, never shared, because `bins` belongs to a
// dataset rather than to a metric.
const metricDef = (field, label, ramp, fmt, empty) =>
  ({ field, label, ramp, fmt, empty, bins: null });

const FLAT_SHORT = {
  "1 ROOM": "1-rm", "2 ROOM": "2-rm", "3 ROOM": "3-rm", "4 ROOM": "4-rm",
  "5 ROOM": "5-rm", "EXECUTIVE": "Exec", "MULTI-GENERATION": "Multi-gen",
};
const flatLabel = (t) => FLAT_SHORT[t] || t;

const TENURE_LABELS = {
  freehold: "Freehold",
  999: "999-year lease",
  99: "99-year lease",
  other: "Other lease term",
};

// District -> estate names, from /api/developments (district_search.py is the
// single source of truth; the dots only ever carry the number).
let districtNames = {};

// A district number tells a local nothing on its own, so every place one is
// shown gets its estate names: "D19 · Hougang / Serangoon / Punggol".
const districtLabel = (code) => {
  const n = parseInt(code, 10);
  if (!n) return "";
  const towns = districtNames[code] || districtNames[String(n).padStart(2, "0")];
  return towns ? `D${n} · ${towns}` : `D${n}`;
};

const line = (label, val) => `<div class="popup-line">${label}: ${val}</div>`;
const psfLine = (d) =>
  d.avg_psf
    ? `${fmtMoney(d.avg_psf)} psf · ${d.txns_12mo} txn${d.txns_12mo === 1 ? "" : "s"}`
    : "<span class='muted'>none in last 12 mo</span>";
const mrtLine = (d) => (d.mrt_m != null ? `${d.mrt_m.toLocaleString("en-SG")} m` : "–");
const viewLink = (q) =>
  `<div class="popup-line"><a href="#" class="popup-view" data-name="${esc(q)}">View details →</a></div>`;

const MARKETS = {
  private: {
    id: "private",
    endpoint: "/api/developments",
    rowsKey: "developments",
    backLabel: "← All developments",
    hint: "Click a dot (or cluster) for details.",
    loadingHint: "Loading all developments…",
    panelIds: ["f-private"],
    metrics: {
      psf: metricDef("avg_psf", "12-mo avg PSF", RAMP_BLUE, psfFmt,
                     "no transactions in the last 12 months"),
      yield: metricDef("yield_pct", "Gross yield", RAMP_ORANGE,
                       (v) => v.toFixed(2) + "%",
                       "not enough recent leases to compute a yield"),
    },
    metricLabels: { psf: "Avg PSF", yield: "Gross yield" },
    sel: { district: new Set(), tenure: new Set() },
    dots: [],
    layer: null,
    metric: null,
    onLoad(payload) {
      districtNames = payload.districts || {};
      buildChips("f-districts", [...new Set(this.dots.map((d) => d.dev.district).filter(Boolean))]
        .sort((a, b) => parseInt(a, 10) - parseInt(b, 10)), (d) => ({
          key: d,
          html: `<span class="chip-code">D${parseInt(d, 10)}</span>` +
                (districtNames[d] ? ` · ${esc(districtNames[d])}` : ""),
        }), "district");
    },
    popup(d) {
      return (
        `<div class="popup-name">${esc(d.project)}</div>` +
        `<div class="popup-line">${esc(d.street)}</div>` +
        `<div class="popup-line">${esc(districtLabel(d.district))}</div>` +
        (d.distance_m != null
          ? line("Distance", `${d.distance_m.toLocaleString("en-SG")} m away`) : "") +
        line("12-mo avg", psfLine(d)) +
        line("Gross yield", d.yield_pct ? d.yield_pct.toFixed(2) + "%" : "<span class='muted'>–</span>") +
        line("Tenure", TENURE_LABELS[d.tenure] || "–") +
        line("Nearest MRT", mrtLine(d)) +
        line("Last transaction", d.last_txn ? esc(d.last_txn) : "–") +
        viewLink(d.project)
      );
    },
    matches(d) {
      if (this.sel.tenure.size && !this.sel.tenure.has(d.tenure)) return false;
      if (this.sel.district.size && !this.sel.district.has(d.district)) return false;
      return true;
    },
  },

  hdb: {
    id: "hdb",
    endpoint: "/api/hdb/blocks",
    rowsKey: "blocks",
    backLabel: "← All HDB blocks",
    hint: "Click a block (or cluster) for details.",
    loadingHint: "Loading every HDB block…",
    panelIds: ["f-hdb"],
    metrics: {
      psf: metricDef("avg_psf", "12-mo avg PSF", RAMP_BLUE, psfFmt,
                     "no resale transactions in the last 12 months"),
      // The signal private has no analogue for. 100% coverage — every block
      // in the window carries a lease reading — so this metric never greys
      // a dot out, which is also why it is worth offering.
      lease: metricDef("lease_years", "Remaining lease", RAMP_TEAL,
                       (v) => Math.round(v) + " yrs", "lease unknown"),
    },
    metricLabels: { psf: "Avg PSF", lease: "Remaining lease" },
    sel: { town: new Set(), flat: new Set() },
    dots: [],
    layer: null,
    metric: null,
    onLoad(payload) {
      buildChips("f-towns", [...new Set(this.dots.map((d) => d.dev.town).filter(Boolean))].sort(),
                 (t) => ({ key: t, html: esc(titleCase(t)) }), "town");
      const present = new Set(this.dots.flatMap((d) => d.dev.flat_types || []));
      buildChips("f-flats", (payload.flat_types || []).filter((t) => present.has(t)),
                 (t) => ({ key: t, html: esc(flatLabel(t)) }), "flat");
    },
    popup(d) {
      return (
        `<div class="popup-name">${esc(d.block)} ${esc(titleCase(d.street))}</div>` +
        `<div class="popup-line">${esc(titleCase(d.town))}</div>` +
        (d.distance_m != null
          ? line("Distance", `${d.distance_m.toLocaleString("en-SG")} m away`) : "") +
        line("12-mo avg", psfLine(d)) +
        line("12-mo median", d.med_price ? fmtMoney(d.med_price) : "<span class='muted'>–</span>") +
        line("Remaining lease", d.lease_years != null ? `${Math.round(d.lease_years)} yrs` : "–") +
        line("Flat types", (d.flat_types || []).map(flatLabel).join(", ") || "–") +
        line("Nearest MRT", mrtLine(d)) +
        line("Last transaction", d.last_txn ? esc(d.last_txn) : "–") +
        viewLink(`${d.block} ${d.street}`)
      );
    },
    matches(d) {
      if (this.sel.town.size && !this.sel.town.has(d.town)) return false;
      if (this.sel.flat.size && !(d.flat_types || []).some((t) => this.sel.flat.has(t)))
        return false;
      const minLease = parseInt(el("f-lease").value, 10);
      if (minLease && (d.lease_years == null || d.lease_years < minLease)) return false;
      return true;
    },
  },
};

for (const m of Object.values(MARKETS)) m.metric = m.metrics.psf;

let market = MARKETS.private;   // the landing market
let exploreOn = false;
// The dot list loads unprompted at boot and the search box is live throughout,
// so a search can land mid-flight. This says who owns the screen when it does.
let searchActive = false;

const backBtn = el("back-to-explore");
const panel = el("explore-panel");

// HDB streets are stored shouting ("BISHAN ST 22"); a map full of capitals is
// a wall, so they are cased for display only — never for matching.
const titleCase = (s) =>
  String(s || "").toLowerCase().replace(/\b[a-z]/g, (c) => c.toUpperCase());

const dotValue = (dev) => dev[market.metric.field];

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
// smaller, grey, semi-transparent. A third of developments have no recent
// transaction, and colouring them like a real value would invent one.
function dotStyle(dev) {
  const v = dotValue(dev);
  return v == null
    ? { radius: 4, color: "#fff", weight: 1, fillColor: NO_DATA, fillOpacity: 0.55 }
    : { radius: 6.5, color: "#fff", weight: 2, fillColor: colorOf(v, market.metric), fillOpacity: 0.95 };
}

// Clusters carry the mean of their children — without this the colour encoding
// is invisible at the default zoom, where almost every dot is inside a cluster.
function clusterIcon(cluster) {
  const vals = cluster.getAllChildMarkers()
    .map((mk) => dotValue(mk.dev)).filter((v) => v != null);
  const mean = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  const n = cluster.getChildCount();
  const size = n < 20 ? 32 : n < 100 ? 40 : 48;
  const ink = mean == null ? "#0b0b0b" : CLUSTER_INK[binOf(mean, market.metric)];
  return L.divIcon({
    className: "cluster-wrap",
    iconSize: [size, size],
    html:
      `<div class="cluster" style="background:${colorOf(mean, market.metric)};color:${ink};` +
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
//
// Three of them mean the same thing in both markets (a recent transaction, a
// walk to the MRT, a price per square foot) and are read here; everything else
// is market-specific and lives in that market's own `matches`.

function readFilters() {
  const num = (id) => {
    const v = parseFloat(el(id).value);
    return Number.isFinite(v) ? v : null;
  };
  return {
    activeOnly: el("f-active").checked,
    mrt: parseInt(el("f-mrt").value, 10) || null,
    psfMin: num("f-psf-min"),
    psfMax: num("f-psf-max"),
  };
}

function matches(dev, f) {
  if (f.activeOnly && !dev.txns_12mo) return false;
  if (f.mrt && (dev.mrt_m == null || dev.mrt_m > f.mrt)) return false;
  if (f.psfMin != null && (dev.avg_psf == null || dev.avg_psf < f.psfMin)) return false;
  if (f.psfMax != null && (dev.avg_psf == null || dev.avg_psf > f.psfMax)) return false;
  return market.matches(dev);
}

function applyFilters() {
  if (!market.layer) return;
  const f = readFilters();
  const keep = market.dots.filter((d) => matches(d.dev, f));
  market.layer.clearLayers();
  market.layer.addLayers(keep.map((d) => d.marker));   // bulk add: one reflow
  // A blank map is never left unexplained — without this, over-narrow filters
  // look identical to a broken layer. The message sits on the filter row
  // itself, beside the Reset button: the status box is below the fold here.
  const total = market.dots.length.toLocaleString("en-SG");
  el("filter-count").textContent = keep.length
    ? `${keep.length.toLocaleString("en-SG")} of ${total} shown`
    : `No matches — widen filters, or tap`;
  el("filter-count").classList.toggle("empty", keep.length === 0);
}

function renderLegend() {
  const m = market.metric;
  const edges = m.bins;
  const swatch = (c, text) => `<span class="lg"><i style="background:${c}"></i>${text}</span>`;
  const cells = m.ramp.map((c, i) => {
    const lo = i === 0 ? null : edges[i - 1];
    const hi = i === m.ramp.length - 1 ? null : edges[i];
    const text =
      lo == null ? `< ${m.fmt(hi)}`
      : hi == null ? `${m.fmt(lo)} +`
      : `${m.fmt(lo)}–${m.fmt(hi)}`;
    return swatch(c, text);
  });
  const anyMissing = market.dots.some(({ dev }) => dev[m.field] == null);
  el("ramp-legend").innerHTML =
    `<div class="lg-title">${m.label}</div>` +
    cells.join("") + (anyMissing ? swatch(NO_DATA, "no data") : "");
}

// The metric buttons are built from the active market's own metrics: the two
// markets answer different questions (yield against lease decay), so the
// toggle is not a fixed pair of buttons.
function renderMetricToggle() {
  el("metric-toggle").innerHTML = Object.keys(market.metrics)
    .map((name) => {
      const on = market.metric === market.metrics[name];
      return `<button type="button" role="radio" data-metric="${name}" ` +
             `aria-checked="${on}" class="${on ? "on" : ""}">` +
             `${esc(market.metricLabels[name])}</button>`;
    })
    .join("");
}

function setMetric(name) {
  if (!market.metrics[name]) return;
  market.metric = market.metrics[name];
  renderMetricToggle();
  for (const { dev, marker } of market.dots) marker.setStyle(dotStyle(dev));
  if (market.layer) market.layer.refreshClusters();   // recolour cluster icons
  renderLegend();
}

// mrt_m comes from the cached station coords; if that cache is empty (no Mongo,
// no OneMap token) every dot has mrt_m = null and each distance option would
// match nothing — blanking the map. Offer the filter only when it can work.
function syncMrtAvailability() {
  const sel = el("f-mrt");
  const usable = market.dots.some((d) => d.dev.mrt_m != null);
  sel.disabled = !usable;
  if (!usable) {
    sel.value = "";
    sel.title = "Nearest-MRT data is unavailable right now";
  }
  el("mrt-label").classList.toggle("disabled", !usable);
}

// One chip cloud builder for districts, towns and flat types: same markup,
// same click handling, different keys.
function buildChips(containerId, values, render, group) {
  el(containerId).innerHTML = values
    .map((v) => {
      const { key, html } = render(v);
      return `<button type="button" class="chip" data-group="${group}" ` +
             `data-key="${esc(key)}">${html}</button>`;
    })
    .join("");
}

// ── Enter / exit ────────────────────────────────────────────────────────────

backBtn.addEventListener("click", () => enterExplore());

// Build a market's dot layer once. Split out from enterExplore so the boot
// load, a market switch and a later return from a search all share it — only
// the first of those has anything to fetch.
async function loadMarket(mk) {
  if (mk.dots.length) return;
  const payload = await (await fetch(mk.endpoint)).json();
  const rows = payload[mk.rowsKey] || [];
  for (const m of Object.values(mk.metrics)) m.bins = computeBins(rows, m);
  mk.dots = rows.map((dev) => {
    const marker = L.circleMarker([dev.lat, dev.lng], dotStyle(dev));
    marker.dev = dev;          // clusters read this to average their children
    marker.bindPopup(() => mk.popup(dev));   // lazily: 9.6k popups is a lot of HTML
    return { dev, marker };
  });
  mk.onLoad(payload);
}

async function enterExplore(target = market) {
  searchActive = false;
  const switching = target !== market;
  market = target;
  backBtn.hidden = true;
  syncMarketToggle();
  setStatus(market.loadingHint);
  try {
    await loadMarket(market);
    // The dot lists take a moment and the search box works the whole time, so
    // a result can already be on screen by now. It wins: the dots are built
    // and waiting, but showing them here would wipe the panel and yank the
    // camera back to the middle of Singapore under the user.
    if (searchActive) {
      setStatus("");
      return;
    }
    if (!market.layer) market.layer = newClusterLayer();
    for (const other of Object.values(MARKETS)) {
      if (other !== market && other.layer) map.removeLayer(other.layer);
    }
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
    syncFilterPanels();
    renderMetricToggle();
    renderLegend();
    syncMrtAvailability();
    map.addLayer(market.layer);
    applyFilters();
    // A market switch keeps the camera: the user has usually zoomed somewhere
    // they care about, and both layers cover the same island.
    if (!switching || !exploreOn) map.setView(SG_CENTER, 12);
    exploreOn = true;
    setStatus(market.hint);
  } catch (err) {
    // Search still works without the dot layer, so say what broke and stop
    // short of implying the whole app is down.
    setStatus("The map failed to load: " + err.message +
              "\nSearch by name or postal code still works.", true);
  }
}

function exitExplore(clearStatus = false) {
  if (market.layer) map.removeLayer(market.layer);
  panel.hidden = true;
  exploreOn = false;
  if (clearStatus) setStatus("");
}

function syncMarketToggle() {
  for (const b of el("market-toggle").querySelectorAll("button")) {
    const on = b.dataset.market === market.id;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", String(on));
  }
  backBtn.textContent = market.backLabel;
}

// Show the active market's own filters and hide the other's. The shared ones
// (recent transaction, MRT, PSF) are outside both groups and keep their
// values across a switch — they mean the same thing on either side.
function syncFilterPanels() {
  for (const mk of Object.values(MARKETS)) {
    for (const id of mk.panelIds) el(id).hidden = mk !== market;
  }
}

// ── Control wiring ──────────────────────────────────────────────────────────

el("market-toggle").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-market]");
  if (b && MARKETS[b.dataset.market] !== market) enterExplore(MARKETS[b.dataset.market]);
});

el("metric-toggle").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-metric]");
  if (b) setMetric(b.dataset.metric);
});

for (const id of ["f-active", "f-mrt", "f-psf-min", "f-psf-max", "f-lease"]) {
  el(id).addEventListener("input", applyFilters);
}

// Every chip cloud is multi-select for the same reason the buckets are split
// at all: freehold and 999-year are separate categories, "4-room or 5-room" is
// one normal thought, and wanting two towns is not two searches.
panel.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  const set = market.sel[chip.dataset.group];
  if (!set) return;
  const key = chip.dataset.key;
  set.has(key) ? set.delete(key) : set.add(key);
  chip.classList.toggle("on", set.has(key));
  applyFilters();
});

// Reset clears the shared controls and the market on screen — never the one
// behind it. Declassing every .chip in the panel also stripped the hidden
// market's chips, whose Sets still held them, so its filter count and its
// chips disagreed the next time it was shown.
el("reset-filters").addEventListener("click", () => {
  el("f-active").checked = false;
  el("f-mrt").value = "";
  el("f-psf-min").value = "";
  el("f-psf-max").value = "";
  for (const set of Object.values(market.sel)) set.clear();
  for (const id of market.panelIds) {
    for (const c of el(id).querySelectorAll(".chip")) c.classList.remove("on");
    for (const f of el(id).querySelectorAll("select, input")) f.value = "";
  }
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

// The search box's HDB routing table. Fetched unawaited and never blocking:
// it is small, and a search typed before it lands still routes by query shape.
loadHdbStreets();

// Explore is the landing state — the map opens full of developments rather
// than empty behind a button.
enterExplore();
