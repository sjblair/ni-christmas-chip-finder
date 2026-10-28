#!/usr/bin/env python3
"""Search Just Eat for festive chips, fries, and roasties and write the static site data.

Run: python3 scrape.py

The site is the files in static/. results.json there is replaced atomically.
A failed postcode keeps its previous stores and its previous lastSuccessfulRun.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
RESULTS_PATH = STATIC / "results.json"
MANIFEST_PATH = DATA / "manifest.json"
HITS_DIR = DATA / "hits"
RUN_STATUS_PATH = DATA / "run-status.json"

DEFAULT_TERMS = [
    "christmas chip",
    "christmas chips",
    "christmas fries",
    "christmas roasties",
    "christmas roast potatoes",
    "xmas chip",
    "xmas chips",
    "xmas fries",
    "xmas roasties",
    "xmas roast potatoes",
    "festive chip",
    "festive chips",
    "festive fries",
    "festive roasties",
    "festive roast potatoes",
    "stuffing chips",
    "stuffing fries",
    "stuffing roasties",
    "turkey chips",
    "turkey fries",
    "turkey roasties",
    "pigs in blankets chips",
    "pigs in blankets fries",
    "pigs in blankets roasties",
    "cranberry chips",
    "cranberry fries",
    "cranberry roasties",
]

# Words that can sit between the phrase and still mean the same dish,
# e.g. "Christmas Loaded Chip" for the phrase "christmas chip".
MODIFIERS = {
    "the",
    "jumbo",
    "regular",
    "large",
    "mega",
    "half",
    "portion",
    "loaded",
    "curry",
    "cheese",
    "gravy",
    "sauce",
    "spicy",
    "seasoned",
    "small",
    "kids",
    "kid",
    "style",
    "special",
    "dinner",
    "garlic",
    "with",
    "and",
}

SEASONS = {"christmas", "xmas", "festive"}
TOPPINGS = {"stuffing", "turkey", "cranberry"}
POTATOES = {"chip", "fry", "roastie"}

JE_HEADERS = {
    "Accept": "application/json",
    "Accept-Tenant": "uk",
}
FIREFOX_PROFILE = Path("/tmp/je-ff-finder")
FIREFOX_PORT = 9224
FIREFOX_START = "https://www.just-eat.co.uk/area/bt1-belfast"

# One Just Eat request at a time. Faster bursts get the IP blocked for about an hour.
REQUESTS_PER_SECOND = 0.4
# First retry waits 5 minutes, then 10, 20, and 40. Four retries, then the postcode is failed.
MAX_RETRIES = 4
INITIAL_RETRY_SECONDS = 5 * 60

je_limiter = None


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_at = 0.0

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            wait = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if wait:
            time.sleep(wait)


def tokenize(text: str) -> list[str]:
    cleaned = text.lower().replace("’", "'").replace("‘", "'").replace("'", "")
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    tokens = []
    for token in cleaned.split():
        if token == "chips":
            token = "chip"
        elif token == "fries":
            token = "fry"
        elif token == "roasties":
            token = "roastie"
        elif token == "potatoes":
            token = "potato"
        elif token == "roasted":
            token = "roast"
        elif token == "blankets":
            token = "blanket"
        tokens.append(token)
    merged = []
    for token in tokens:
        if token == "mas" and merged and merged[-1] == "x":
            merged[-1] = "xmas"
            continue
        merged.append(token)
    return merged


def _marker_spans(tokens: list[str]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    festive = []
    potatoes = []
    for index, token in enumerate(tokens):
        if token in SEASONS or token in TOPPINGS:
            festive.append((index, index + 1))
        if token in POTATOES:
            potatoes.append((index, index + 1))
        if tokens[index:index + 3] == ["pigs", "in", "blanket"]:
            festive.append((index, index + 3))
        if tokens[index:index + 2] == ["roast", "potato"]:
            potatoes.append((index, index + 2))
    return festive, potatoes


def matches_dish(name: str) -> bool:
    """Season or festive topping, plus chips, fries, roasties, or roast potatoes.

    The two parts can come in either order. Only modifier words may sit between them.
    A Christmas dinner or festive feast with no potato word does not match.
    """
    tokens = tokenize(name)
    festive, potatoes = _marker_spans(tokens)
    for start, end in festive:
        for potato_start, potato_end in potatoes:
            if end <= potato_start and all(token in MODIFIERS for token in tokens[end:potato_start]):
                return True
            if potato_end <= start and all(token in MODIFIERS for token in tokens[potato_end:start]):
                return True
    return False


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "term"


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"{stamp} {message}", flush=True)


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2))
    temporary.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text())


def wait_with_status(seconds: int, label: str) -> None:
    deadline = time.monotonic() + seconds
    while True:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            return
        if remaining >= 90:
            amount = f"about {max(1, round(remaining / 60))} min"
        else:
            amount = f"{remaining} seconds"
        log(f"retrying {label} in {amount}")
        time.sleep(min(15, remaining))


def retry_pause(attempt: int) -> int:
    """Seconds to wait before retry number attempt (0 is the first retry)."""
    return INITIAL_RETRY_SECONDS * (2 ** attempt)


def firefox_binary() -> str:
    mac = Path("/Applications/Firefox.app/Contents/MacOS/firefox")
    if mac.exists():
        return str(mac)
    found = shutil.which("firefox")
    if found:
        return found
    raise RuntimeError("Firefox is not installed.")


class FirefoxHttpError(Exception):
    def __init__(self, code: int):
        super().__init__(f"HTTP {code}")
        self.code = code


def remote_value(node: dict):
    kind = node.get("type")
    if kind in ("string", "number", "boolean"):
        return node.get("value")
    if kind in ("null", "undefined"):
        return None
    if kind == "array":
        return [remote_value(item) for item in node.get("value") or []]
    if kind == "object":
        return {pair[0]: remote_value(pair[1]) for pair in node.get("value") or []}
    raise RuntimeError(f"Unexpected Firefox value: {kind}")


class FirefoxClient:
    """Headless Firefox. Just Eat answers this browser and refuses the direct API."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sock: socket.socket | None = None
        self.buf = b""
        self.next_id = 0
        self.context = None
        self.process: subprocess.Popen | None = None

    def fetch_json(self, url: str, timeout: float):
        with self.lock:
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    self.ensure()
                    return self._fetch(url, timeout)
                except (ConnectionError, TimeoutError, OSError) as error:
                    last_error = error
                    self.close()
                except RuntimeError as error:
                    last_error = error
                    if "NetworkError" not in str(error) or attempt == 1:
                        raise
                    self.quit_firefox()
            raise last_error or ConnectionError("Firefox connection failed")

    def ensure(self) -> None:
        if self.sock and self.context:
            return
        self.quit_firefox()
        self.launch()
        self.connect()
        self.call(
            "session.new",
            {"capabilities": {"alwaysMatch": {"acceptInsecureCerts": True}}},
            timeout=20,
        )
        self.wait_for_site()

    def port_open(self) -> bool:
        try:
            probe = socket.create_connection(("127.0.0.1", FIREFOX_PORT), timeout=0.4)
        except OSError:
            return False
        probe.close()
        return True

    def launch(self) -> None:
        binary = firefox_binary()
        FIREFOX_PROFILE.mkdir(parents=True, exist_ok=True)
        prefs = FIREFOX_PROFILE / "user.js"
        if not prefs.exists():
            prefs.write_text(
                "\n".join(
                    [
                        'user_pref("browser.shell.checkDefaultBrowser", false);',
                        'user_pref("browser.aboutwelcome.enabled", false);',
                        'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
                        'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);',
                    ]
                )
            )
        log("opening Firefox to search Just Eat")
        self.process = subprocess.Popen(
            [
                binary,
                "--headless",
                "--profile",
                str(FIREFOX_PROFILE),
                "--no-remote",
                f"--remote-debugging-port={FIREFOX_PORT}",
                FIREFOX_START,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.port_open():
                return
            if self.process.poll() is not None:
                raise RuntimeError("Firefox exited before the debugging port opened.")
            time.sleep(0.2)
        raise TimeoutError("Firefox debugging port did not open.")

    def quit_firefox(self) -> None:
        self.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        subprocess.run(
            ["pkill", "-f", "je-ff-finder"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and (self.port_open() or self.profile_running()):
            time.sleep(0.2)
        time.sleep(0.4)

    def profile_running(self) -> bool:
        result = subprocess.run(
            ["pgrep", "-f", "MacOS/firefox --profile /tmp/je-ff-finder"],
            check=False,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())

    def connect(self) -> None:
        sock = socket.create_connection(("127.0.0.1", FIREFOX_PORT), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET /session HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{FIREFOX_PORT}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Firefox closed the debugging handshake.")
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.decode(errors="replace")[:200])
        sock.settimeout(1)
        self.sock = sock
        self.buf = rest
        self.context = None

    def wait_for_site(self) -> None:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                contexts = self.contexts()
            except (ConnectionError, TimeoutError, OSError, RuntimeError):
                time.sleep(0.5)
                continue
            chosen = next(
                (item for item in contexts if "just-eat.co.uk" in (item.get("url") or "")),
                None,
            )
            if chosen is None:
                if contexts:
                    try:
                        self.call(
                            "browsingContext.navigate",
                            {
                                "context": contexts[0]["context"],
                                "url": FIREFOX_START,
                                "wait": "complete",
                            },
                            timeout=40,
                        )
                    except (ConnectionError, TimeoutError, OSError, RuntimeError):
                        pass
                time.sleep(0.5)
                continue
            self.context = chosen["context"]
            try:
                title = self.evaluate("document.title || ''", timeout=10)
            except (ConnectionError, TimeoutError, OSError, RuntimeError):
                time.sleep(0.5)
                continue
            if isinstance(title, str) and "Just Eat" in title and "moment" not in title.lower():
                return
            time.sleep(0.5)
        raise TimeoutError("Just Eat did not finish loading in Firefox.")

    def contexts(self) -> list[dict]:
        tree = self.call("browsingContext.getTree", {}, timeout=15)
        found = []

        def walk(nodes):
            for node in nodes or []:
                found.append(node)
                walk(node.get("children") or [])

        walk(tree["result"]["contexts"])
        return found

    def area_json(self, code: str, url: str, timeout: float = 40):
        """Read a Just Eat JSON URL from the open area search page."""
        with self.lock:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    self._open_area(code, force=attempt > 0)
                    value = self.evaluate(self._area_expression(url), timeout=timeout)
                except (ConnectionError, TimeoutError, OSError, RuntimeError) as error:
                    last_error = error
                    self.close()
                    time.sleep(1)
                    continue
                status = int(value["status"])
                if status == 404:
                    return None
                if status != 200:
                    last_error = FirefoxHttpError(status)
                    time.sleep(1)
                    continue
                return json.loads(value["body"])
            raise last_error or RuntimeError(f"Could not read {code} from the search page")

    def _open_area(self, code: str, force: bool = False) -> None:
        self.ensure()
        slug_code = code.lower()
        if not force:
            try:
                href = self.evaluate("location.href || ''", timeout=8)
            except (ConnectionError, TimeoutError, OSError, RuntimeError):
                href = ""
            if f"/area/{slug_code}?q=" in str(href).lower():
                return
        target = f"https://www.just-eat.co.uk/area/{slug_code}?q=christmas+chip"
        log(f"opening {target}")
        self.call(
            "browsingContext.navigate",
            {"context": self.context, "url": target, "wait": "complete"},
            timeout=40,
        )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            chosen = next(
                (
                    item
                    for item in self.contexts()
                    if f"/area/{slug_code}" in (item.get("url") or "")
                ),
                None,
            )
            if chosen:
                self.context = chosen["context"]
                title = self.evaluate("document.title || ''", timeout=10)
                if isinstance(title, str) and "Just Eat" in title and "moment" not in title.lower():
                    time.sleep(1)
                    return
            time.sleep(0.4)
        raise TimeoutError(f"{code} search page did not finish loading.")

    def _area_expression(self, url: str) -> str:
        search = "/search/restaurants/" in url
        return (
            "(async () => {"
            f" const response = await fetch({json.dumps(url)}, {{credentials: 'include'}});"
            " const text = await response.text();"
            " if (!response.ok) return {status: response.status, body: text.slice(0, 180)};"
            " const data = JSON.parse(text);"
            " const rows = data.restaurants || [];"
            f" const search = {str(search).lower()};"
            " const body = search ? rows.map(row => ({"
            " restaurantId: row.restaurantId,"
            " products: (row.products || []).map(product => ({fullName: product.fullName, price: product.price}))"
            " })) : rows.map(row => {"
            " const address = row.address || {};"
            " const location = address.location || {};"
            " return {"
            " id: row.id, name: row.name, uniqueName: row.uniqueName,"
            " address: {"
            " firstLine: address.firstLine || '', city: address.city || '',"
            " postalCode: address.postalCode || '',"
            " location: {coordinates: location.coordinates || [null, null]}"
            " }"
            " };"
            " });"
            " return {status: 200, body: JSON.stringify({restaurants: body})};"
            "})()"
        )

    def evaluate(self, expression: str, timeout: float):
        message = self.call(
            "script.evaluate",
            {
                "expression": expression,
                "target": {"context": self.context},
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        result = message["result"]
        if result.get("type") == "exception":
            details = result.get("exceptionDetails") or {}
            raise RuntimeError(details.get("text") or "Firefox page script failed")
        return remote_value(result["result"])

    def _fetch(self, url: str, timeout: float):
        expression = (
            "(async () => {"
            f" const response = await fetch({json.dumps(url)}, "
            f"{{headers: {json.dumps(JE_HEADERS)}}});"
            " const body = await response.text();"
            " return {status: response.status, body};"
            "})()"
        )
        last_error: Exception | None = None
        for _ in range(3):
            try:
                value = self.evaluate(expression, timeout=max(timeout, 30))
            except RuntimeError as error:
                last_error = error
                try:
                    href = self.evaluate("location.href", timeout=5)
                except Exception:
                    href = "unavailable"
                log(f"firefox fetch failed: {error} at {href}")
                time.sleep(1)
                continue
            status = int(value["status"])
            body = value.get("body") or ""
            if status == 404:
                return None
            if status != 200:
                raise FirefoxHttpError(status)
            return json.loads(body)
        raise last_error or RuntimeError("Just Eat request failed in Firefox")

    def call(self, method: str, params: dict, timeout: float) -> dict:
        self.next_id += 1
        message_id = self.next_id
        self.send_json({"id": message_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self.read_message(deadline)
            if message.get("id") == message_id:
                if message.get("type") == "error":
                    raise RuntimeError(message.get("message") or message.get("error") or method)
                return message

    def send_json(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        length = len(data)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        elif length < 65536:
            header = bytes([0x81, 0xFE]) + length.to_bytes(2, "big")
        else:
            header = bytes([0x81, 0xFF]) + length.to_bytes(8, "big")
        self.sock.sendall(header + mask + masked)

    def read_message(self, deadline: float) -> dict:
        fragments = []
        while True:
            fin, opcode, payload = self.read_frame(deadline)
            if opcode == 9:
                self.send_control(0x8A, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 8:
                raise ConnectionError("Firefox closed the debugging connection.")
            if opcode in (0, 1, 2):
                fragments.append(payload)
                if fin:
                    return json.loads(b"".join(fragments))

    def send_control(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        header = bytes([0x80 | opcode, 0x80 | len(payload)])
        self.sock.sendall(header + mask + masked)

    def read_frame(self, deadline: float):
        header = self.read_bytes(2, deadline)
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        length = header[1] & 127
        if length == 126:
            length = int.from_bytes(self.read_bytes(2, deadline), "big")
        elif length == 127:
            length = int.from_bytes(self.read_bytes(8, deadline), "big")
        payload = self.read_bytes(length, deadline) if length else b""
        return fin, opcode, payload

    def read_bytes(self, size: int, deadline: float) -> bytes:
        while len(self.buf) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for Firefox.")
            assert self.sock is not None
            self.sock.settimeout(min(1.0, remaining))
            try:
                chunk = self.sock.recv(max(65536, size - len(self.buf)))
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("Firefox debugging connection closed.")
            self.buf += chunk
        data = self.buf[:size]
        self.buf = self.buf[size:]
        return data

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buf = b""
        self.context = None


firefox_client = FirefoxClient()


def get_json(url: str, headers: dict, limiter: RateLimiter | None, timeout: float, label: str = "the search"):
    last_error = "request failed"
    for attempt in range(MAX_RETRIES + 1):
        if limiter:
            limiter.acquire()
        try:
            if "uk.api.just-eat.io" in url:
                return firefox_client.fetch_json(url, timeout)
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except FirefoxHttpError as error:
            last_error = f"HTTP {error.code}"
            if error.code == 404:
                return None
            if error.code != 429 and error.code < 500:
                raise
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code}"
            error.read()
            error.close()
            if error.code == 404:
                return None
            if error.code != 429 and error.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError, OSError) as error:
            last_error = str(error)
        if attempt >= MAX_RETRIES:
            break
        wait_with_status(retry_pause(attempt), label)
    raise RuntimeError(f"{last_error} for {label}")


def load_outcodes() -> list[dict]:
    cache_path = DATA / "outcodes.json"
    if cache_path.exists():
        return read_json(cache_path)

    limiter = RateLimiter(8)
    found = []

    def fetch(number: int):
        code = f"BT{number}"
        payload = get_json(
            f"https://api.postcodes.io/outcodes/{code}",
            headers={"Accept": "application/json", "User-Agent": "festive-chips-finder/1.0"},
            limiter=limiter,
            timeout=20,
        )
        if not payload or not payload.get("result"):
            return None
        result = payload["result"]
        if result.get("latitude") is None or result.get("longitude") is None:
            return None
        districts = result.get("admin_district") or []
        label = districts[0] if districts else ""
        return {
            "code": result.get("outcode") or code,
            "lat": result["latitude"],
            "lng": result["longitude"],
            "area": label,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        for item in pool.map(fetch, range(1, 95)):
            if item:
                found.append(item)
    found.sort(key=lambda item: int(item["code"][2:]))
    if len(found) < 70:
        raise RuntimeError(f"Only found {len(found)} BT districts. Expected about 80.")
    write_json(cache_path, found)
    return found


def search_cache_path(code: str, term: str) -> Path:
    return DATA / "search" / code / f"{slug(term)}.json"


def restaurant_cache_path(code: str) -> Path:
    return DATA / "restaurants" / f"{code}.json"


def search_term(outcode: dict, term: str, use_cache: bool) -> dict:
    path = search_cache_path(outcode["code"], term)
    if use_cache and path.exists():
        return read_json(path)
    query = urllib.parse.quote(term)
    latlong = f"{outcode['lat']:.6f},{outcode['lng']:.6f}"
    url = (
        "https://uk.api.just-eat.io/search/restaurants/uk"
        f"?searchTerm={query}&latlong={latlong}"
    )
    if je_limiter:
        je_limiter.acquire()
    payload = firefox_client.area_json(outcode["code"], url, timeout=40)
    write_json(path, payload or {"restaurants": []})
    return payload or {"restaurants": []}


def load_restaurant_index(code: str, use_cache: bool) -> dict:
    path = restaurant_cache_path(code)
    if use_cache and path.exists():
        return read_json(path)
    if je_limiter:
        je_limiter.acquire()
    payload = firefox_client.area_json(
        code,
        f"https://uk.api.just-eat.io/discovery/uk/restaurants/enriched/byslug/{code.lower()}?serviceType=delivery",
        timeout=60,
    )
    index = {}
    for restaurant in (payload or {}).get("restaurants") or []:
        address = restaurant.get("address") or {}
        location = address.get("location") or {}
        coordinates = location.get("coordinates") or [None, None]
        restaurant_id = str(restaurant.get("id"))
        index[restaurant_id] = {
            "id": restaurant_id,
            "name": restaurant.get("name") or "Unknown store",
            "uniqueName": restaurant.get("uniqueName") or "",
            "firstLine": address.get("firstLine") or "",
            "city": address.get("city") or "",
            "postalCode": address.get("postalCode") or "",
            "lng": coordinates[0] if len(coordinates) > 0 else None,
            "lat": coordinates[1] if len(coordinates) > 1 else None,
        }
    write_json(path, index)
    return index


def search_outcode(outcode: dict, terms: list[str], use_cache: bool) -> list[dict]:
    found: dict[str, dict] = {}
    code = outcode["code"]
    for index, term in enumerate(terms, start=1):
        log(f"{code} query {index}/{len(terms)}: {term}")
        payload = search_term(outcode, term, use_cache)
        restaurants = payload.get("restaurants") or []
        log(f"{code} query {index}/{len(terms)}: {term} returned {len(restaurants)} restaurants")
        for restaurant in restaurants:
            restaurant_id = str(restaurant.get("restaurantId"))
            for product in restaurant.get("products") or []:
                name = (product.get("fullName") or "").strip()
                if not name or not matches_dish(name):
                    continue
                key = " ".join(tokenize(name))
                bucket = found.setdefault(restaurant_id, {})
                if key not in bucket:
                    price = product.get("price")
                    bucket[key] = {
                        "name": name,
                        "pricePence": price if isinstance(price, int) else None,
                    }
    if not found:
        return []

    index = load_restaurant_index(outcode["code"], use_cache)
    hits = []
    for restaurant_id, items in found.items():
        info = index.get(restaurant_id)
        if info is None:
            hits.append(
                {
                    "id": restaurant_id,
                    "name": f"Just Eat store {restaurant_id}",
                    "uniqueName": "",
                    "address": "",
                    "postalCode": "",
                    "lat": None,
                    "lng": None,
                    "url": "",
                    "items": list(items.values()),
                    "foundIn": [outcode["code"]],
                    "located": False,
                }
            )
            continue
        parts = [info["firstLine"], info["city"], info["postalCode"]]
        unique = info["uniqueName"]
        hits.append(
            {
                "id": info["id"],
                "name": info["name"],
                "uniqueName": unique,
                "address": ", ".join(part for part in parts if part),
                "postalCode": info["postalCode"],
                "lat": info["lat"],
                "lng": info["lng"],
                "url": f"https://www.just-eat.co.uk/restaurants-{unique}/menu" if unique else "",
                "items": list(items.values()),
                "foundIn": [outcode["code"]],
                "located": info["lat"] is not None and info["lng"] is not None,
            }
        )
    return hits


def merge_hit(stores: dict, hit: dict) -> None:
    current = stores.get(hit["id"])
    if current is None:
        hit["items"].sort(key=lambda item: item["name"].lower())
        stores[hit["id"]] = hit
        return
    known = {" ".join(tokenize(item["name"])) for item in current["items"]}
    for item in hit["items"]:
        key = " ".join(tokenize(item["name"]))
        if key not in known:
            current["items"].append(item)
            known.add(key)
    current["items"].sort(key=lambda item: item["name"].lower())
    for code in hit["foundIn"]:
        if code not in current["foundIn"]:
            current["foundIn"].append(code)
    current["foundIn"].sort(key=lambda code: int(code[2:]))
    if hit["located"] and not current["located"]:
        for field in ("name", "uniqueName", "address", "postalCode", "lat", "lng", "url", "located"):
            current[field] = hit[field]


def public_results(stores: dict) -> list[dict]:
    rows = list(stores.values())
    for row in rows:
        row["foundIn"].sort(key=lambda code: int(code[2:]))
    rows.sort(key=lambda row: row["name"].lower())
    return rows


def search_queries(terms: list[str]) -> list[str]:
    """Collapse chip/chips, fry/fries, and roastie/roasties so each dish is requested once."""
    chosen = {}
    for term in terms:
        key = tuple(tokenize(term))
        current = chosen.get(key)
        if current is None or len(term) < len(current):
            chosen[key] = term
    queries = []
    seen = set()
    for term in terms:
        query = chosen[tuple(tokenize(term))]
        if query not in seen:
            seen.add(query)
            queries.append(query)
    return queries


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def postcode_number(code: str) -> int:
    return int(code[2:])


def empty_manifest_entry(outcode: dict) -> dict:
    return {
        "code": outcode["code"],
        "area": outcode.get("area") or "",
        "lat": outcode["lat"],
        "lng": outcode["lng"],
        "status": "never",
        "searchedAt": None,
        "lastSuccessfulRun": None,
        "error": None,
    }


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"postcodes": []}
    payload = read_json(MANIFEST_PATH)
    postcodes = payload.get("postcodes") if isinstance(payload, dict) else None
    return {"postcodes": postcodes or []}


def save_manifest(entries: list[dict]) -> None:
    entries.sort(key=lambda entry: postcode_number(entry["code"]))
    write_json(MANIFEST_PATH, {"postcodes": entries})


def ensure_manifest(outcodes: list[dict]) -> list[dict]:
    current = {entry["code"]: entry for entry in load_manifest()["postcodes"]}
    entries = []
    for outcode in outcodes:
        entry = current.get(outcode["code"]) or empty_manifest_entry(outcode)
        entry["area"] = outcode.get("area") or entry.get("area") or ""
        entry["lat"] = outcode["lat"]
        entry["lng"] = outcode["lng"]
        if entry.get("status") == "ok" and not entry.get("lastSuccessfulRun"):
            entry["lastSuccessfulRun"] = entry.get("searchedAt")
        entry["lastSuccessfulRun"] = entry.get("lastSuccessfulRun")
        if entry.get("status") == "searching":
            entry["status"] = "failed"
            entry["error"] = entry.get("error") or "Search was interrupted."
        entries.append(entry)
    save_manifest(entries)
    return entries


def scrape_sort_key(entry: dict):
    """Recent failures first, then never searched, then oldest successful runs."""
    status = entry.get("status") or "never"
    number = postcode_number(entry["code"])
    searched_at = parse_time(entry.get("searchedAt"))
    success_at = parse_time(entry.get("lastSuccessfulRun"))
    if status == "failed":
        stamp = searched_at.timestamp() if searched_at else 0
        return (0, -stamp, number)
    if status != "ok" or success_at is None:
        return (1, number, 0)
    return (2, success_at.timestamp(), number)


def ordered_postcodes(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=scrape_sort_key)


def load_saved_results() -> list[dict]:
    if not RESULTS_PATH.exists():
        return []
    payload = read_json(RESULTS_PATH)
    if isinstance(payload, list):
        return payload
    return payload.get("results") or []


def hits_path(code: str) -> Path:
    return HITS_DIR / f"{code}.json"


def save_hits(code: str, hits: list[dict]) -> None:
    write_json(hits_path(code), hits)


def rebuild_results() -> list[dict]:
    stores: dict = {}
    if HITS_DIR.exists():
        for path in sorted(HITS_DIR.glob("BT*.json"), key=lambda item: postcode_number(item.stem)):
            for hit in read_json(path):
                merge_hit(stores, hit)
    results = public_results(stores)
    write_json(RESULTS_PATH, {"results": results})
    return results


def update_manifest_entry(code: str, **updates) -> None:
    entries = load_manifest()["postcodes"]
    for entry in entries:
        if entry["code"] == code:
            entry.update(updates)
            break
    save_manifest(entries)


def record_failure(code: str, error: str) -> None:
    """Keep this postcode's previous stores and its previous lastSuccessfulRun."""
    update_manifest_entry(
        code,
        status="failed",
        searchedAt=datetime.now(timezone.utc).isoformat(),
        error=error,
    )
    rebuild_results()


def store_counts() -> tuple[int, int]:
    if not RESULTS_PATH.exists():
        return 0, 0
    rows = read_json(RESULTS_PATH).get("results") or []
    located = sum(1 for row in rows if row.get("located") and row.get("lat") is not None)
    return len(rows), located


def write_run_status(payload: dict) -> None:
    write_json(RUN_STATUS_PATH, payload)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## Search run",
        "",
        f"- Succeeded: {payload['succeeded']}",
        f"- Failed: {payload['failed']}",
        f"- Stores: {payload['stores']} ({payload['locatedStores']} on the map)",
        f"- Duration: {payload['duration']}",
        "",
    ]
    failed = payload["failedPostcodes"]
    if failed:
        lines.append("| Postcode | Last success | Previous stores kept | Error |")
        lines.append("| --- | --- | --- | --- |")
        for item in failed:
            kept = "yes" if item["previousStoresKept"] else "no"
            last_success = item["lastSuccessfulRun"] or "never"
            error = str(item["error"]).replace("|", "/")
            lines.append(f"| {item['code']} | {last_success} | {kept} | {error} |")
    else:
        lines.append("Every postcode succeeded.")
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def run_search() -> None:
    """Search every district once. Recent failures go first."""
    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    outcomes: list[dict] = []
    stopped = None
    finished = False
    planned = 0
    queries: list[str] = []
    try:
        outcodes = load_outcodes()
        entries = ensure_manifest(outcodes)
        by_code = {item["code"]: item for item in outcodes}
        queries = search_queries(DEFAULT_TERMS)
        order = ordered_postcodes(entries)
        planned = len(order)
        prior = {"failed": 0, "never": 0, "ok": 0}
        for entry in order:
            status = entry.get("status") or "never"
            prior[status if status in prior else "never"] += 1
        log(
            f"starting {len(order)} postcodes: "
            f"{prior['failed']} recent failures, {prior['never']} never searched, "
            f"{prior['ok']} previous successes"
        )
        log("queries: " + ", ".join(queries))
        succeeded = 0
        failed = 0
        for index, entry in enumerate(order, start=1):
            code = entry["code"]
            outcode = by_code[code]
            reason = entry.get("status") or "never"
            had_hits = hits_path(code).exists()
            began = time.monotonic()
            log(f"[{index}/{len(order)}] {code} previous status {reason}")
            update_manifest_entry(code, status="searching", error=None)
            try:
                hits = search_outcode(outcode, queries, use_cache=False)
            except Exception as error:
                record_failure(code, str(error))
                failed += 1
                elapsed = time.monotonic() - began
                outcomes.append(
                    {
                        "code": code,
                        "status": "failed",
                        "previousStatus": reason,
                        "matches": 0,
                        "seconds": round(elapsed, 1),
                        "error": str(error),
                        "lastSuccessfulRun": entry.get("lastSuccessfulRun"),
                        "previousStoresKept": had_hits,
                    }
                )
                log(
                    f"[{index}/{len(order)}] {code} failed in {format_duration(elapsed)} "
                    f"({succeeded} ok, {failed} failed, elapsed {format_duration(time.monotonic() - started)}): {error}"
                )
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    detail = " ".join(str(error).split())
                    print(f"::warning title={code}::{detail}", flush=True)
                continue
            now = datetime.now(timezone.utc).isoformat()
            save_hits(code, hits)
            update_manifest_entry(
                code,
                status="ok",
                searchedAt=now,
                lastSuccessfulRun=now,
                error=None,
            )
            results = rebuild_results()
            succeeded += 1
            elapsed = time.monotonic() - began
            outcomes.append(
                {
                    "code": code,
                    "status": "ok",
                    "previousStatus": reason,
                    "matches": len(hits),
                    "seconds": round(elapsed, 1),
                    "error": None,
                    "lastSuccessfulRun": now,
                    "previousStoresKept": False,
                }
            )
            log(
                f"[{index}/{len(order)}] {code} ok in {format_duration(elapsed)}, "
                f"{len(hits)} matches, {len(results)} stores "
                f"({succeeded} ok, {failed} failed, elapsed {format_duration(time.monotonic() - started)})"
            )
        finished = True
    except Exception as error:
        stopped = str(error)
        log(f"search stopped: {error}")
        raise
    finally:
        finished_at = datetime.now(timezone.utc)
        duration = time.monotonic() - started
        stores, located = store_counts()
        failed_postcodes = [item for item in outcomes if item["status"] == "failed"]
        payload = {
            "startedAt": started_at.isoformat(),
            "finishedAt": finished_at.isoformat(),
            "durationSeconds": round(duration, 1),
            "duration": format_duration(duration),
            "completed": finished and stopped is None,
            "stoppedReason": stopped,
            "postcodeCount": planned,
            "searched": len(outcomes),
            "succeeded": sum(1 for item in outcomes if item["status"] == "ok"),
            "failed": len(failed_postcodes),
            "stores": stores,
            "locatedStores": located,
            "matchesFound": sum(item["matches"] for item in outcomes),
            "queries": queries,
            "failedPostcodes": [
                {
                    "code": item["code"],
                    "error": item["error"],
                    "lastSuccessfulRun": item["lastSuccessfulRun"],
                    "previousStoresKept": item["previousStoresKept"],
                }
                for item in failed_postcodes
            ],
            "postcodes": outcomes,
        }
        write_run_status(payload)
        headline = "finished" if finished else "stopped"
        log(
            f"{headline} in {payload['duration']}: "
            f"{payload['succeeded']} succeeded, {payload['failed']} failed, "
            f"{payload['searched']}/{planned} searched, "
            f"{stores} stores ({located} on the map)"
        )
        if stopped:
            log(f"stopped because: {stopped}")
        if failed_postcodes:
            names = ", ".join(
                f"{item['code']} ({item['error']})" for item in failed_postcodes
            )
            log(f"failed postcodes: {names}")
        elif finished:
            log("every searched postcode succeeded")
        log(f"run status written to {RUN_STATUS_PATH.relative_to(ROOT)}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(
                "::notice title=Search finished::"
                f"{payload['succeeded']} succeeded, {payload['failed']} failed, {stores} stores",
                flush=True,
            )


def main() -> None:
    global je_limiter
    je_limiter = RateLimiter(REQUESTS_PER_SECOND)
    DATA.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(parents=True, exist_ok=True)
    if not RESULTS_PATH.exists() and (DATA / "results.json").exists():
        write_json(RESULTS_PATH, read_json(DATA / "results.json"))
    if not RESULTS_PATH.exists():
        write_json(RESULTS_PATH, {"results": []})
    try:
        run_search()
    finally:
        firefox_client.quit_firefox()


if __name__ == "__main__":
    main()
