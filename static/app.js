const postcodeInput = document.querySelector("#postcode");
const storeInput = document.querySelector("#store-name");
const itemInput = document.querySelector("#item-name");
const listNode = document.querySelector("#list");
const countNode = document.querySelector("#count");

const map = L.map("map", { scrollWheelZoom: true }).setView([54.6, -6.7], 8);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);

const markers = L.layerGroup().addTo(map);
let latest = [];
let lastPayload = null;
let userMoved = false;
let plotted = 0;
let lastFilter = "";
let lastResults = "";

map.on("dragstart", () => {
  userMoved = true;
});
map.on("zoomstart", (event) => {
  if (event.originalEvent) userMoved = true;
});

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[character]));
}

function money(pence) {
  if (pence == null) return "";
  return `£${(pence / 100).toFixed(2)}`;
}

function dishLine(item) {
  const price = money(item.pricePence);
  return price ? `${item.name} · ${price}` : item.name;
}

function dishLabel(store) {
  const names = store.items.map((item) => item.name);
  const text = names.join(", ");
  return text.length > 70 ? `${text.slice(0, 67)}…` : text;
}

function outwardCode(value) {
  const match = String(value || "").toUpperCase().match(/BT\d{1,2}/);
  return match ? match[0] : "";
}

function postcodeMatches(store, query) {
  const wanted = query.trim().toUpperCase().replace(/\s+/g, "");
  if (!wanted) return true;
  const values = [store.postalCode, store.address];
  if (/^BT\d{1,2}$/.test(wanted)) {
    return values.some((value) => outwardCode(value) === wanted);
  }
  return values.some((value) => String(value || "").toUpperCase().replace(/\s+/g, "").includes(wanted));
}

function contains(value, query) {
  const wanted = query.trim().toLowerCase();
  if (!wanted) return true;
  return String(value || "").toLowerCase().includes(wanted);
}

function matchesFilter(store) {
  if (!postcodeMatches(store, postcodeInput.value)) return false;
  if (!contains(store.name, storeInput.value)) return false;
  const itemQuery = itemInput.value.trim().toLowerCase();
  if (!itemQuery) return true;
  return store.items.some((item) => item.name.toLowerCase().includes(itemQuery));
}

function render(data) {
  lastPayload = data;
  latest = data.results || [];
  const visible = latest.filter((store) => matchesFilter(store));
  const located = visible.filter((store) => store.located && store.lat != null && store.lng != null);
  const filterKey = [postcodeInput.value, storeInput.value, itemInput.value].join("\n");
  const filterChanged = filterKey !== lastFilter;
  lastFilter = filterKey;
  if (filterChanged) userMoved = false;

  countNode.textContent = latest.length ? `${visible.length} shown` : "";
  listNode.replaceChildren();
  if (!visible.length && filterKey.trim()) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "No stores match.";
    listNode.append(empty);
  }
  for (const store of visible) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "store";
    const name = document.createElement("strong");
    name.textContent = store.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = store.address || "";
    const dishes = document.createElement("span");
    dishes.className = "dishes";
    dishes.textContent = store.items.map(dishLine).join("  ·  ");
    button.append(name, meta, dishes);
    button.addEventListener("click", () => focusStore(store));
    item.append(button);
    listNode.append(item);
  }

  markers.eachLayer((layer) => {
    const tooltip = layer.getTooltip();
    if (tooltip) {
      layer.closeTooltip();
      if (tooltip._map) tooltip.remove();
      layer.unbindTooltip();
    }
    if (layer.getPopup()) layer.unbindPopup();
  });
  markers.clearLayers();
  document.querySelectorAll("#map .leaflet-tooltip").forEach((node) => node.remove());
  const bounds = [];
  for (const store of located) {
    const marker = L.circleMarker([store.lat, store.lng], {
      radius: 8,
      color: "#f6d36b",
      weight: 3,
      fillColor: "#c81e1e",
      fillOpacity: 0.95,
    });
    marker.bindTooltip(escapeHtml(dishLabel(store)), {
      permanent: false,
      direction: "top",
      offset: [0, -8],
      className: "dish-label",
      opacity: 1,
    });
    const items = store.items.map((entry) => `<li>${escapeHtml(dishLine(entry))}</li>`).join("");
    const link = store.url
      ? `<a href="${escapeHtml(store.url)}" target="_blank" rel="noreferrer">${escapeHtml(store.name)}</a>`
      : escapeHtml(store.name);
    marker.bindPopup(
      `<p class="popup-name">${link}</p>` +
      (store.address ? `<p class="popup-address">${escapeHtml(store.address)}</p>` : "") +
      `<ul class="popup-dishes">${items}</ul>`,
      { className: "chip-popup" }
    );
    marker.addTo(markers);
    bounds.push([store.lat, store.lng]);
  }

  if (bounds.length && !userMoved && (filterChanged || bounds.length !== plotted)) {
    map.fitBounds(bounds, { padding: [48, 48], maxZoom: 12 });
    plotted = bounds.length;
  }
}

function focusStore(store) {
  if (store.lat == null || store.lng == null) return;
  userMoved = true;
  map.setView([store.lat, store.lng], Math.max(map.getZoom(), 14));
  markers.eachLayer((layer) => {
    const point = layer.getLatLng();
    if (Math.abs(point.lat - store.lat) < 0.00001 && Math.abs(point.lng - store.lng) < 0.00001) {
      layer.openPopup();
    }
  });
}

async function refreshResults() {
  const response = await fetch("results.json", { cache: "no-store" });
  const payload = await response.json();
  const results = Array.isArray(payload) ? payload : (payload.results || []);
  const signature = JSON.stringify(results);
  if (signature === lastResults && lastPayload) return;
  lastResults = signature;
  render({ results });
}

for (const input of [postcodeInput, storeInput, itemInput]) {
  input.addEventListener("input", () => {
    if (lastPayload) render(lastPayload);
  });
}

refreshResults().catch(() => {});

const lightRow = document.querySelector(".lights");
const lightColours = ["red", "gold", "green"];
const lightTimers = new Set();
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function lightLater(fn, delay) {
  const timer = setTimeout(() => {
    lightTimers.delete(timer);
    fn();
  }, delay);
  lightTimers.add(timer);
}

function clearLightTimers() {
  for (const timer of lightTimers) clearTimeout(timer);
  lightTimers.clear();
}

function flickerBulb(bulb) {
  const blink = () => {
    bulb.classList.add("off");
    const dark = 70 + Math.random() * 220;
    lightLater(() => {
      bulb.classList.remove("off");
      if (Math.random() < 0.35) {
        lightLater(blink, 60 + Math.random() * 140);
        return;
      }
      lightLater(blink, 250 + Math.random() * 2200);
    }, dark);
  };
  lightLater(blink, Math.random() * 1800);
}

function buildLights() {
  if (!lightRow) return;
  clearLightTimers();
  const count = Math.max(8, Math.ceil(lightRow.clientWidth / 18));
  lightRow.replaceChildren();
  for (let index = 0; index < count; index += 1) {
    const bulb = document.createElement("span");
    bulb.className = lightColours[index % lightColours.length];
    lightRow.append(bulb);
    if (!reduceMotion) flickerBulb(bulb);
  }
}

buildLights();
window.addEventListener("resize", buildLights);
