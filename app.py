import json as _json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict
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

# In-memory snapshot store: uuid -> payload (capped at 20 entries)
_snapshots = OrderedDict()

EN_QUERIES = [
    "Ho Chi Minh City metro",
    "Hanoi metro line",
    "vietnam high speed rail",
    "vietnam railway infrastructure",
]

MAX_VI      = 20
MAX_EN      = 10
MAX_AGE_DAYS = 30
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ---------------------------------------------------------------------------
# HTTP fetch — urllib first (works on cloud), curl fallback (local Anaconda)
# ---------------------------------------------------------------------------

def _get_url(url):
    """Return the raw text of a URL. Tries urllib, falls back to curl."""
    # Try urllib (production / any Python with working SSL)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    # Fallback: curl subprocess (handles broken SSL in local Anaconda 3.8)
    try:
        proc = subprocess.run(
            ["curl", "-sL", url, "--max-time", "15", "-H", f"User-Agent: {_UA}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
    except Exception:
        pass
    return ""


def fetch_rss(query, lang="en"):
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

def parse_rss(xml_text, lang="en"):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items = []
    for item in root.findall(".//item"):
        title  = (item.findtext("title")   or "").strip()
        link   = (item.findtext("link")    or "").strip()
        pub    = (item.findtext("pubDate") or "").strip()
        source = ""
        src_el = item.find("source")
        if src_el is not None:
            source = src_el.text or ""

        dt_obj, date_str, age_label = None, "", ""
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
                "source":   source.strip(),
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

def age_ago(dt):
    try:
        s = int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
        if s < 3600:
            m = max(1, s // 60);  return f"{m} minute{'s' if m!=1 else ''} ago"
        if s < 86400:
            h = s // 3600;        return f"{h} hour{'s' if h!=1 else ''} ago"
        d = s // 86400;           return f"{d} day{'s' if d!=1 else''} ago"
    except Exception:
        return ""


def categorize(title):
    t = title.lower()
    if any(k in t for k in ["metro","mrt","ben thanh","suoi tien","nhon","cat linh"]):
        return "Metro"
    if any(k in t for k in [
        "railway","railroad","train","duong sat","ga "," ga",
        "high-speed","high speed","toc do cao","speed rail","hsr",
        "bac nam","lien vung","thong nhat","lao cai","hai phong",
    ]):
        return "Railway"
    return "Transport"


def locate(title):
    t = title.lower()
    if any(k in t for k in ["ho chi minh","hcmc","saigon","sai gon"]):
        return "Ho Chi Minh City"
    if any(k in t for k in ["hanoi","ha noi","cat linh","nhon"]):
        return "Hanoi"
    if any(k in t for k in ["da nang","danang"]):
        return "Da Nang"
    return "Vietnam"


def _collect(queries, lang, limit, cutoff, seen):
    """Fetch ALL queries (so every topic gets a chance), then return the newest `limit` articles."""
    results = []
    for query in queries:
        for a in fetch_rss(query, lang=lang):
            key = re.sub(r"\s+", " ", a["title"].lower()[:60])
            if key in seen:
                continue
            dt = a.get("dt")
            if dt and dt.astimezone(timezone.utc) < cutoff:
                continue
            seen.add(key)
            results.append(a)
    # Sort newest-first, then cap
    results.sort(key=sort_key, reverse=True)
    return results[:limit]


def sort_key(a):
    dt = a.get("dt")
    return dt.astimezone(timezone.utc) if dt else datetime.min.replace(tzinfo=timezone.utc)


def get_news():
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    seen   = set()
    vi = _collect(VI_QUERIES, "vi", MAX_VI, cutoff, seen)
    en = _collect(EN_QUERIES, "en", MAX_EN, cutoff, seen)
    results = vi + en
    for a in results:
        a.pop("dt", None)
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
    """Freeze the current 30 articles into a unique shareable URL."""
    try:
        items = get_news()
        snap_id = uuid.uuid4().hex
        payload = {
            "source":     "Vietnam Metro & Railway News",
            "snapshot_id": snap_id,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total":      len(items),
            "vi_count":   sum(1 for a in items if a.get("lang") == "vi"),
            "en_count":   sum(1 for a in items if a.get("lang") == "en"),
            "articles":   items,
        }
        # Keep only last 20 snapshots in memory
        _snapshots[snap_id] = payload
        if len(_snapshots) > 20:
            _snapshots.popitem(last=False)

        base = request.host_url.rstrip("/")
        return {"status": "success", "url": f"{base}/snapshot/{snap_id}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}, 500


@app.route("/snapshot/<snap_id>")
def view_snapshot(snap_id):
    """Serve a frozen snapshot as JSON — importable by any external service."""
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
