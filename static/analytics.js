const lede = document.querySelector("#lede");

function money(pence) {
  return `£${(pence / 100).toFixed(2)}`;
}

function outwardCode(value) {
  const match = String(value || "").toUpperCase().match(/BT\d{1,2}/);
  return match ? match[0] : "";
}

function mean(values) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function dishesFrom(stores) {
  const dishes = [];
  for (const store of stores) {
    const postcode = outwardCode(store.postalCode) || outwardCode(store.address);
    for (const item of store.items || []) {
      if (typeof item.pricePence !== "number") continue;
      dishes.push({
        name: item.name,
        price: item.pricePence,
        postcode,
        shop: store.name,
      });
    }
  }
  return dishes;
}

function hasEmoji(name) {
  return /\p{Extended_Pictographic}/u.test(name);
}

function groupAverage(dishes, labelOf) {
  const groups = new Map();
  for (const dish of dishes) {
    const label = labelOf(dish);
    if (!label) continue;
    const bucket = groups.get(label) || [];
    bucket.push(dish.price);
    groups.set(label, bucket);
  }
  return [...groups.entries()].map(([label, prices]) => ({
    label,
    count: prices.length,
    avg: mean(prices),
  }));
}

function fillBars(node, rows, cheap) {
  node.replaceChildren();
  if (!rows.length) {
    node.textContent = "Nothing to rank yet.";
    return;
  }
  const extreme = cheap
    ? Math.min(...rows.map((row) => row.value))
    : Math.max(...rows.map((row) => row.value));
  for (const row of rows) {
    const width = extreme === 0 ? 0 : cheap ? (extreme / row.value) * 100 : (row.value / extreme) * 100;
    const line = document.createElement("div");
    line.className = "bar-row";
    const label = document.createElement("span");
    label.className = "bar-label";
    label.textContent = row.label;
    const track = document.createElement("div");
    track.className = "bar-track";
    const fill = document.createElement("div");
    fill.className = cheap ? "bar-fill cheap" : "bar-fill";
    fill.style.width = `${Math.max(8, width)}%`;
    track.append(fill);
    const note = document.createElement("span");
    note.className = "bar-note";
    note.textContent = row.note;
    line.append(label, track, note);
    node.append(line);
  }
}

function renderSpread(dishes) {
  const prices = dishes.map((dish) => dish.price);
  const aside = document.querySelector("#spread-aside");
  const hist = document.querySelector("#spread");
  if (!prices.length) {
    aside.textContent = "No prices in the file yet.";
    return;
  }
  const low = Math.floor(Math.min(...prices) / 100);
  const high = Math.floor(Math.max(...prices) / 100);
  const buckets = [];
  for (let pound = low; pound <= high; pound += 1) {
    buckets.push({ pound, count: prices.filter((price) => Math.floor(price / 100) === pound).length });
  }
  const tallest = Math.max(...buckets.map((bucket) => bucket.count));
  aside.textContent = `${dishes.length} dishes, from ${money(Math.min(...prices))} to ${money(Math.max(...prices))}. Average ${money(mean(prices))}.`;
  hist.replaceChildren();
  for (const bucket of buckets) {
    const column = document.createElement("div");
    column.className = "hist-col";
    const count = document.createElement("span");
    count.className = "hist-count";
    count.textContent = bucket.count ? String(bucket.count) : "";
    const bar = document.createElement("div");
    bar.className = "hist-bar";
    bar.style.height = `${Math.max(4, (bucket.count / tallest) * 130)}px`;
    const label = document.createElement("span");
    label.className = "hist-label";
    label.textContent = `£${bucket.pound}`;
    column.append(count, bar, label);
    hist.append(column);
  }
}

function renderPostcodes(dishes) {
  const grouped = new Map();
  for (const dish of dishes) {
    if (!dish.postcode) continue;
    const bucket = grouped.get(dish.postcode) || [];
    bucket.push(dish.price);
    grouped.set(dish.postcode, bucket);
  }
  const ranked = [...grouped.entries()]
    .filter(([, prices]) => prices.length >= 2)
    .map(([postcode, prices]) => ({
      label: postcode,
      value: mean(prices),
      note: `${money(mean(prices))} · ${prices.length} dishes`,
    }));
  const byPrice = (left, right) => right.value - left.value || Number(left.label.slice(2)) - Number(right.label.slice(2));
  fillBars(document.querySelector("#dearest"), [...ranked].sort(byPrice).slice(0, 5), false);
  fillBars(
    document.querySelector("#cheapest"),
    [...ranked].sort((left, right) => left.value - right.value || Number(left.label.slice(2)) - Number(right.label.slice(2))).slice(0, 5),
    true,
  );
}

function renderGroups(node, aside, rows, sentence) {
  const ordered = [...rows].sort((left, right) => left.avg - right.avg);
  aside.textContent = sentence(ordered);
  fillBars(
    node,
    ordered.map((row) => ({
      label: row.label,
      value: row.avg,
      note: `${money(row.avg)} · ${row.count}`,
    })),
    false,
  );
}

function renderCommitment(stores) {
  const counts = new Map();
  for (const store of stores) {
    const count = (store.items || []).length;
    counts.set(count, (counts.get(count) || 0) + 1);
  }
  const rows = [...counts.entries()]
    .sort((left, right) => left[0] - right[0])
    .map(([count, shops]) => ({
      label: String(count),
      value: shops,
      note: `${shops} shop${shops === 1 ? "" : "s"}`,
    }));
  const lonely = counts.get(1) || 0;
  document.querySelector("#commit-aside").textContent = `${lonely} of ${stores.length} shops listed one festive dish and went home.`;
  fillBars(document.querySelector("#commitment"), rows, false);
}

function renderEssay(dishes) {
  const quote = document.querySelector("#essay");
  if (!dishes.length) {
    quote.textContent = "No dishes yet.";
    return;
  }
  const longest = dishes.reduce((best, dish) => (dish.name.length > best.name.length ? dish : best));
  quote.textContent = longest.name;
  const detail = document.createElement("span");
  detail.textContent = `${longest.shop} · ${money(longest.price)}`;
  quote.append(detail);
}

function render(stores) {
  const dishes = dishesFrom(stores);
  lede.textContent = `${stores.length} shops in the current data. The charts use dish prices, and the postcode lists use the shop's own address.`;
  renderSpread(dishes);
  renderPostcodes(dishes);

  const named = groupAverage(dishes, (dish) => {
    const name = dish.name.toLowerCase().replace(/['’\-]/g, "");
    if (name.includes("festive")) return "Festive";
    if (name.includes("xmas")) return "Xmas";
    if (name.includes("christmas")) return "Christmas";
    return "";
  });
  renderGroups(
    document.querySelector("#naming"),
    document.querySelector("#name-aside"),
    named,
    (rows) => {
      if (rows.length < 2) return "Average price by the season word in the dish name.";
      const cheap = rows[0];
      const dear = rows[rows.length - 1];
      return `${cheap.label} is the cheap spelling at ${money(cheap.avg)}. ${dear.label} is the dear one at ${money(dear.avg)}.`;
    },
  );

  const emoji = groupAverage(dishes, (dish) => (hasEmoji(dish.name) ? "Emoji" : "Plain"));
  renderGroups(
    document.querySelector("#emoji"),
    document.querySelector("#emoji-aside"),
    emoji,
    (rows) => {
      const fancy = rows.find((row) => row.label === "Emoji");
      const plain = rows.find((row) => row.label === "Plain");
      if (!fancy || !plain) return "No dish in this file has drawn a picture in its name.";
      const gap = fancy.avg - plain.avg;
      return `${fancy.count} dishes put a picture in the name and charge ${money(Math.abs(gap))} ${gap >= 0 ? "more" : "less"}.`;
    },
  );

  const large = groupAverage(dishes, (dish) => (/\blarge\b/i.test(dish.name) ? "Large" : "The Rest"));
  renderGroups(
    document.querySelector("#large"),
    document.querySelector("#large-aside"),
    large,
    (rows) => {
      const big = rows.find((row) => row.label === "Large");
      const rest = rows.find((row) => row.label === "The Rest");
      if (!big || !rest) return "No dish currently says large.";
      return big.avg <= rest.avg
        ? `The ${big.count} dishes that say large average ${money(rest.avg - big.avg)} less than the rest.`
        : `The ${big.count} dishes that say large average ${money(big.avg - rest.avg)} more than the rest.`;
    },
  );

  const debate = groupAverage(dishes, (dish) => {
    const name = dish.name.toLowerCase();
    if (name.includes("fries") || name.includes("fry")) return "Fries";
    if (name.includes("chip")) return "Chip";
    return "";
  });
  renderGroups(
    document.querySelector("#debate"),
    document.querySelector("#debate-aside"),
    debate,
    (rows) => {
      if (rows.length < 2) return "This file is mostly one kind of potato.";
      const gap = Math.abs(rows[0].avg - rows[1].avg);
      return `The average gap is ${money(gap)}. The argument can continue, the bill barely changes.`;
    },
  );

  renderCommitment(stores);
  renderEssay(dishes);
}

fetch("results.json", { cache: "no-store" })
  .then((response) => response.json())
  .then((payload) => {
    const stores = Array.isArray(payload) ? payload : (payload.results || []);
    render(stores);
  })
  .catch(() => {
    lede.textContent = "The results file did not load.";
  });

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
