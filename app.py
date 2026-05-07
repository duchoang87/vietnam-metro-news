import json as _json
import os
import re
import subprocess
import threading
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from flask import Flask, Response, render_template, request

app = Flask(__name__)

VI_QUERIES = [
    "metro Ho Chi Minh",
    "metro Ha Noi",
    "tuyen metro thanh pho ho chi minh",
    "du an Metro",
    "metro ben thanh",
    "duong sat toc do cao",
    "duong sat Bac Nam",
    "duong sat Lien vung",
    "duong sat Thong Nhat",
    "duong sat Ha Noi Lao Cai Hai Phong",
]

EN_QUERIES = [
    "Ho Chi Minh City metro",
    "Hanoi metro line",
    "vietnam high speed rail",
    "vietnam railway infrastructure",
]

MAX_VI       = 20
MAX_EN       = 10
MAX_AGE_DAYS = 30
FETCH_TIMEOUT = 8   # seconds per RSS request
MAX_WORKERS   = 8   # parallel threads

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# In-memory snapshot store: uuid -> payload (capped at 20 entries)
_snapshots = OrderedDict()


# ---------------------------------------------------------------------------
# HTTP fetch — urllib with curl fallback
# ---------------------------------------------------------------------------

def _get_url(url: str) -> str:
    """Return the raw text of a URL. Tries urllib first, then curl."""
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
    """Remove XML namespace declarations so ET can parse without ns prefixes."""
    xml_text = re.sub(r'\s+xmlns(?::\w+)?\s*=\s*"[^"]*"', "", xml_text)
    xml_text = re.sub(r"<(/?)[a-zA-Z][a-zA-Z0-9_]*:", r"<\1", xml_text)
    return xml_text


def parse_rss(xml_text: str, lang: str = "en") -> list:
    root = None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        try:
            root = ET.fromstring(_strip_namespaces(xml_text))
        except ET.ParseError as e:
            print(f"[parse_rss] XML error ({lang}): {e}")
            return []

    items = []
    entries = root.findall(".//item") or root.findall(".//entry")
    for item in entries:
        title  = (item.findtext("title")   or "").strip()
        link   = (item.findtext("link")    or "").strip()
        pub    = (item.findtext("pubDate") or "").strip()

        # <source> element
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
    if any(k in t for k in ["ho chi minh", "hcmc", "saigon", "sai gon"]):
        return "Ho Chi Minh City"
    if any(k in t for k in ["hanoi", "ha noi", "cat linh", "nhon"]):
        return "Hanoi"
    if any(k in t for k in ["da nang", "danang"]):
        return "Da Nang"
    return "Vietnam"


def sort_key(a):
    dt = a.get("dt")
    return dt.astimezone(timezone.utc) if dt else datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Parallel news fetcher
# ---------------------------------------------------------------------------

_cached_news: list = []
_last_fetch_time = None
_fetch_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutes


def _fetch_all_parallel(queries_lang_pairs: list, cutoff, seen: set) -> list:
    """
    Fetch all (query, lang) pairs in parallel using ThreadPoolExecutor.
    Returns deduplicated list sorted newest-first.
    """
    results_map: dict[int, list] = {}  # preserve insertion order per query

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_idx = {
            executor.submit(fetch_rss, q, lang): i
            for i, (q, lang) in enumerate(queries_lang_pairs)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results_map[idx] = future.result()
            except Exception as e:
                print(f"[parallel fetch] error index {idx}: {e}")
                results_map[idx] = []

    # Flatten in original order, deduplicate, apply cutoff
    all_articles = []
    local_seen = set(seen)  # copy so we don't mutate the shared set during iteration
    for i in range(len(queries_lang_pairs)):
        for a in results_map.get(i, []):
            key = re.sub(r"\s+", " ", a["title"].lower()[:60])
            if key in local_seen:
                continue
            dt = a.get("dt")
            if dt and dt.astimezone(timezone.utc) < cutoff:
                continue
            local_seen.add(key)
            all_articles.append(a)

    # Update shared seen set
    seen.update(local_seen)

    all_articles.sort(key=sort_key, reverse=True)
    return all_articles


def get_news() -> list:
    global _cached_news, _last_fetch_time

    now = datetime.now(timezone.utc)

    # Fast path: return fresh cache (no lock needed for read)
    if _cached_news and _last_fetch_time and (now - _last_fetch_time).total_seconds() < CACHE_TTL:
        return _cached_news

    with _fetch_lock:
        # Double-check inside lock
        if _cached_news and _last_fetch_time and (now - _last_fetch_time).total_seconds() < CACHE_TTL:
            return _cached_news

        t0 = time.time()
        print(f"[get_news] Fetching at {now.strftime('%H:%M:%S')} UTC ...")

        cutoff = now - timedelta(days=MAX_AGE_DAYS)
        seen: set = set()

        # Build combined query list: VI first, then EN
        pairs = [(q, "vi") for q in VI_QUERIES] + [(q, "en") for q in EN_QUERIES]
        all_articles = _fetch_all_parallel(pairs, cutoff, seen)

        # Split and cap
        vi_articles = [a for a in all_articles if a["lang"] == "vi"][:MAX_VI]
        en_articles = [a for a in all_articles if a["lang"] == "en"][:MAX_EN]
        results = vi_articles + en_articles

        elapsed = time.time() - t0
        print(f"[get_news] Done in {elapsed:.1f}s — VI={len(vi_articles)}, EN={len(en_articles)}")

        # Strip non-serializable dt field
        for a in results:
            a.pop("dt", None)

        if results:
            _cached_news = results
            _last_fetch_time = now
        else:
            print("[get_news] No results; returning stale cache.")
            return _cached_news

        return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/news")
def news():
    try:
        items = get_news()
        return {"status": "success", "news": items, "count": len(items)}
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(exc)}, 500


@app.route("/api/export")
def export():
    try:
        items = get_news()
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
    except Exception as exc:
        return {"status": "error", "message": str(exc)}, 500


@app.route("/api/snapshot", methods=["POST"])
def create_snapshot():
    """Freeze the current articles into a unique shareable URL."""
    try:
        items = get_news()
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
        if len(_snapshots) > 20:
            _snapshots.popitem(last=False)

        base = request.host_url.rstrip("/")
        return {"status": "success", "url": f"{base}/snapshot/{snap_id}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}, 500


@app.route("/snapshot/<snap_id>")
def view_snapshot(snap_id):
    """Serve a frozen snapshot as JSON."""
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
