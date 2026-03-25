"""
Vercel serverless handler for QUO VOIP connector.
Uses BaseHTTPRequestHandler — the most basic Vercel Python pattern.
"""
import json
import os
import secrets
from datetime import date as _date
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import requests as _requests

_TOKEN: str = os.environ.get("QUO_SERVER_TOKEN", "")
_API_KEY: str = os.environ.get("QUO_API_KEY", "")
_BASE_URL: str = os.environ.get("QUO_BASE_URL", "https://api.quo.voip/v1").rstrip("/")


def _quo_get(path: str, params: dict) -> dict:
    resp = _requests.get(
        f"{_BASE_URL}/{path.lstrip('/')}",
        params=params,
        headers={"Authorization": f"Bearer {_API_KEY}", "Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        # ── /health ──────────────────────────────────────────────────────────
        if parsed.path in ("/health", "/api/index"):
            self._json(200, {"status": "ok", "service": "quo-voip-mcp"})
            return

        # ── /call-volume ─────────────────────────────────────────────────────
        if parsed.path == "/call-volume":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                self._json(401, {"error": "Authorization: Bearer <token> required"})
                return
            if not _TOKEN or not secrets.compare_digest(auth[7:].encode(), _TOKEN.encode()):
                self._json(403, {"error": "Invalid token"})
                return

            target = qs.get("date", [str(_date.today())])[0]
            try:
                data = _quo_get("/calls", {
                    "from_date": f"{target}T00:00:00",
                    "to_date": f"{target}T23:59:59",
                    "page_size": 1,
                })
                self._json(200, {"date": target, "call_volume": data.get("total")})
            except _requests.HTTPError as exc:
                self._json(exc.response.status_code, {"error": exc.response.text})
            except Exception as exc:
                self._json(502, {"error": str(exc)})
            return

        self._json(404, {"error": "Not found"})

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
