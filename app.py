"""
Vietnam Metro & Railway News — Flask backend
=============================================
Flask làm lightweight RSS proxy: browser gọi /proxy?url=<feed>,
server fetch XML từ nguồn báo và trả về. Không cần external CORS
proxy (allorigins.win, v.v.) — tránh phụ thuộc dịch vụ bên ngoài
và tránh bị AdBlock chặn trong browser người dùng.
"""

import json as _json
import os
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from flask import Flask, Response, render_template, request

app = Flask(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Chỉ cho phép fetch từ các nguồn báo đã whitelist
_ALLOWED_HOSTS = {
    "vnexpress.net",
    "e.vnexpress.net",
    "tuoitre.vn",
    "thanhnien.vn",
    "vtv.vn",
    "dantri.com.vn",
}

_snapshots: "OrderedDict[str, dict]" = OrderedDict()
_SNAPSHOT_CAP = 20


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


@app.route("/proxy")
def proxy():
    """
    Lightweight RSS proxy. Browser gọi /proxy?url=<rss_feed_url>,
    server fetch XML và trả về — cùng origin nên không cần CORS.
    Whitelist host để tránh bị dùng làm open proxy.
    """
    url = request.args.get("url", "").strip()
    if not url.startswith("https://"):
        return Response("invalid url", status=400)

    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lstrip("www.")
    if host not in _ALLOWED_HOSTS:
        return Response("host not allowed", status=403)

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        })
        with urllib.request.urlopen(req, timeout=12) as r:
            content = r.read().decode("utf-8", errors="replace")
        return Response(content, mimetype="text/xml; charset=utf-8")
    except Exception as e:
        print(f"[proxy] {url[:80]}: {e}")
        return Response(f"fetch error: {e}", status=502)


@app.route("/api/snapshot", methods=["POST"])
def create_snapshot():
    """Nhận danh sách articles từ browser, lưu thành snapshot URL."""
    try:
        articles = request.get_json(force=True) or []
        if not isinstance(articles, list) or not articles:
            return {"status": "error", "message": "No articles provided."}, 400

        snap_id = uuid.uuid4().hex
        payload = {
            "source":      "Vietnam Metro & Railway News",
            "snapshot_id": snap_id,
            "created_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total":       len(articles),
            "vi_count":    sum(1 for a in articles if a.get("lang") == "vi"),
            "en_count":    sum(1 for a in articles if a.get("lang") == "en"),
            "articles":    articles,
        }
        _snapshots[snap_id] = payload
        if len(_snapshots) > _SNAPSHOT_CAP:
            _snapshots.popitem(last=False)

        base = request.host_url.rstrip("/")
        return {"status": "success", "url": f"{base}/snapshot/{snap_id}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}, 500


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
