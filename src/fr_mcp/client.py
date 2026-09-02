"""Async HTTP client for the FieldRoutes API.

FieldRoutes is not REST: every call is `POST /api/{entity}/{action}` with
form-encoded params (auth included in the body), and HTTP always returns 200
-- failures show up as `{"success": false, "errorMessage": ...}` in the JSON
body. See CLAUDE.md for the full write-up of these quirks; this module is
where they're encoded so nothing else in the codebase has to think about them.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from typing import Any, Iterable

import httpx

GET_CHUNK_SIZE = 1000


class FieldRoutesError(Exception):
    """A FieldRoutes call failed: bad HTTP, a non-JSON body, or `success: false`."""

    def __init__(
        self,
        message: str,
        *,
        entity: str | None = None,
        action: str | None = None,
        response: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.entity = entity
        self.action = action
        self.response = response


class RateLimiter:
    """Sliding-window limiter: at most `limit` calls in any rolling `window` seconds.

    FieldRoutes caps an office at 60 requests/minute; we self-limit under that
    (55/min by default) so a burst from this server alone never trips it, and
    the office still has headroom for FieldRoutes' own UI and other integrations.
    """

    def __init__(self, limit: int = 55, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.window:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                sleep_for = self.window - (now - self._calls[0])
                await asyncio.sleep(max(sleep_for, 0.01))


def encode_form(params: dict[str, Any]) -> list[tuple[str, str]]:
    """Encode a param dict into FieldRoutes' form-encoding conventions.

    - `None` values are dropped (omit the param rather than send it empty).
    - `bool` becomes `"1"`/`"0"` -- FieldRoutes booleans are integers on the wire.
    - `list`/`tuple` becomes repeated `key[]=value` pairs (PHP-style arrays),
      e.g. `customerIDs=[1, 2]` -> `customerIDs[]=1&customerIDs[]=2`.
    - `dict` is JSON-encoded as a single string value -- this is how search
      filter objects like `{"operator": ">", "value": "0"}` are sent.
    - Everything else is stringified.
    """
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            pairs.append((key, "1" if value else "0"))
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, (dict, list)):
                    pairs.append((f"{key}[]", json.dumps(item)))
                else:
                    pairs.append((f"{key}[]", str(item)))
        elif isinstance(value, dict):
            pairs.append((key, json.dumps(value)))
        else:
            pairs.append((key, str(value)))
    return pairs


def normalize_search_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    """Turn plain lists in search filters into `{"operator": "IN", "value": [...]}` objects.

    `get` takes ID arrays PHP-style (`customerIDs[]=1&customerIDs[]=2`), but
    `search` params are either a scalar or a JSON query object, so a list
    there has to become an IN filter. Dicts (explicit operator objects) and
    scalars pass through untouched.
    """
    out: dict[str, Any] = {}
    for key, value in (filters or {}).items():
        if isinstance(value, (list, tuple)):
            out[key] = {"operator": "IN", "value": list(value)}
        else:
            out[key] = value
    return out


class FieldRoutesClient:
    """Thin async client for `POST /api/{entity}/{action}`."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        subdomain: str | None = None,
        auth_key: str | None = None,
        auth_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 3,
    ):
        base_url = base_url or os.environ.get("FR_BASE_URL")
        if not base_url:
            subdomain = subdomain or os.environ.get("FR_SUBDOMAIN")
            if not subdomain:
                raise ValueError("Set FR_BASE_URL or FR_SUBDOMAIN")
            base_url = f"https://{subdomain}.fieldroutes.com/api"
        self.base_url = base_url.rstrip("/")

        self.auth_key = auth_key or os.environ.get("FR_AUTH_KEY")
        self.auth_token = auth_token or os.environ.get("FR_AUTH_TOKEN")
        if not self.auth_key or not self.auth_token:
            raise ValueError("Set FR_AUTH_KEY and FR_AUTH_TOKEN")

        self.max_retries = max_retries
        self.rate_limiter = rate_limiter or RateLimiter()
        self._http = httpx.AsyncClient(transport=transport, timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "FieldRoutesClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- low level -----------------------------------------------------

    async def call(self, entity: str, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST `{entity}/{action}` with form-encoded params; return the parsed JSON body.

        Raises `FieldRoutesError` on a transport failure, a non-JSON body, or
        `success: false` in the body (HTTP status is 200 either way).
        """
        body_params = dict(params or {})
        body_params["authenticationKey"] = self.auth_key
        body_params["authenticationToken"] = self.auth_token
        body = encode_form(body_params)

        url = f"{self.base_url}/{entity}/{action}"
        last_exc: Exception | None = None

        for attempt in range(self.max_retries):
            await self.rate_limiter.acquire()
            try:
                resp = await self._http.post(url, data=body)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt + 1 < self.max_retries:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise FieldRoutesError(
                    f"{entity}/{action}: transport error: {exc}", entity=entity, action=action
                ) from exc

            if resp.status_code >= 500 and attempt + 1 < self.max_retries:
                await asyncio.sleep(0.5 * (2**attempt))
                continue

            try:
                data = resp.json()
            except ValueError as exc:
                raise FieldRoutesError(
                    f"{entity}/{action}: non-JSON response (HTTP {resp.status_code}): {resp.text[:500]}",
                    entity=entity,
                    action=action,
                ) from exc

            if not data.get("success", False):
                raise FieldRoutesError(
                    f"{entity}/{action}: {data.get('errorMessage') or 'request failed'}",
                    entity=entity,
                    action=action,
                    response=data,
                )
            return data

        raise FieldRoutesError(f"{entity}/{action}: retries exhausted", entity=entity, action=action) from last_exc

    # -- search + get pagination ----------------------------------------

    async def search(
        self, entity: str, filters: dict[str, Any] | None = None, *, include_data: bool = False
    ) -> dict[str, Any]:
        """`{entity}/search`: returns the raw body, including `{entity}IDs` and,

        with `include_data=True`, the first 1000 resolved records inline
        (plus `{entity}IDsNoDataExported` for anything past that cutoff).
        """
        params = normalize_search_filters(filters)
        if include_data:
            params["includeData"] = 1
        return await self.call(entity, "search", params)

    @staticmethod
    def extract_ids(entity: str, search_response: dict[str, Any]) -> list[int]:
        return list(search_response.get(f"{entity}IDs", []) or [])

    async def get(
        self, entity: str, ids: Iterable[int], *, extra_params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """`{entity}/get`: fetch full records for `ids`, chunked at 1000 per FieldRoutes' cap."""
        ids = [i for i in ids]
        id_field = f"{entity}IDs"
        records: list[dict[str, Any]] = []
        for i in range(0, len(ids), GET_CHUNK_SIZE):
            chunk = ids[i : i + GET_CHUNK_SIZE]
            if not chunk:
                continue
            params = {id_field: chunk, **(extra_params or {})}
            data = await self.call(entity, "get", params)
            records.extend(self.extract_records(entity, data))
        return records

    @staticmethod
    def extract_records(entity: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull the record list out of a `get` response.

        FieldRoutes nests bulk-get results under an entity-named key; fall back
        to the first list-valued key in the body (other than metadata) so a
        naming mismatch degrades gracefully instead of silently dropping data.
        """
        records = data.get(entity)
        if isinstance(records, list):
            return records
        for key, value in data.items():
            if key in ("success", "errorMessage", "idName", "propertyName", "count"):
                continue
            if key.endswith("IDs") or "NoDataExported" in key or not isinstance(value, list):
                continue
            return value
        return []

    async def search_and_get(
        self,
        entity: str,
        filters: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        extra_get_params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search then hydrate: the common two-step read used by every curated tool."""
        search_response = await self.search(entity, filters)
        ids = self.extract_ids(entity, search_response)
        if limit is not None:
            ids = ids[:limit]
        return await self.get(entity, ids, extra_params=extra_get_params)
