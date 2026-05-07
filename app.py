"""
Vietnam Metro & Railway News
============================
Fetches directly from Vietnamese news-site RSS feeds (VnExpress, Tuổi Trẻ,
Thanh Niên, VTV, Dân Trí, …) and filters for metro/railway articles.

Why not Google News RSS?
  Render's shared-IP servers are rate-limited by Google, which returns HTML
  (captcha/block pages) instead of XML, yielding 0 articles every time.
  Direct news-site RSS has no such restriction.

Architecture:
  Background thread refreshes cache every CACHE_TTL seconds.
  /api/news returns cache instantly — never blocks. Frontend polls.
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
# RSS feed sources — direct Vietnamese news sites, no API key needed.
# Parallel fetching is safe here because each request goes to a different
# server (unlike Google News where many requests to one IP triggers blocking).
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    # Vietnamese sources
    {"url": "https://vnexpress.net/rss/thoi-su.rss",    "lang": "vi", "src": "VnExpress"},
    {"url": "https://vnexpress.net/rss/kinh-te.rss",    "lang": "vi", "src": "VnExpress"},
    {"url": "https://vnexpress.net/rss/giao-thong.rss", "lang": "vi", "src": "VnExpress"},
    {"url": "https://tuoitre.vn/rss/thoi-su.rss",       "lang": "vi", "src": "Tuổi Trẻ"},
    {"url": "https://tuoitre.vn/rss/kinh-te.rss",       "lang": "vi", "src": "Tuổi Trẻ"},
    {"url": "https://thanhnien.vn/rss/home.rss",        "lang": "vi", "src": "Thanh Niên"},
    {"url": "https://vtv.vn/trong-nuoc.rss",            "lang": "vi", "src": "VTV"},
    {"url": "https://dantri.com.vn/xa-hoi.rss",         "lang": "vi", "src": "Dân Trí"},
    # English sources
    {"url": "https://e.vnexpress.net/rss/news.rss",     "lang": "en", "src": "VnExpress EN"},
    {"url": "https://e.vnexpress.net/rss/business.rss", "lang": "en", "src": "VnExpress EN"},
]

# Keywords to filter relevant articles from the general-topic RSS feeds.
VI_KEYWORDS = [
    "metro", "mrt", "tàu điện", "bến thành", "suối tiến",
    "nhổn", "cát linh", "đường sắt", "tốc độ cao",
    "đường ray", "tuyến tàu", "ga hà nội", "ga sài gòn",
    "đường sắt cao tốc", "đường sắt đô thị", "đường sắt liên",
    "đô thị rail", "vành đai giao thông", "vận tải hành khách",
]

EN_KEYWORDS = [
    "metro", "railway", "rail line", "rail project", "rail network",
    "high-speed rail", "high speed rail", "mrt", "metro line",
    "metro station", "metro system", "rail infrastructure",
]

MAX_VI        = 20
MAX_EN        = 10
MAX_AGE_DAYS  = 30
FETCH_TIMEOUT = 10    # seconds per RSS request
MAX_WORKERS   = 6     # parallel threads (safe — different servers)
CACHE_TTL     = 300   # 5 minutes
CACHE_FILE    = os.environ.get("CACHE_FILE", "/tmp/vn_metro_news_cache.json")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_snapshots: "OrderedDict[str, dict]" = OrderedDict()
_SNAPSHOT_CAP = 20


# ---------------------------------------------------------------------------
# Cache state
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict = {
    "news":       [],
    "fetched_at": None,
    "status":     "warming",   # warming | ready | stale
    "last_error": None,
}
_last_fetch_stats: list = []   # [{url, src, lang, fetched, matched, ok}]


def _load_cache_from_disk() -> None:
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
            print(f"[cache] Restored {len(data['news'])} articles from disk")
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
# HTTP fetch
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


# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------

def _strip_namespaces(xml_text: str) -> str:
    xml_text = re.sub(r'\s+xmlns(?::\w+)?\s*=\s*"[^"]*"', "", xml_text)
    xml_text = re.sub(r"<(/?)[a-zA-Z][a-zA-Z0-9_]*:", r"<\1", xml_text)
    return xml_text


def _try_parse_date(s: str):
    """Try RFC-2822 first, then ISO 8601, then give up."""
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt[:len(fmt)])
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def parse_feed(xml_text: str, lang: str, default_source: str) -> list:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        try:
            root = ET.fromstring(_strip_namespaces(xml_text))
        except ET.ParseError as e:
            print(f"[parse] XML error: {e}")
            return []

    items = []
    for item in (root.findall(".//item") or root.findall(".//entry")):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue

        link = (item.findtext("link") or "").strip()
        # Atom feeds store link in an attribute
        if not link:
            lel = item.find("link")
            if lel is not None:
                link = (lel.get("href") or "").strip()

        # Date: try pubDate, then dc:date, then published/updated
        pub_raw = (
            item.findtext("pubDate") or
            item.findtext("date") or
            item.findtext("published") or
            item.findtext("updated") or ""
        )
        dt_obj    = _try_parse_date(pub_raw.strip())
        date_str  = dt_obj.strftime("%d %b %Y") if dt_obj else ""
        age_label = age_ago(dt_obj) if dt_obj else ""

        # Source: use default_source (the feed's site name)
        items.append({
            "title":    title,
            "link":     link,
            "source":   default_source,
            "date":     date_str,
            "age":      age_label,
            "lang":     lang,
            "dt":       dt_obj,
            "category": categorize(title),
            "location": locate(title),
        })
    return items


def fetch_feed(feed: dict) -> dict:
    """Fetch one RSS feed and return stats + parsed items."""
    url  = feed["url"]
    lang = feed["lang"]
    src  = feed["src"]
    text = _get_url(url)
    if not text:
        return {"url": url, "src": src, "lang": lang, "fetched": 0, "matched": 0, "ok": False}

    all_items = parse_feed(text, lang=lang, default_source=src)
    relevant  = [a for a in all_items if is_relevant(a["title"], lang)]
    return {
        "url":     url,
        "src":     src,
        "lang":    lang,
        "fetched": len(all_items),
        "matched": len(relevant),
        "ok":      len(all_items) > 0,
        "_items":  relevant,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_relevant(title: str, lang: str) -> bool:
    t = title.lower()
    keywords = VI_KEYWORDS if lang == "vi" else EN_KEYWORDS
    return any(kw in t for kw in keywords)


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
    if any(k in t for k in ["metro", "mrt", "bến thành", "suối tiến", "nhổn", "cát linh",
                              "tàu điện", "đường sắt đô thị"]):
        return "Metro"
    return "Railway"


def locate(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["ho chi minh", "hcmc", "saigon", "sài gòn", "tphcm", "tp.hcm",
                              "bến thành", "suối tiến"]):
        return "Ho Chi Minh City"
    if any(k in t for k in ["hanoi", "hà nội", "cát linh", "nhổn"]):
        return "Hanoi"
    if any(k in t for k in ["đà nẵng", "da nang", "danang"]):
        return "Da Nang"
    return "Vietnam"


def _sort_key(a):
    dt = a.get("dt")
    return dt.astimezone(timezone.utc) if dt else datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Parallel fetch across different news sites
# ---------------------------------------------------------------------------

def _fetch_all() -> list:
    """
    Fetch all RSS feeds in parallel (safe — each is a different server) and
    return a keyword-filtered, deduplicated, sorted article list.
    """
    global _last_fetch_stats

    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_to_idx = {ex.submit(fetch_feed, f): i for i, f in enumerate(RSS_FEEDS)}
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except Exception as e:
                feed = RSS_FEEDS[idx]
                print(f"[fetch] {feed['url']}: {e}")
                results[idx] = {**feed, "fetched": 0, "matched": 0, "ok": False, "_items": []}

    # Build stats (without _items to keep it small)
    _last_fetch_stats = [
        {k: v for k, v in results[i].items() if k != "_items"}
        for i in range(len(RSS_FEEDS))
    ]

    # Flatten + deduplicate + cutoff
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    seen:  set  = set()
    flat:  list = []
    for i in range(len(RSS_FEEDS)):
        for a in results.get(i, {}).get("_items", []):
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

    for a in final:
        a.pop("dt", None)

    return final


# ---------------------------------------------------------------------------
# Background warmer
# ---------------------------------------------------------------------------

def _refresh_cache_once() -> None:
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
                if _cache["news"]:
                    _cache["status"] = "stale"
                _cache["last_error"] = "fetch returned 0 articles after keyword filter"
                print(f"[refresh] WARN no articles in {elapsed:.1f}s")
        _save_cache_to_disk()
    except Exception as e:
        with _cache_lock:
            _cache["last_error"] = str(e)
            if _cache["news"]:
                _cache["status"] = "stale"
        print(f"[refresh] ERROR: {e}")


_warmer_started = False
_warmer_lock    = threading.Lock()


def _start_warmer_thread() -> None:
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


# Start immediately on import
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
    with _cache_lock:
        return {
            "status":     _cache["status"],
            "count":      len(_cache["news"]),
            "fetched_at": _cache["fetched_at"],
            "last_error": _cache["last_error"],
        }


@app.route("/api/debug")
def debug():
    """Per-feed fetch stats from the last refresh cycle."""
    with _cache_lock:
        return {
            "cache_status":  _cache["status"],
            "article_count": len(_cache["news"]),
            "fetched_at":    _cache["fetched_at"],
            "last_error":    _cache["last_error"],
            "feed_stats":    _last_fetch_stats,
        }


@app.route("/api/news")
def news():
    """Always returns instantly from cache. Frontend polls while cache='warming'."""
    with _cache_lock:
        items      = list(_cache["news"])
        status     = _cache["status"]
        fetched_at = _cache["fetched_at"]
        last_error = _cache["last_error"]

    return {
        "status":     "success",
        "cache":      status,
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
