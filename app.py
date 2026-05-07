"""
Vietnam Metro & Railway News — Flask backend
=============================================
Server chỉ serve HTML. Toàn bộ RSS fetching chạy ở browser (client-side)
qua CORS proxy allorigins.win — tránh bị block bởi Render's server IP.
"""

import json as _json
import os
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from flask import Flask, Response, render_template, request

app = Flask(__name__)

_snapshots: "OrderedDict[str, dict]" = OrderedDict()
_SNAPSHOT_CAP = 20


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


@app.route("/api/snapshot", methods=["POST"])
def create_snapshot():
    """Freeze a list of articles (sent by browser) into a shareable URL."""
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
