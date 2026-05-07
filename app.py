"""
Vietnam Metro & Railway News
============================
Flask app aggregating Google News RSS feeds.

Architecture (the important part):
  - A background thread continuously refreshes the cache every CACHE_TTL.
  - /api/news ALWAYS returns the current cache instantly (never blocks on fetch).
  - When the cache is cold (warming up), the API reports status="warming" and
    the frontend polls until it goes "ready". This is what avoids Render's
    30s response timeout that produced the recurring 502 errors.
  - Cache also persists to /tmp so warm restarts don't start from zero.
"""

import json as _json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

from flask import Flask, Response, render_template, request

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VI_QUERIES = [
    "metro Ho Chi Minh",
    "metro Ha Noi",
    "du an metro Viet Nam",
    "duong sat toc do cao Bac Nam",
    "duong sat Viet Nam",
    "tuyen metro Ben Thanh Suoi Tien",
]

EN_QUERIES = [
    "Vietnam metro line",
    "Vietnam high speed rail",
    "Hanoi Ho Chi Minh railway",
]

MAX_VI        = 20
MAX_EN        = 10
MAX_AGE_DAYS  = 30
FETCH_TIMEOUT = 8     # per-RSS HTTP timeout (seconds)
MAX_WORKERS   = 6     # parallel RSS fetches
CACHE_TTL     = 300   # 5 minutes — refresh interval for the background warmer
CACHE_FILE    = os.environ.get("CACHE_FILE", "/tmp/vn_metro_news_cache.json")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Snapshot store for /api/snapshot (capped LRU)
_snapshots: "OrderedDict[str, dict]" = OrderedDict()
_SNAPSHOT_CAP = 20


# ---------------------------------------------------------------------------
# Cache state — accessed by background thread + request handlers
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict = {
    "news": [],
    "fetched_at": None,    # ISO string
    "status": "warming",   # "warming" | "ready" | "stale"
    "last_error": None,
}


def _load_cache_from_disk() -> None:
    """Best-effort: restore cache after a warm restart."""
    try:
        if not os.path.exists(CACHE_FILE):
            return
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, dict) and isinstance(data.get("news"), list) and data["news"]:
            with _cache_lock:
                _cache["news"]       = data["news"]
                _cache["fetched_at"] = data.get("fetched_at")
                _cache["status"]     = "ready"
            print(f"[cache] Restored {len(data['news'])} articles from {CACHE_FILE}")
    except Exception as e:
        print(f"[cache] load failed: {e}")


def _save_cache_to_disk() -> None:
    try:
        with _cache_lock:
            payload = {"news": _cache["news"], "fetched_at": _cache["fetched_at"]}
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        print(f"[cache] save failed: {e}")


# ---------------------------------------------------------------------------
# HTTP fetch with curl fallback
# ---------------------------------------------------------------------------

def _get_url(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[urllib] {url[:70]}: {e}")

    try:
        proc = subprocess.run(
            ["curl", "-sL", "--max-time", str(FETCH_TIMEOUT), "-A", _UA, url],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=FETCH_TIMEOUT + 2,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
    except Exception as e:
        print(f"[curl] {url[:70]}: {e}")

    return ""


def fetch_rss(query: str, lang: str = "en") -> list:
    encoded = urllib.parse.quote(query)
    if lang == "vi":
        url = f"https://news.google.com/rss/search?q={encoded}&hl=vi&gl=VN&ceid=VN:vi"
    else:
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=VN&ceid=VN:en"
    text = _get_url(url)
    return parse_rss(text, lang=lang) if text else []


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------

def _strip_namespaces(xml_text: str) -> str:
    xml_text = re.sub(r'\s+xmlns(?::\w+)?\s*=\s*"[^"]*"', "", xml_text)
    xml_text = re.sub(r"<(/?)[a-zA-Z][a-zA-Z0-9_]*:", r"<\1", xml_text)
    return xml_text


def parse_rss(xml_text: str, lang: str = "en") -> list:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        try:
            root = ET.fromstring(_strip_namespaces(xml_text))
        except ET.ParseError as e:
            print(f"[parse_rss] XML error ({lang}): {e}")
            return []

    items = []
    for item in (root.findall(".//item") or root.findall(".//entry")):
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        pub   = (item.findtext("pubDate") or "").strip()

        source = ""
        src_el = item.find("source")
        if src_el is not None:
            source = (src_el.text or "").strip()

        dt_obj = None
        date_str = ""
        age_label = ""
        try:
            dt_obj    = parsedate_to_datetime(pub)
            date_str  = dt_obj.strftime("%d %b %Y")
            age_label = age_ago(dt_obj)
        except Exception:
            date_str = pub[:16] if pub else ""

        if title:
            items.append({
                "title":    title,
                "link":     link,
                "source":   source,
                "date":     date_str,
                "age":      age_label,
                "lang":     lang,
                "dt":       dt_obj,
                "category": categorize(title),
                "location": locate(title),
            })
    return items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def age_ago(dt) -> str:
    try:
        s = int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
        if s < 3600:
            m = max(1, s // 60)
            return f"{m} minute{'s' if m != 1 else ''} ago"
        if s < 86400:
            h = s // 3600
            return f"{h} hour{'s' if h != 1 else ''} ago"
        d = s // 86400
        return f"{d} day{'s' if d != 1 else ''} ago"
    except Exception:
        return ""


def categorize(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["metro", "mrt", "ben thanh", "suoi tien", "nhon", "cat linh"]):
        return "Metro"
    return "Railway"


def locate(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["ho chi minh", "hcmc", "saigon", "sai gon", "tphcm", "tp.hcm"]):
        return "Ho Chi Minh City"
    if any(k in t for k in ["hanoi", "ha noi", "cat linh", "nhon"]):
        return "Hanoi"
    if any(k in t for k in ["da nang", "danang"]):
        return "Da Nang"
    return "Vietnam"


def _sort_key(a):
    dt = a.get("dt")
    return dt.astimezone(timezone.utc) if dt else datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Parallel fetch + cache refresh
# ---------------------------------------------------------------------------

def _fetch_all() -> list:
    """Fetch every (query, lang) pair in parallel and return a deduped, sorted list."""
    pairs = [(q, "vi") for q in VI_QUERIES] + [(q, "en") for q in EN_QUERIES]
    results: dict[int, list] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_idx = {
            executor.submit(fetch_rss, q, lang): i
            for i, (q, lang) in enumerate(pairs)
        }
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except Exception as e:
                print(f"[fetch] index {idx} error: {e}")
                results[idx] = []

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    seen: set = set()
    flat: list = []
    for i in range(len(pairs)):
        for a in results.get(i, []):
            key = re.sub(r"\s+", " ", a["title"].lower()[:60])
            if key in seen:
                continue
            dt = a.get("dt")
            if dt and dt.astimezone(timezone.utc) < cutoff:
                continue
            seen.add(key)
            flat.append(a)

    flat.sort(key=_sort_key, reverse=True)

    vi_items = [a for a in flat if a["lang"] == "vi"][:MAX_VI]
    en_items = [a for a in flat if a["lang"] == "en"][:MAX_EN]
    final = vi_items + en_items

    # Strip non-serializable datetime field
    for a in final:
        a.pop("dt", None)

    return final


def _refresh_cache_once() -> None:
    """Run one fetch cycle and update cache. Never raises."""
    t0 = time.time()
    print(f"[refresh] start at {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    try:
        items = _fetch_all()
        elapsed = time.time() - t0
        with _cache_lock:
            if items:
                _cache["news"]       = items
                _cache["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                _cache["status"]     = "ready"
                _cache["last_error"] = None
                print(f"[refresh] OK in {elapsed:.1f}s — {len(items)} articles")
            else:
                # Keep previous cache; mark stale if we ever had data
                if _cache["news"]:
                    _cache["status"] = "stale"
                _cache["last_error"] = "fetch returned 0 articles"
                print(f"[refresh] WARN no articles in {elapsed:.1f}s — keeping previous cache")
        _save_cache_to_disk()
    except Exception as e:
        with _cache_lock:
            _cache["last_error"] = str(e)
            if _cache["news"]:
                _cache["status"] = "stale"
        print(f"[refresh] ERROR: {e}")


_warmer_started = False
_warmer_lock = threading.Lock()


def _start_warmer_thread() -> None:
    """Start the background warmer exactly once per process."""
    global _warmer_started
    with _warmer_lock:
        if _warmer_started:
            return
        _warmer_started = True

    def _loop():
        while True:
            _refresh_cache_once()
            time.sleep(CACHE_TTL)

    t = threading.Thread(target=_loop, name="news-warmer", daemon=True)
    t.start()
    print("[warmer] background thread started")


# Kick off warming as soon as the module loads (i.e. first request to gunicorn worker)
_load_cache_from_disk()
_start_warmer_thread()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    """Cheap endpoint for uptime pingers (cron-job.org / UptimeRobot)."""
    with _cache_lock:
        return {
            "status":     _cache["status"],
            "count":      len(_cache["news"]),
            "fetched_at": _cache["fetched_at"],
            "last_error": _cache["last_error"],
        }


@app.route("/api/news")
def news():
    """
    NEVER blocks on fetch. Returns whatever is currently in the cache.
    Frontend polls while status='warming'.
    """
    with _cache_lock:
        items      = list(_cache["news"])
        status     = _cache["status"]
        fetched_at = _cache["fetched_at"]
        last_error = _cache["last_error"]

    return {
        "status":     "success",   # request itself succeeded
        "cache":      status,      # warming | ready | stale
        "news":       items,
        "count":      len(items),
        "fetched_at": fetched_at,
        "last_error": last_error,
    }


@app.route("/api/export")
def export():
    with _cache_lock:
        items = list(_cache["news"])
    payload = {
        "source":     "Vietnam Metro & Railway News",
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total":      len(items),
        "vi_count":   sum(1 for a in items if a.get("lang") == "vi"),
        "en_count":   sum(1 for a in items if a.get("lang") == "en"),
        "articles":   items,
    }
    return Response(
        _json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.route("/api/snapshot", methods=["POST"])
def create_snapshot():
    """Freeze the current articles into a unique shareable URL."""
    with _cache_lock:
        items = list(_cache["news"])

    if not items:
        return {"status": "error", "message": "Cache is still warming. Try again in a few seconds."}, 503

    snap_id = uuid.uuid4().hex
    payload = {
        "source":      "Vietnam Metro & Railway News",
        "snapshot_id": snap_id,
        "created_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total":       len(items),
        "vi_count":    sum(1 for a in items if a.get("lang") == "vi"),
        "en_count":    sum(1 for a in items if a.get("lang") == "en"),
        "articles":    items,
    }
    _snapshots[snap_id] = payload
    if len(_snapshots) > _SNAPSHOT_CAP:
        _snapshots.popitem(last=False)

    base = request.host_url.rstrip("/")
    return {"status": "success", "url": f"{base}/snapshot/{snap_id}"}


@app.route("/snapshot/<snap_id>")
def view_snapshot(snap_id):
    payload = _snapshots.get(snap_id)
    if not payload:
        return {"error": "Snapshot not found or expired."}, 404
    return Response(
        _json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8",
        headers={"Access-Control-Allow-Origin": "*"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Vietnam Metro & Railway News -> http://localhost:{port}")
    app.run(host="0.0.0.0", debug=False, port=port)
