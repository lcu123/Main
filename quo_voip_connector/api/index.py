"""
Vercel serverless entry point for the QUO VOIP connector.
Exposes /health and /call-volume as lightweight REST endpoints.
"""
import os
import secrets
from datetime import date as _date

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from quo_voip import QUOConfig
from quo_voip.client import QUOClient

_TOKEN: str = os.environ.get("QUO_SERVER_TOKEN", "")


def _require_bearer(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization: Bearer <token> required")
    if not _TOKEN or not secrets.compare_digest(auth[7:].encode(), _TOKEN.encode()):
        raise HTTPException(status_code=403, detail="Invalid token")


app = FastAPI(title="QUO VOIP", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["Authorization"])

_client = QUOClient(QUOConfig.from_env())


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
        data = _client.get(
            "/calls",
            params={"from_date": f"{target}T00:00:00", "to_date": f"{target}T23:59:59", "page_size": 1},
        )
        total = data.get("total") if isinstance(data, dict) else None
        return JSONResponse({"date": target, "call_volume": total})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
