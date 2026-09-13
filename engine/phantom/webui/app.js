/* Phantom renderer — premium dark map + WebSocket control of the engine.
   The "current location" shown on the map is the VIRTUAL (spoofed) position the
   engine is pushing to the iPhone — never the Mac's own GPS. */
"use strict";

const $ = (id) => document.getElementById(id);
const MPS_TO_MPH = 2.2369362920544;

// Per-launch auth token from the Electron preload. Sent on every backend /api
// call and as the /ws subprotocol so only this app can drive the device.
const TOKEN = (window.phantom && window.phantom.token) || "";
function apiFetch(path, opts) {
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {}, { "X-Phantom-Token": TOKEN });
  return fetch(path, Object.assign({}, opts, { headers }));
}
const MOVE_DT = 0.3; // ~3.3 Hz device updates — smooth on-screen motion
// (60fps client interpolation + buffered gliding keep the dot fluid).
const LUDICROUS_MPH = 22369;    // capped below the backend limit of 10,000 m/s
const NORMAL_MAX_MPH = 1000; // ceiling in normal mode
const MAX_WAYPOINTS = 50;

const state = {
  mode: "static",
  presets: { walk: 1.4, cycle: 5.0, drive: 16.7, fly: 222.0 },
  pin: null,           // {lat, lon} static-mode selection (the target)
  pinName: null,       // display name for the current pin (place/business), if any
  pinMarker: null,
  bookmarks: [],       // [{name, lat, lon}] saved spots
  recents: [],         // [{lat, lon, name}] recently-used spots
  waypoints: [],       // [{lat, lon}]
  wpMarkers: [],
  virtual: null,       // {lat, lon} the device's current virtual location
  virtualName: null,   // place name of the current spoof, if it came from a named spot
  deviceMarker: null,  // blue dot = current virtual location
  moving: false,
  ready: false,
  connected: false, paused: false, profile: "driving", logs: [], startedAt: Date.now(),
  centered: false,
  status: null,
  pollTimer: null,
  // smooth-motion interpolation
  prev: null,          // {lon, lat} shown position at last fix
  targetPt: null,      // {lon, lat} newest fix
  lastFixMs: 0,
  interval: 300,       // measured ms between fixes
  rafId: null,         // single-owner guard for the interpolation loop
  follow: true,
};

let map = null;

/* ---------------- boot ---------------- */

async function boot() {
  let cfg = {};
  try { cfg = await (await apiFetch("/api/health")).json(); } catch (_) {}
  if (cfg.presets) state.presets = cfg.presets;

  // Keyless, CORS-friendly, no per-key limits — and no shared key shipped to buyers.
  const style = "https://tiles.openfreemap.org/styles/dark";

  initMap(style);
  wireUI();
  wireExtras();
  applyPreset("drive");
  loadPlaces();
  connect();
}

// Display-only filters; keep routing and all original style filters intact.
const highwayFilters = new Map();
function showMainHighways(visible) {
  if (!map || !map.getStyle()?.layers) return;
  for (const layer of map.getStyle().layers) {
    if (!["transportation", "transportation_name"].includes(layer["source-layer"])) continue;
    if (!highwayFilters.has(layer.id)) highwayFilters.set(layer.id, layer.filter || null);
    const original = highwayFilters.get(layer.id);
    const exclude = ["!", ["in", ["get", "class"], ["literal", ["motorway", "trunk"]]]];
    map.setFilter(layer.id, visible ? original : original ? ["all", original, exclude] : exclude);
  }
}

const surfaceVisibility = new Map();
function setSatellite(enabled) {
  if (!map?.getStyle()?.layers) return;
  if (enabled && !map.getSource("satellite")) {
    map.addSource("satellite", { type: "raster", tileSize: 256, maxzoom: 14,
      tiles: ["https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/GoogleMapsCompatible/{z}/{y}/{x}.jpg"],
      attribution: 'Sentinel-2 cloudless — <a href="https://s2maps.eu">s2maps.eu</a> by <a href="https://eox.at">EOX IT Services GmbH</a> (Contains modified Copernicus Sentinel data 2016 &amp; 2017), <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>' });
    map.addLayer({ id: "satellite", type: "raster", source: "satellite" }, map.getStyle().layers[0]?.id);
  }
  if (map.getLayer("satellite")) map.setLayoutProperty("satellite", "visibility", enabled ? "visible" : "none");
  for (const layer of map.getStyle().layers) {
    if (!["background", "fill", "fill-extrusion", "hillshade"].includes(layer.type)) continue;
    if (!surfaceVisibility.has(layer.id)) surfaceVisibility.set(layer.id, layer.layout?.visibility || "visible");
    map.setLayoutProperty(layer.id, "visibility", enabled ? "none" : surfaceVisibility.get(layer.id));
  }
}

function initMap(style) {
  map = new maplibregl.Map({
    container: "map",
    style,
    center: [-98.5, 39.5],
    zoom: 3.6,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  map.on("error", (event) => {
    if (event.sourceId === "satellite" && $("mapView")?.value === "satellite") {
      $("mapView").value = "map";
      setSatellite(false);
      toast("Satellite imagery unavailable. Returned to map view.", "err");
    }
  });

  // OpenFreeMap occasionally references a sprite that is absent from its style.
  // Only city markers may use a circle. Unknown sprites must stay transparent:
  // a circle substituted for a repeating terrain texture covers the map in dots.
  map.on("styleimagemissing", (e) => {
    if (map.hasImage(e.id)) return;
    const size = 22;
    const data = new Uint8Array(size * size * 4);
    const c = (size - 1) / 2;
    for (let y = 0; y < size; y += 1) {
      for (let x = 0; x < size; x += 1) {
        const i = (y * size + x) * 4;
        const inside = e.id === "circle-11" && Math.hypot(x - c, y - c) <= c - 2;
        data[i] = 111; data[i + 1] = 154; data[i + 2] = 196; data[i + 3] = inside ? 210 : 0;
      }
    }
    map.addImage(e.id, { width: size, height: size, data });
  });

  // Register click immediately (not inside 'load') so waypoints/pins always work.
  map.on("click", onMapClick);

  map.on("load", () => {
    try {
      for (const layer of map.getStyle().layers) {
        if (layer.type === "fill" && layer.paint?.["fill-pattern"]) {
          map.setPaintProperty(layer.id, "fill-pattern", null);
        }
        // Keep all cities equally restrained, independent of population rank.
        if (layer.type === "symbol" && layer["source-layer"] === "place" && !/^place_(?:state|country)/.test(layer.id)) {
          map.setLayoutProperty(layer.id, "text-size", ["interpolate", ["linear"], ["zoom"], 3, 10, 8, 11, 12, 12]);
          map.setLayoutProperty(layer.id, "text-transform", "none");
          map.setPaintProperty(layer.id, "text-opacity", ["step", ["zoom"], 0, 8, 1]);
          map.setPaintProperty(layer.id, "icon-opacity", ["step", ["zoom"], 0, 8, 0.7]);
        }
        if (layer.type === "symbol" && layer.layout && layer.layout["text-field"]) map.setPaintProperty(layer.id, "text-color", "#d9e3ec");
        if (layer.type === "line" && /road|street|highway/.test(layer.id)) map.setPaintProperty(layer.id, "line-color", "#607389");
      }
      if (!map.getSource("route")) map.addSource("route", { type: "geojson", data: emptyLine() });
      if (!map.getLayer("route-casing")) map.addLayer({
        id: "route-casing", type: "line", source: "route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#08325f", "line-width": 9, "line-opacity": 0.55, "line-blur": 1 },
      });
      if (!map.getLayer("route-line")) map.addLayer({
        id: "route-line", type: "line", source: "route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#0a84ff", "line-width": 5, "line-opacity": 0.96 },
      });
    } catch (e) { console.error("[phantom] layer setup:", e); }
    showMainHighways($("showHighways")?.checked !== false);
    setSatellite($("mapView")?.value === "satellite");
    loadVirtual();
  });
}

const emptyLine = () => ({ type: "Feature", geometry: { type: "LineString", coordinates: [] } });

function drawRoute(points) {
  if (!map.getSource("route")) return;
  map.getSource("route").setData({ type: "Feature", geometry: { type: "LineString", coordinates: points.map(([la, lo]) => [lo, la]) } });
}

function makeMarker(lat, lon, cls, label) {
  const el = document.createElement("div");
  el.className = "mk " + cls;
  if (label) el.textContent = label;
  return new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(map);
}

function bounds(points) {
  const b = new maplibregl.LngLatBounds();
  points.forEach(([la, lo]) => b.extend([lo, la]));
  return b;
}

/* ---------------- virtual location ("current location" dot) ---------------- */

async function loadVirtual() {
  try {
    const w = await (await apiFetch("/api/where")).json();
    if (state.ready && ["holding", "moving", "paused", "arrived"].includes(state.status?.session?.state)) return;
    if (w && w.source === "virtual" && w.lat != null) {
      showDevice(w.lat, w.lon, false);
      map.jumpTo({ center: [w.lon, w.lat], zoom: 14 });
      state.centered = true;
      setSpoofed(false); // Saved coordinates are not confirmation of active device control.
      $("sessionUpdate").textContent = "Saved position shown · device location unconfirmed";
    }
  } catch (_) {}
}

function showDevice(lat, lon, moving) {
  state.virtual = { lat, lon };
  if (!state.deviceMarker) state.deviceMarker = makeMarker(lat, lon, "me" + (moving ? " moving" : ""));
  else {
    state.deviceMarker.setLngLat([lon, lat]);
    state.deviceMarker.getElement().className = "mk me" + (moving ? " moving" : "");
  }
}

function clearDevice() {
  if (state.deviceMarker) { state.deviceMarker.remove(); state.deviceMarker = null; }
  state.virtual = null;
}

function centerOnDevice(fly) {
  if (!state.virtual) return toast("No virtual location yet — set one, then tap ◎.", "err");
  const c = [state.virtual.lon, state.virtual.lat];
  fly ? map.flyTo({ center: c, zoom: 15, duration: 900 }) : map.jumpTo({ center: c, zoom: 14 });
}

/* ---------------- websocket ---------------- */

let ws = null;
let statusTimer = null;

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`, TOKEN ? ["phantom-" + TOKEN] : []);
  ws.onopen = () => {
    state.connected = true; updateControls();
    send({ cmd: "status" });
    statusTimer = setInterval(() => !state.moving && send({ cmd: "status" }), 4000);
  };
  ws.onclose = () => { clearInterval(statusTimer); state.connected = false; state.ready = false; state.moving = false; state.prev = null; state.targetPt = null; setSpoofed(false); updateControls(); $("sessionConnection").textContent = "Disconnected — reconnecting…"; $("gateMsg").textContent = "Engine disconnected. Commands are unavailable until it reconnects."; $("gate").classList.remove("hidden"); setTimeout(connect, 1500); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}

function send(obj) { if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(obj)); if (obj.cmd !== "status") $("sessionCommand").textContent = "Last command sent: " + new Date().toLocaleTimeString(); return true; } if (obj.cmd !== "status") toast("Command was not sent: engine disconnected. Reconnect and try again.", "err"); return false; }

function handle(msg) {
  if (msg.type !== "fix" && msg.type !== "status") { state.logs.push({at: new Date().toISOString(), ...msg}); if (state.logs.length > 1000) state.logs.shift(); }
  if (["fix", "located", "cleared"].includes(msg.type)) $("sessionUpdate").textContent = "Last update: " + new Date().toLocaleTimeString();
  if (["route", "fix", "paused", "resumed", "arrived", "stopped", "failed", "interrupted", "located", "cleared"].includes(msg.type)) $("sessionConnection").textContent = "Connected · " + ({route:"Moving",fix:state.paused ? "Paused" : msg.dwell_remaining_s > 0 ? "Timed stop" : "Moving",located:"Holding location",cleared:"Real GPS restored"}[msg.type] || msg.type);
  switch (msg.type) {
    case "status": return onStatus(msg);
    case "located":
      // The green "target" pin is now confirmed — it becomes the blue device
      // dot, so drop it to avoid two overlapping markers on the same spot.
      if (state.pinMarker) { state.pinMarker.remove(); state.pinMarker = null; }
      state.pin = null;
      showDevice(msg.lat, msg.lon, false);
      map.flyTo({ center: [msg.lon, msg.lat], zoom: 15, duration: 800 });
      setSpoofed(true);
      toast(`Now at ${state.virtualName || `${msg.lat.toFixed(5)}, ${msg.lon.toFixed(5)}`}`, "ok");
      $("pickInfo").textContent = "Tap the map or search to choose a spot.";
      $("pickInfo").classList.add("empty");
      loadPlaces(); // the engine just recorded this as a recent
      break;
    case "cleared":
      clearDevice(); state.virtualName = null; setSpoofed(false);
      toast("Real GPS restored.", "ok");
      break;
    case "route": drawRoute(msg.points); if (msg.points.length) map.fitBounds(bounds(msg.points), { padding: 120, duration: 700 }); break;
    case "fix": return onFix(msg);
    case "arrived":
      if (msg.cleared) { clearDevice(); state.virtualName = null; setSpoofed(false); }
      else if (msg.lat != null) showDevice(msg.lat, msg.lon, false);
      endMove("Arrived.");
      break;
    case "stopped":
      if (msg.cleared) { clearDevice(); state.virtualName = null; setSpoofed(false); }
      else if (msg.lat != null) showDevice(msg.lat, msg.lon, false);
      endMove("Stopped.");
      break;
    case "tunnel":
      if (msg.state === "error") toast("Couldn't enable: " + (msg.message || "cancelled"), "err");
      else { toast("Enabling location control… a few seconds.", "ok"); pollUntilReady(); }
      break;
    case "extended": state.pendingAddStop = null; drawRoute(msg.points); toast("Route extended", "ok"); break;
    case "paused": state.paused = true; $("pauseBtn").textContent = "Resume"; break;
    case "resumed": state.paused = false; $("pauseBtn").textContent = "Pause"; break;
    case "preferences": $("autoResume").checked = !!msg.auto_resume; break;
    case "failed": case "interrupted": state.targetPt = null; state.prev = null; setSpoofed(false); endMove(); toast(msg.message || "Movement interrupted. Check the device and retry.", "err"); break;
    case "speed": break;
    case "info": toast(msg.message, ""); break;
    case "error": toast(msg.message || "Error", "err"); $("straightRoute").classList.toggle("hidden", msg.code !== "routing_failed"); if (msg.cmd === "add_stop" || (!msg.cmd && state.pendingAddStop)) { if (state.pendingAddStop) state.waypoints = state.waypoints.filter(w => w !== state.pendingAddStop); state.pendingAddStop = null; renderWaypoints(); } else if (state.moving && msg.cmd !== "set_speed" && msg.cmd !== "preferences") endMove(); break;
  }
}

/* ---------------- device status ---------------- */

function onStatus(s) {
  state.ready = !!s.ready;
  state.status = s;
  const session = s.session || {};
  if (["holding", "moving", "paused", "arrived"].includes(session.state) && session.lat != null && s.ready) { showDevice(session.lat, session.lon, session.state === "moving"); setSpoofed(true); } else if (["idle", "cleared", "interrupted", "failed"].includes(session.state)) setSpoofed(false);
  if (session.last_command_at) $("sessionCommand").textContent = "Last command: " + new Date(session.last_command_at * 1000).toLocaleTimeString();
  $("sessionConnection").textContent = s.ready ? "Connected · " + (session.state || "Ready") : "Connected · Device setup required";
  $("autoResume").disabled = !session.device_udid; $("autoResume").checked = !!session.auto_resume;
  state.deviceId = session.device_udid;
  if (["moving", "running", "paused"].includes(session.state)) {
    state.moving = true; state.paused = session.state === "paused";
    $("pauseBtn").textContent = state.paused ? "Resume" : "Pause";
    $("pauseBtn").classList.remove("hidden"); $("stopBtn").classList.remove("hidden"); $("startBtn").classList.add("hidden");
  } else if (session.state) {
    state.moving = false; state.paused = false; state.prev = null; state.targetPt = null; $("pauseBtn").classList.add("hidden"); $("stopBtn").classList.add("hidden"); $("startBtn").classList.remove("hidden"); $("readout").classList.add("hidden");
  }
  if (session.last_update_at) $("sessionUpdate").textContent = "Last update: " + new Date(session.last_update_at * 1000).toLocaleTimeString();
  updateControls();
  $("statusDot").className = "dot " + (s.ready ? "ready" : (s.device ? "busy" : "off"));
  $("deviceName").textContent = s.device ? s.device.name : "No device";
  $("diName").textContent = s.device ? s.device.name : "—";
  $("diModel").textContent = s.device ? s.device.model : "—";
  $("diIos").textContent = s.device ? s.device.ios : "—";
  $("diStatus").innerHTML = s.ready ? '<span class="ok">Ready</span>' : (s.device ? '<span class="bad">Not ready</span>' : "—");
  $("diDev").innerHTML = s.developer_mode === true ? '<span class="ok">On</span>'
    : s.developer_mode === false ? '<span class="bad">Off</span>' : "Unknown";
  $("diHint").textContent = (s.hints && s.hints.length && !s.ready) ? s.hints[0] : "";
  updateGate(s);
  if (s.ready && state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

function setSpoofed(on) {
  $("diSpoof").innerHTML = on ? '<span class="ok">Yes</span>' : "No";
  const bar = $("spoofBar");
  if (on && state.virtual) {
    const coords = `${state.virtual.lat.toFixed(5)}, ${state.virtual.lon.toFixed(5)}`;
    $("spoofCoords").textContent = (!state.moving && state.virtualName) ? state.virtualName : coords;
    bar.classList.remove("hidden");
  } else {
    bar.classList.add("hidden");
  }
}

/* ---------------- readiness gate ---------------- */

function updateGate(s) {
  const gate = $("gate"), msg = $("gateMsg"), btn = $("gateBtn");
  if (s.ready) { gate.classList.add("hidden"); return; }
  gate.classList.remove("hidden");
  btn.classList.add("hidden");
  if (!s.device) {
    msg.textContent = "Plug in your iPhone with a cable and unlock it.";
  } else if (s.tunnel_state === "tunneld-down") {
    msg.textContent = "Location control is off.";
    btn.textContent = "Enable";
    btn.classList.remove("hidden");
  } else if (s.tunnel_state === "upgrade-required") {
    msg.textContent = "Security update required for the USB helper.";
    btn.textContent = "Secure & Enable";
    btn.classList.remove("hidden");
  } else if (s.developer_mode === false) {
    msg.textContent = "Turn on Developer Mode: Settings › Privacy & Security › Developer Mode, then restart.";
  } else {
    msg.textContent = (s.hints && s.hints[0]) || "Getting ready…";
  }
}

function enableTunnel() {
  toast("Approve the macOS admin prompt to enable location control…", "ok");
  send({ cmd: "start_tunnel" });
}

function attemptEnable() {
  const s = state.status || {};
  if (!s.device) return toast("Plug in your iPhone (unlocked) first.", "err");
  if (s.tunnel_state === "tunneld-down" || s.tunnel_state === "upgrade-required") return enableTunnel();
  if (s.developer_mode === false) return toast("Enable Developer Mode on the iPhone first.", "err");
  toast("Getting ready…", "");
  send({ cmd: "status" });
}

function pollUntilReady() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  let n = 0;
  state.pollTimer = setInterval(() => {
    send({ cmd: "status" });
    if (++n > 20 || state.ready) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }, 2000);
}

/* ---------------- modes ---------------- */

function setMode(mode) {
  state.mode = mode;
  [...$("modeToggle").children].forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("routePanel").classList.toggle("hidden", mode !== "route");
  // The action bar (with Reset) stays visible in BOTH modes so you can always
  // restore real GPS; only the static "Change Location" control is mode-specific.
  $("setBtn").classList.toggle("hidden", mode !== "static");
  $("pickInfo").classList.toggle("hidden", mode !== "static");
}

/* ---------------- map interaction ---------------- */

function onMapClick(e) {
  const lat = e.lngLat.lat, lon = e.lngLat.lng;
  if (state.mode === "static") setPin(lat, lon); else addWaypoint(lat, lon);
}

function setPin(lat, lon, name) {
  state.pin = { lat, lon };
  state.pinName = name || null;
  if (state.pinMarker) state.pinMarker.setLngLat([lon, lat]);
  else state.pinMarker = makeMarker(lat, lon, "pin");
  const coords = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  $("pickInfo").textContent = name ? `${name} · ${coords}` : coords;
  $("pickInfo").classList.remove("empty");
}

function addWaypoint(lat, lon) {
  if (state.waypoints.length >= MAX_WAYPOINTS) { toast(`Max ${MAX_WAYPOINTS} waypoints.`, "err"); return; }
  if (state.moving && state.pendingAddStop) return toast("Wait for the previous stop to be confirmed.", "err");
  const waypoint = { lat, lon, dwell_s: 0 };
  if (state.moving) { if (!send({ cmd: "add_stop", lat, lon })) return; state.pendingAddStop = waypoint; }
  state.waypoints.push(waypoint);
  renderWaypoints();
  // // extend the live route on the fly
}

function renderWaypoints() {
  state.wpMarkers.forEach((m) => m.remove());
  state.wpMarkers = state.waypoints.map((w, i) => makeMarker(w.lat, w.lon, "wp", String(i + 1)));
  const ol = $("waypointList");
  ol.innerHTML = "";
  state.waypoints.forEach((w, i) => {
    const li = document.createElement("li");
    const idx = document.createElement("span"); idx.className = "idx"; idx.textContent = String(i + 1); li.append(idx);
    for (const key of ["lat", "lon"]) {
      const input = document.createElement("input"); input.type = "number"; input.step = "any"; input.value = w[key]; input.disabled = state.moving;
      input.setAttribute("aria-label", `Waypoint ${i + 1} ${key === "lat" ? "latitude" : "longitude"}`);
      input.onchange = () => { const value = Number(input.value), limit = key === "lat" ? 90 : 180; if (!input.value || !Number.isFinite(value) || Math.abs(value) > limit) { input.value = w[key]; return toast("Invalid coordinate.", "err"); } w[key] = value; renderWaypoints(); };
      const field = document.createElement("label"); field.className = "waypoint-field";
      const caption = document.createElement("span"); caption.textContent = key === "lat" ? "Latitude" : "Longitude";
      field.append(caption, input); li.append(field);
    }
    const dwell = document.createElement("input"); dwell.type="number"; dwell.min=0; dwell.max=86400; dwell.value=w.dwell_s || 0; dwell.disabled=state.moving; dwell.setAttribute("aria-label", `Waypoint ${i+1} stop duration in seconds`); dwell.title="Stop duration (seconds)";
    dwell.onchange=()=>{const value=Number(dwell.value); if(!Number.isFinite(value)||value<0||value>86400){dwell.value=w.dwell_s||0;return toast("Stop duration must be 0–86400 seconds.","err");} w.dwell_s=value;};
    const dwellField = document.createElement("label"); dwellField.className="waypoint-field waypoint-dwell";
    const dwellCaption = document.createElement("span"); dwellCaption.textContent="Stop · seconds";
    dwellField.append(dwellCaption, dwell); li.append(dwellField);
    const actions = document.createElement("div"); actions.className="waypoint-actions"; li.append(actions);
    const reorder = (to) => { if (state.moving || to < 0 || to >= state.waypoints.length) return; state.waypoints.splice(to, 0, state.waypoints.splice(i, 1)[0]); renderWaypoints(); };
    for (const [label, action] of [["↑", () => reorder(i - 1)], ["↓", () => reorder(i + 1)], ["✕", () => { state.waypoints.splice(i, 1); renderWaypoints(); }]]) {
      const btn = document.createElement("button"); btn.textContent = label; btn.setAttribute("aria-label", `${label === "✕" ? "Remove" : label === "↑" ? "Move up" : "Move down"} waypoint ${i + 1}`); btn.disabled = state.moving || (label === "↑" && i === 0) || (label === "↓" && i === state.waypoints.length - 1); btn.onclick = action; actions.append(btn);
    }
    li.draggable = !state.moving;
    li.ondragstart = e => e.dataTransfer.setData("text/plain", String(i));
    li.ondragover = e => e.preventDefault();
    li.ondrop = e => { e.preventDefault(); const from = Number(e.dataTransfer.getData("text/plain")); if (state.moving || !Number.isInteger(from) || from < 0 || from >= state.waypoints.length) return; state.waypoints.splice(i, 0, state.waypoints.splice(from, 1)[0]); renderWaypoints(); };
    ol.appendChild(li);
  });
  if (!state.moving) drawRoute(state.waypoints.map(w => [w.lat, w.lon]));
  $("wpCount").textContent = `(${state.waypoints.length})`;
  $("wpHint").style.display = state.waypoints.length ? "none" : "";
}

/* ---------------- speed controls ---------------- */

// Logarithmic slider: raw position 0..1000 maps to 1..MAX_MPH so the whole
// range (walking to warp) is draggable with usable resolution at every scale.
// Speed cap depends on Ludicrous mode; the log slider rescales to match.
function maxMph() { return ($("ludicrous") && $("ludicrous").checked) ? LUDICROUS_MPH : NORMAL_MAX_MPH; }
function logMax() { return Math.log10(maxMph()); }
function posToMph(pos) {
  return Math.round(Math.pow(10, (pos / 1000) * logMax()));
}
function mphToPos(mph) {
  const m = Math.max(1, Math.min(maxMph(), mph));
  return Math.round((Math.log10(m) / logMax()) * 1000);
}

function applyPreset(preset) {
  if (preset === "custom") return;
  state.profile = ({walk: "walking", cycle: "cycling", drive: "driving", fly: "driving"})[preset];
  const mph = Math.round((state.presets[preset] || state.presets.drive) * MPS_TO_MPH);
  setMph(mph);
  $("speedSelect").value = preset;
  $("followRoads").checked = preset !== "fly";
}

function setMph(mph) {
  mph = Math.max(1, Math.min(maxMph(), Math.round(mph)));
  $("speedMph").max = maxMph();
  $("speedMph").value = mph;
  const sl = $("speedSlider");
  sl.value = mphToPos(mph);
  sl.style.setProperty("--fill", `${(Number(sl.value) / Number(sl.max)) * 100}%`);
}

function currentMph() {
  const v = parseFloat($("speedMph").value);
  if (!Number.isFinite(v) || v <= 0) return 37;
  return Math.min(v, maxMph());
}

// Push a live speed change to a running route.
function pushLiveSpeed() {
  if (state.moving) send({ cmd: "set_speed", speed_mps: currentMph() / MPS_TO_MPH });
}

// Compact speed text so huge values don't blow out the readout.
function fmtMph(mph) {
  if (mph >= 1e6) return mph.toExponential(1).replace("e+", "e");
  return Math.round(mph).toLocaleString();
}

/* ---------------- movement ---------------- */

function onFix(f) {
  const now = performance.now();
  // Interpolate from the currently-shown position toward the new fix over the
  // *measured* inter-fix interval, so the dot glides at 60fps instead of jumping.
  const cur = state.deviceMarker ? state.deviceMarker.getLngLat() : { lng: f.lon, lat: f.lat };
  state.prev = { lon: cur.lng, lat: cur.lat };
  state.targetPt = { lon: f.lon, lat: f.lat };
  // Interpolate over ~1.4x the real gap so the dot keeps gliding to the next
  // point and never stalls waiting for the next 1 Hz fix (that stall reads as lag).
  const gap = state.lastFixMs ? (now - state.lastFixMs) : MOVE_DT * 1000;
  state.interval = Math.min(Math.max(gap, 300), 2000) * 1.4;
  state.lastFixMs = now;
  state.virtual = { lat: f.lat, lon: f.lon };
  if (!state.deviceMarker) state.deviceMarker = makeMarker(f.lat, f.lon, "me moving");
  if (state.follow) map.easeTo({ center: [f.lon, f.lat], duration: state.interval, easing: (t) => t });

  $("roCoords").textContent = `${f.lat.toFixed(5)}, ${f.lon.toFixed(5)}`;
  $("roSpeed").textContent = fmtMph(f.speed_mps * MPS_TO_MPH);
  const m = Math.floor(f.eta_s / 60), s = Math.floor(f.eta_s % 60);
  $("roEta").textContent = f.dwell_remaining_s > 0 ? `Stop ${Math.ceil(f.dwell_remaining_s)}s` : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  $("roHdg").textContent = `${Math.round(f.bearing)}°`;
  $("roBar").style.width = `${(f.progress * 100).toFixed(1)}%`;
  setSpoofed(true);
}

function moveLoop() {
  if (!state.moving) { state.rafId = null; return; }
  if (!state.paused && state.prev && state.targetPt && state.deviceMarker) {
    let t = (performance.now() - state.lastFixMs) / state.interval;
    if (t > 1) t = 1;
    const lon = state.prev.lon + (state.targetPt.lon - state.prev.lon) * t;
    const lat = state.prev.lat + (state.targetPt.lat - state.prev.lat) * t;
    state.deviceMarker.setLngLat([lon, lat]);
  }
  state.rafId = requestAnimationFrame(moveLoop);
}

function startMove() {
  if (!state.ready || !state.connected) return attemptEnable();
  const radius = Number($("geofenceRadius").value);
  if (!Number.isFinite(radius) || radius < 0 || radius > 100000) return toast("Geofence radius must be 0–100000 meters.", "err");
  if (state.waypoints.length < 2) return toast("Add at least two waypoints.", "err");
  const cmd = $("followRoads").checked ? "drive" : "fly";
  state.moving = true;
  renderWaypoints();
  state.paused = false; $("pauseBtn").textContent = "Pause"; $("pauseBtn").classList.remove("hidden");
  state.prev = null;
  state.targetPt = null;
  state.lastFixMs = 0;
  if (state.rafId == null) state.rafId = requestAnimationFrame(moveLoop); // single loop only
  $("startBtn").classList.add("hidden");
  $("stopBtn").classList.remove("hidden");
  $("readout").classList.remove("hidden");
  send({
    cmd, profile: state.profile,
    dwell_s: state.waypoints.map(w=>w.dwell_s || 0),
    geofences: Number($("geofenceRadius").value) > 0 ? state.waypoints.map((w,i)=>({name:`Waypoint ${i+1}`,lat:w.lat,lon:w.lon,radius_m:Number($("geofenceRadius").value)})) : [],
    waypoints: state.waypoints.map((w) => [w.lat, w.lon]),
    speed_mps: currentMph() / MPS_TO_MPH,
    round_trip: $("roundTrip").checked,
    realistic: $("realistic").checked,
    jitter: $("realistic").checked ? 3 : 0,
    loop: $("loopRoute").checked,
    // "Stay at end" ON = keep the last fix; OFF = restore real GPS on arrival.
    clear_at_end: !$("stayAtEnd").checked,
    dt: MOVE_DT,
  });
}

function endMove(note) {
  state.pendingAddStop = null;
  state.moving = false;
  $("pauseBtn").classList.add("hidden");
  state.loopOn = false;
  renderWaypoints();
  if (state.deviceMarker && state.targetPt) state.deviceMarker.setLngLat([state.targetPt.lon, state.targetPt.lat]);
  if (state.deviceMarker) state.deviceMarker.getElement().className = "mk me";
  $("startBtn").classList.remove("hidden");
  $("stopBtn").classList.add("hidden");
  $("readout").classList.add("hidden");
  if (note) toast(note, "ok");
  send({ cmd: "status" });
}

// Immediately stop any running route (local UI + tell the engine).
function haltMovement() {
  if (state.moving) send({ cmd: "stop" });
  state.moving = false;
  $("pauseBtn").classList.add("hidden");
  state.loopOn = false;
  renderWaypoints();
  $("startBtn").classList.remove("hidden");
  $("stopBtn").classList.add("hidden");
  $("readout").classList.add("hidden");
}

/* ---------------- search (Nominatim) — places/businesses only ---------------- */

let searchTimer = null;

async function geocode(q) {
  try {
    const r = await apiFetch(`/api/search?q=${encodeURIComponent(q)}`);
    if (!r.ok) throw new Error((await r.json()).error || "Search unavailable. Try again shortly.");
    const results = await r.json();
    const box = $("searchResults");
    box.innerHTML = "";
    if (!results.length) { box.innerHTML = `<div class="sr-empty">No matches. Try a business, address, or paste coordinates.</div>`; box.classList.remove("hidden"); return; }
    results.forEach((res) => {
      const lat = parseFloat(res.lat), lon = parseFloat(res.lon);
      const name = res.display_name.split(",")[0];
      box.appendChild(srItem("◉", name, res.display_name, () => pickPlace(lat, lon, name)));
    });
    box.classList.remove("hidden");
  } catch (e) { toast(e.message || "Search failed. Try again.", "err"); }
}

/* ---------------- saved places, recents & coordinate entry ---------------- */

// Load bookmarks + recents from the engine's config so they survive restarts.
let placesGeneration = 0;
async function loadPlaces() {
  const generation = ++placesGeneration;
  try {
    const response = await apiFetch("/api/places" + (state.deviceId ? "?udid=" + encodeURIComponent(state.deviceId) : ""));
    if (!response.ok) throw Error("Could not load saved places.");
    const p = await response.json();
    if (generation !== placesGeneration) return;
    state.bookmarks = p.bookmarks || [];
    state.recents = p.recents || [];
  } catch (_) {}
}

function hideSearch() { $("searchResults").classList.add("hidden"); }

// A styled result row shared by search hits, saved spots, recents and coords.
function srHead(text) {
  const d = document.createElement("div");
  d.className = "sr-head";
  d.textContent = text;
  return d;
}
function srItem(icon, label, sub, onClick, onDelete) {
  const d = document.createElement("div");
  d.className = "sr-item"; d.tabIndex = 0; d.setAttribute("role", "button");
  d.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); onClick(); } });
  const ic = document.createElement("span"); ic.className = "sr-ic"; ic.textContent = icon;
  const text = document.createElement("span"); text.className = "sr-text";
  const lab = document.createElement("span"); lab.className = "sr-label"; lab.textContent = label;
  text.appendChild(lab);
  if (sub) { const s = document.createElement("span"); s.className = "sr-sub"; s.textContent = sub; text.appendChild(s); }
  d.appendChild(ic); d.appendChild(text);
  d.addEventListener("click", (e) => { if (e.target.closest(".sr-del")) return; onClick(); });
  if (onDelete) {
    const del = document.createElement("button");
    del.className = "sr-del"; del.textContent = "✕"; del.title = "Remove";
    del.addEventListener("click", (e) => { e.stopPropagation(); onDelete(); });
    d.appendChild(del);
  }
  return d;
}

// Fly to a place and either drop the static pin or add a route waypoint.
function pickPlace(lat, lon, name) {
  map.flyTo({ center: [lon, lat], zoom: 14, duration: 900 });
  if (state.mode === "static") setPin(lat, lon, name); else addWaypoint(lat, lon);
  hideSearch();
  $("search").value = "";
  $("search").blur();
}

// Saved + Recent lists, shown when the search box is focused and empty.
function showPlacesDropdown() {
  const box = $("searchResults");
  box.innerHTML = "";
  const { bookmarks, recents } = state;
  if (!bookmarks.length && !recents.length) {
    box.innerHTML = `<div class="sr-empty">Search a place, paste coordinates (e.g. 40.6892, -74.0445), or tap the map. Spots you save appear here.</div>`;
    box.classList.remove("hidden");
    return;
  }
  if (bookmarks.length) {
    box.appendChild(srHead(state.deviceId ? "Saved for this device" : "Legacy saved places · read only"));
    bookmarks.forEach((b, i) => box.appendChild(
      srItem("★", b.name, `${b.lat.toFixed(4)}, ${b.lon.toFixed(4)}`,
        () => pickPlace(b.lat, b.lon, b.name),
        state.deviceId ? () => deleteBookmark(i) : null)
    ));
  }
  if (recents.length) {
    box.appendChild(srHead("Recent"));
    recents.forEach((r) => box.appendChild(
      srItem("↩", r.name || `${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}`,
        r.name ? `${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}` : "",
        () => pickPlace(r.lat, r.lon, r.name))
    ));
  }
  box.classList.remove("hidden");
}

// Permissive "lat, lon" parser — also tolerates a space separator and
// Google Maps "@lat,lon,zoom" URL fragments.
function parseCoords(q) {
  const at = q.match(/@(-?\d{1,3}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)/);
  const m = at || q.match(/^\s*(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$/);
  if (!m) return null;
  const lat = parseFloat(m[1]), lon = parseFloat(m[2]);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
  return { lat, lon };
}

function showCoordResult({ lat, lon }) {
  const box = $("searchResults");
  box.innerHTML = "";
  box.appendChild(srHead("Coordinates"));
  box.appendChild(srItem("⌖", `${lat.toFixed(5)}, ${lon.toFixed(5)}`,
    state.mode === "static" ? "Set as location" : "Add as waypoint",
    () => pickPlace(lat, lon, null)));
  box.classList.remove("hidden");
}

// Save the current spot (pin, else the live virtual location). No prompt() —
// Electron has no window.prompt; the engine auto-names from coords when blank.
async function bookmarkCurrent() {
  if (!state.deviceId) return toast("Connect a device to save places to its profile.", "err");
  const p = state.pin || state.virtual;
  if (!p) return toast("Pick a spot on the map first, then save it.", "err");
  const name = state.pinName || null;
  try {
    const r = await apiFetch("/api/bookmark", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat: p.lat, lon: p.lon, name, udid: state.deviceId }),
    });
    if (!r.ok) throw Error("Saved-place update failed.");
    const data = await r.json();
    state.bookmarks = data.bookmarks || [];
    toast(`Saved ${name || `${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}`}`, "ok");
    if (!$("search").value.trim() && !hidden("searchResults")) showPlacesDropdown();
  } catch (_) { toast("Couldn't save that spot.", "err"); }
}

async function deleteBookmark(i) {
  if (!state.deviceId) return toast("Legacy places are read only. Connect a device to manage its saved places.", "err");
  try {
    const r = await apiFetch("/api/unbookmark", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: i, udid: state.deviceId }),
    });
    if (!r.ok) throw Error("Saved-place update failed.");
    const data = await r.json();
    state.bookmarks = data.bookmarks || [];
    showPlacesDropdown();
  } catch (_) { toast("Could not remove saved place. Try again.", "err"); }
}

// Copy the current coordinates to the clipboard for pasting elsewhere.
function copyCoords() {
  const p = state.pin || state.virtual;
  if (!p) return toast("No spot selected yet — tap the map or search.", "err");
  const text = `${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}`;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => toast(`Copied ${text}`, "ok"), () => toast(text, ""));
  } else {
    toast(text, "");
  }
}

const hidden = (id) => $(id).classList.contains("hidden");

async function previewRoute() {
  if (state.waypoints.length < 2) return toast("Add at least two waypoints.", "err");
  const pts = state.waypoints.map((w) => [w.lat, w.lon]);
  if (!$("followRoads").checked) {
    drawRoute(pts);
    map.fitBounds(bounds(pts), { padding: 120, duration: 700 });
    return;
  }
  const coords = state.waypoints.map((w) => `${w.lat},${w.lon}`).join(";");
  try {
    const r = await apiFetch(`/api/route?coords=${encodeURIComponent(coords)}&profile=${encodeURIComponent(state.profile)}`);
    const data = await r.json();
    if (data.error) { $("straightRoute").classList.remove("hidden"); return toast(data.error, "err"); }
    drawRoute(data.points);
    map.fitBounds(bounds(data.points), { padding: 120, duration: 700 });
    toast(`Route: ${(data.distance_m / 1000).toFixed(2)} km`, "ok");
  } catch (_) { toast("Preview failed.", "err"); }
}

/* ---------------- UI wiring ---------------- */

function wireUI() {
  $("mapView").addEventListener("change", (event) => setSatellite(event.target.value === "satellite"));
  $("showHighways").addEventListener("change", (event) => showMainHighways(event.target.checked));
  $("modeToggle").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (btn) setMode(btn.dataset.mode);
  });

  const searchEl = $("search");
  searchEl.addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    if (!q) return showPlacesDropdown();          // empty → Saved + Recent
    const coord = parseCoords(q);
    if (coord) return showCoordResult(coord);      // "lat, lon" paste
    if (q.length < 3) return hideSearch();
    hideSearch(); // Public search is submitted explicitly; never autocomplete.
  });
  searchEl.addEventListener("focus", () => { if (!searchEl.value.trim()) showPlacesDropdown(); });
  searchEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const q = searchEl.value.trim();
      const coord = parseCoords(q);
      if (coord) return pickPlace(coord.lat, coord.lon, null);
      e.preventDefault(); e.stopPropagation(); if (q.length >= 3) geocode(q);
    } else if (e.key === "ArrowDown") { e.preventDefault(); $("searchResults").querySelector(".sr-item")?.focus();
    } else if (e.key === "Escape") { hideSearch(); searchEl.blur(); }
  });

  $("speedSelect").addEventListener("change", (e) => { applyPreset(e.target.value); pushLiveSpeed(); });
  $("speedMph").addEventListener("input", () => {
    // Sync the slider from the typed value WITHOUT reformatting the field (so typing isn't disrupted).
    const sl = $("speedSlider");
    sl.value = mphToPos(currentMph());
    sl.style.setProperty("--fill", `${(Number(sl.value) / Number(sl.max)) * 100}%`);
    $("speedSelect").value = "custom";
    pushLiveSpeed();
  });
  $("speedSlider").addEventListener("input", (e) => {
    $("speedMph").value = posToMph(Number(e.target.value));
    e.target.style.setProperty("--fill", `${(Number(e.target.value) / Number(e.target.max)) * 100}%`);
    $("speedSelect").value = "custom";
    pushLiveSpeed();
  });
  $("ludicrous").addEventListener("change", () => {
    setMph(currentMph()); // re-clamp + rescale the slider to the new ceiling
    pushLiveSpeed();
    toast($("ludicrous").checked ? "High-speed testing — up to 22,369 mph" : "Normal cap: 1,000 mph", "ok");
  });

  $("locateBtn").addEventListener("click", () => centerOnDevice(true));
  $("gateBtn").addEventListener("click", attemptEnable);
  $("usbBtn").addEventListener("click", () => {
    toast("Approve the admin prompt to switch to a USB tunnel (stops drops)…", "ok");
    send({ cmd: "start_tunnel" });
  });
  $("devicePill").addEventListener("click", () => $("deviceInfo").classList.toggle("hidden"));
  $("setBtn").addEventListener("click", () => {
    if (!state.ready) return attemptEnable();
    if (!state.pin) return toast("Pick a spot on the map first.", "err");
    state.virtualName = state.pinName;
    haltMovement();
    send({ cmd: "set", lat: state.pin.lat, lon: state.pin.lon, name: state.pinName || undefined });
  });
  $("resetBtn").addEventListener("click", () => { haltMovement(); send({ cmd: "clear" }); });
  $("previewBtn").addEventListener("click", previewRoute);
  $("startBtn").addEventListener("click", startMove);
  $("stopBtn").addEventListener("click", haltMovement);
  $("clearWps").addEventListener("click", () => {
    haltMovement();
    state.waypoints = [];
    renderWaypoints();
    if (map.getSource("route")) map.getSource("route").setData(emptyLine());
  });

  $("bmBtn").addEventListener("click", bookmarkCurrent);
  $("pickInfo").addEventListener("click", copyCoords);
  $("spoofStop").addEventListener("click", () => { haltMovement(); send({ cmd: "clear" }); });
  $("toast").addEventListener("click", hideToast);

  // Dismiss the search dropdown / device popover on an outside click.
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search")) hideSearch();
    if (!e.target.closest("#deviceInfo") && !e.target.closest("#devicePill")) $("deviceInfo").classList.add("hidden");
  });

  // Keyboard shortcuts — ignored while typing in a field.
  document.addEventListener("keydown", (e) => {
    const el = document.activeElement;
    const typing = el && /^(input|textarea|select)$/i.test(el.tagName);
    if (((e.metaKey || e.ctrlKey) && (e.key === "f" || e.key === "F")) || (e.key === "/" && !typing)) {
      e.preventDefault();
      const s = $("search"); s.focus(); s.select();
      if (!s.value.trim()) showPlacesDropdown();
      return;
    }
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    switch (e.key) {
      case "Escape": hideSearch(); $("deviceInfo").classList.add("hidden"); break;
      case "Enter":
        if (state.mode === "static") $("setBtn").click();
        else if (!state.moving) $("startBtn").click();
        break;
      case "s": case "S": e.preventDefault(); bookmarkCurrent(); break;
      case "c": case "C": e.preventDefault(); copyCoords(); break;
      case "l": case "L": centerOnDevice(true); break;
      case "m": case "M": setMode(state.mode === "static" ? "route" : "static"); break;
    }
  });
}

let toastTimer = null, toastLeaveTimer = null;
function toast(text, kind) {
  const t = $("toast");
  clearTimeout(toastTimer); clearTimeout(toastLeaveTimer);
  const glyph = kind === "ok" ? "✓" : kind === "err" ? "⚠" : "ℹ";
  t.innerHTML = "";
  const ic = document.createElement("span"); ic.className = "toast-ic"; ic.textContent = glyph;
  const msg = document.createElement("span"); msg.className = "toast-msg"; msg.textContent = text;
  t.appendChild(ic); t.appendChild(msg);
  t.className = "toast " + (kind || "");
  if (kind === "err") { $("errorText").textContent = text; $("errorPanel").classList.remove("hidden"); }
  toastTimer = setTimeout(hideToast, 3600);
}
function hideToast() {
  const t = $("toast");
  clearTimeout(toastTimer); clearTimeout(toastLeaveTimer);
  if (t.classList.contains("hidden")) return;
  t.classList.add("leaving");
  toastLeaveTimer = setTimeout(() => { t.classList.add("hidden"); t.classList.remove("leaving"); }, 220);
}

// Debug hook (used for automated verification).
window.PHANTOM = {
  state,
  get map() { return map; },
  setMode, addWaypoint, applyPreset, setMph, currentMph, startMove, showDevice, centerOnDevice,
  setPin, setSpoofed, loadPlaces, showPlacesDropdown, bookmarkCurrent, copyCoords, toast,
};

boot();

function updateControls() {
  for (const id of ["setBtn", "resetBtn", "spoofStop", "startBtn", "stopBtn", "pauseBtn"]) $(id).disabled = !state.connected || !state.ready;
  for (const id of ["usbBtn", "gateBtn"]) $(id).disabled = !state.connected;
  if (state.libraryDevice !== state.deviceId) { state.libraryDevice = state.deviceId; loadLibrary(); loadPlaces(); }
}
function downloadFile(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], {type}));
  const a = document.createElement("a"); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function routeSettings() {
  return {geofenceRadius:Number($("geofenceRadius").value)||0, profile:state.profile, mph:currentMph(), preset:$("speedSelect").value,
    ...Object.fromEntries(["followRoads","roundTrip","realistic","loopRoute","stayAtEnd","ludicrous"].map(id=>[id,$(id).checked]))};
}
let library = [];
let libraryGeneration = 0;
let libraryBusy = false;
async function loadLibrary() {
  const generation = ++libraryGeneration;
  libraryBusy = true; $("saveRoute").disabled = true; library = []; renderLibrary();
  try {
    const r = await apiFetch('/api/library?udid='+encodeURIComponent(state.deviceId || ''));
    if (!r.ok) throw Error('Unable to load saved routes');
    const data = await r.json(); if (generation !== libraryGeneration) return;
    library = data.routes || []; libraryBusy = false; $("saveRoute").disabled = false; renderLibrary();
  } catch(e) { if (generation === libraryGeneration) toast(e.message,"err"); }
}
async function persistLibrary(next) {
  if (libraryBusy) throw Error('Wait for the saved route library to finish loading or saving.');
  const deviceId = state.deviceId || '', generation = libraryGeneration;
  libraryBusy = true; $("saveRoute").disabled = true;
  try {
    const r = await apiFetch('/api/library?udid='+encodeURIComponent(deviceId), {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({routes:next})});
    if (!r.ok) throw Error('Unable to save routes. Your existing library is unchanged.');
    if (generation !== libraryGeneration || deviceId !== (state.deviceId || '')) return;
    library=next; renderLibrary();
  } finally {
    if (generation === libraryGeneration && deviceId === (state.deviceId || '')) {libraryBusy = false; $("saveRoute").disabled = false;}
  }
}
function loadRoute(r) {
  if (state.moving) return toast('Stop movement before loading another route.','err');
  state.waypoints=r.waypoints.map(w=>({...w})); const s=r.settings || {};
  for(const id of ['followRoads','roundTrip','realistic','loopRoute','stayAtEnd','ludicrous']) if(typeof s[id]==='boolean') $(id).checked=s[id];
  $("geofenceRadius").value=s.geofenceRadius || 0; state.profile=s.profile || 'driving'; setMph(s.mph || 37); $('speedSelect').value=s.preset || 'custom';
  $('routeName').value=r.name; $('routeFolder').value=r.folder || ''; setMode('route'); renderWaypoints(); drawRoute(state.waypoints.map(w=>[w.lat,w.lon]));
}
function renderLibrary() {
  const box=$('routeLibrary'); box.replaceChildren();
  for (const r of library) {
    const row=document.createElement('div'); row.className='library-row'; const label=document.createElement('span'); label.textContent=(r.folder ? r.folder+' / ' : '')+r.name; row.append(label);
    const actions=document.createElement('div'); actions.className='library-actions'; row.append(actions);
    for (const [title, action] of [['Load',()=>loadRoute(r)],['Duplicate',()=>persistLibrary([...library,{...r,id:crypto.randomUUID(),name:r.name+' copy'}])],['Delete',()=>persistLibrary(library.filter(x=>x.id!==r.id))]]) {
      const b=document.createElement('button'); b.className='btn tinted'; b.textContent=title; b.onclick=()=>Promise.resolve(action()).catch(e=>toast(e.message,'err')); actions.append(b);
    } box.append(row);
  }
}
function wireExtras() {
  $('pauseBtn').onclick=()=>send({cmd:state.paused?'resume':'pause'});
  $('autoResume').onchange=()=>{ if(!send({cmd:'preferences',udid:state.deviceId,auto_resume:$('autoResume').checked})) $('autoResume').checked=!$('autoResume').checked; };
  $('dismissError').onclick=()=>$('errorPanel').classList.add('hidden');
  $('straightRoute').onclick=()=>{ $('followRoads').checked=false; $('straightRoute').classList.add('hidden'); $('errorPanel').classList.add('hidden'); previewRoute(); toast('Straight-line route selected. Review it, then press Start.',''); };
  $('exportLog').onclick=async()=>{try {const r=await apiFetch('/api/session/log'); if(!r.ok) throw Error('Unable to retrieve log'); downloadFile('phantom-session.json',JSON.stringify(await r.json(),null,2),'application/json');}catch(e){toast(e.message,'err');}};
  $('saveRoute').onclick=async()=>{try {if(state.waypoints.length<2) throw Error('Add at least two waypoints before saving.'); const name=$('routeName').value.trim(); if(!name) throw Error('Give your route a name.'); await persistLibrary([...library,{id:crypto.randomUUID(),name,folder:$('routeFolder').value.trim(),waypoints:state.waypoints.map(w=>({...w})),settings:routeSettings()}]);toast('Route and scenario settings saved.','ok');}catch(e){toast(e.message,'err');}};
  $('exportGpx').onclick=()=>{if(state.waypoints.length<2)return toast('Add at least two waypoints.','err');downloadFile('phantom-route.gpx','<?xml version="1.0"?><gpx version="1.1" creator="Phantom" xmlns="http://www.topografix.com/GPX/1/1"><rte>'+state.waypoints.map(w=>`<rtept lat="${w.lat}" lon="${w.lon}"/>`).join('')+'</rte></gpx>','application/gpx+xml');};
  $('importGpx').onchange=async(e)=>{try {if(state.moving)throw Error('Stop movement before importing.');const file=e.target.files[0];if(!file)return;if(file.size>2000000)throw Error('GPX must be under 2 MB.');const doc=new DOMParser().parseFromString(await file.text(),'application/xml');if(doc.querySelector('parsererror'))throw Error('Invalid GPX XML.');let nodes=[...doc.getElementsByTagNameNS('*','rtept')];if(!nodes.length)nodes=[...doc.getElementsByTagNameNS('*','trkpt')];if(!nodes.length)nodes=[...doc.getElementsByTagNameNS('*','wpt')];if(nodes.length<2 || nodes.length>MAX_WAYPOINTS)throw Error('Import a route with 2–50 points.');if(nodes.some(n=>!n.hasAttribute("lat")||!n.hasAttribute("lon")))throw Error("GPX point is missing coordinates.");const pts=nodes.map(n=>({lat:Number(n.getAttribute('lat')),lon:Number(n.getAttribute('lon'))}));if(pts.some(w=>!Number.isFinite(w.lat)||!Number.isFinite(w.lon)||Math.abs(w.lat)>90||Math.abs(w.lon)>180))throw Error('GPX contains invalid coordinates.');state.waypoints=pts;setMode('route');renderWaypoints();drawRoute(pts.map(w=>[w.lat,w.lon]));toast('GPX imported. Review the route before starting.','ok');}catch(e){toast(e.message,'err');}finally{e.target.value='';}};
  setInterval(()=>{const s=Math.floor((Date.now()-state.startedAt)/1000);$('sessionElapsed').textContent=`Session: ${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;},1000);
  loadLibrary(); updateControls();
}
