// Shared map code for both pages: incident pins, safety zones, helpers.

// Pin colors avoid red/yellow/green, which are reserved for safety-zone levels
const COLORS = { harassment: "#e84393", assault: "#6c3483", theft: "#2471a3", stalking: "#138d90", other: "#4d4d4d" };
const ZONE_COLORS = { none: "#2ecc71", some: "#f1c40f", many: "#e74c3c" };
const ROUTE_COLORS = { normal: "#7f8c8d", safer: "#27ae60" };

// Grid of ~150 m squares over downtown San Jose / SJSU. The grid is anchored
// to a fixed corner so squares stay in the same place as incidents are added.
const CELL_LAT = 0.0014, CELL_LNG = 0.0017;
const GRID = { south: 37.3180, west: -121.9100, north: 37.3520, east: -121.8700 };

// Escape text so article content can't inject HTML
function esc(s) {
  const d = document.createElement("div");
  d.textContent = String(s ?? "");
  return d.innerHTML;
}

// Shows a status message; loading=true adds a spinning circle in front
function showStatus(el, msg, { isError = false, loading = false } = {}) {
  el.innerHTML = msg ? (loading ? '<span class="spinner"></span>' : "") + esc(msg) : "";
  el.className = "status" + (isError ? " error" : "");
}

// Legend rows for the pin categories
function pinLegendHTML() {
  return Object.entries(COLORS)
    .map(([cat, color]) => `<div class="item"><span class="dot" style="background:${color}"></span>${cat}</div>`).join("");
}

// Creates a Leaflet map with incident pins and a safety-zones layer.
function createSafetyMap(elementId) {
  const map = L.map(elementId).setView([37.3352, -121.8811], 15);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);

  // Separate pane under the pins so zones never block tapping a pin
  map.createPane("zones");
  map.getPane("zones").style.zIndex = 350;

  const pins = L.layerGroup().addTo(map);
  const zones = L.layerGroup().addTo(map);
  const incidents = [];  // every incident currently on the map
  let redSquares = [];   // red squares as GeoJSON rings ([lng, lat] corners), for route avoidance

  function cellOf(lat, lng) {
    if (lat < GRID.south || lat >= GRID.north || lng < GRID.west || lng >= GRID.east) return null;
    return [Math.floor((lat - GRID.south) / CELL_LAT), Math.floor((lng - GRID.west) / CELL_LNG)];
  }

  function recomputeZones() {
    zones.clearLayers();
    redSquares = [];
    const counts = new Map();  // "row,col" -> number of incidents
    for (const inc of incidents) {
      const cell = cellOf(inc.lat, inc.lng);
      if (!cell) continue;  // outside the downtown grid
      const key = cell.join(",");
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    // Draw squares with incidents, plus green squares only in the ring around them
    const toDraw = new Set();
    for (const key of counts.keys()) {
      const [r, c] = key.split(",").map(Number);
      for (let dr = -1; dr <= 1; dr++)
        for (let dc = -1; dc <= 1; dc++) {
          const rr = r + dr, cc = c + dc;
          const s = GRID.south + rr * CELL_LAT, w = GRID.west + cc * CELL_LNG;
          if (s >= GRID.south && s < GRID.north && w >= GRID.west && w < GRID.east) toDraw.add(`${rr},${cc}`);
        }
    }
    for (const key of toDraw) {
      const [r, c] = key.split(",").map(Number);
      const n = counts.get(key) || 0;
      const color = n === 0 ? ZONE_COLORS.none : n <= 2 ? ZONE_COLORS.some : ZONE_COLORS.many;
      const s = GRID.south + r * CELL_LAT, w = GRID.west + c * CELL_LNG;
      if (n >= 3) {
        const nn = s + CELL_LAT, e = w + CELL_LNG;
        redSquares.push([[w, s], [e, s], [e, nn], [w, nn], [w, s]]);
      }
      L.rectangle([[s, w], [s + CELL_LAT, w + CELL_LNG]], {
        pane: "zones", interactive: false,
        color, weight: 1, opacity: 0.4, fillColor: color, fillOpacity: 0.25
      }).addTo(zones);
    }
  }

  function addPin(inc) {
    const color = COLORS[inc.category] || COLORS.other;
    L.circleMarker([inc.lat, inc.lng], {
      radius: 9, color: "#fff", weight: 2, fillColor: color, fillOpacity: 0.9,
      bubblingMouseEvents: false  // tapping a pin opens its popup without counting as a map tap
    }).bindPopup(
      `<b>${esc(inc.category)}</b><br>${esc(inc.summary)}<br><br>` +
      `<b>Where:</b> ${esc(inc.location_text)}<br>` +
      `<b>When:</b> ${esc(inc.date)}, ${esc(inc.time_of_day)}`
    ).addTo(pins);
  }

  return {
    map,
    incidents,
    getRedSquares: () => redSquares,
    addIncidents(list) {
      list.forEach(addPin);
      incidents.push(...list);
      recomputeZones();
    },
    clear() {
      pins.clearLayers();
      incidents.length = 0;
      recomputeZones();
    },
    setZonesVisible(show) {
      if (show) zones.addTo(map); else zones.remove();
    },
    async loadSaved() {
      const res = await fetch("/api/incidents");
      const data = await res.json();
      this.addIncidents(data.incidents);
      return data.incidents.length;
    }
  };
}
