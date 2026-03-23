"""
Vercel serverless entry point for the QUO VOIP connector.
Self-contained — no local package imports so @vercel/python can build it.
"""
import os
import secrets
from datetime import date as _date

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

_TOKEN: str = os.environ.get("QUO_SERVER_TOKEN", "")
_API_KEY: str = os.environ.get("QUO_API_KEY", "")
_BASE_URL: str = os.environ.get("QUO_BASE_URL", "https://api.quo.voip/v1").rstrip("/")


def _require_bearer(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization: Bearer <token> required")
    if not _TOKEN or not secrets.compare_digest(auth[7:].encode(), _TOKEN.encode()):
        raise HTTPException(status_code=403, detail="Invalid token")


def _quo_get(path: str, params: dict) -> dict:
    resp = requests.get(
        f"{_BASE_URL}/{path.lstrip('/')}",
        params=params,
        headers={"Authorization": f"Bearer {_API_KEY}", "Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


app = FastAPI(title="QUO VOIP", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["Authorization"])


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "quo-voip-mcp"})


@app.get("/call-volume")
async def call_volume(
    request: Request,
    date: str = Query(default=None, description="YYYY-MM-DD (default: today)"),
) -> JSONResponse:
    _require_bearer(request)
    target = date or str(_date.today())
    try:
        data = _quo_get("/calls", {"from_date": f"{target}T00:00:00", "to_date": f"{target}T23:59:59", "page_size": 1})
        return JSONResponse({"date": target, "call_volume": data.get("total")})
    except requests.HTTPError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
