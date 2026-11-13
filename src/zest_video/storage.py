"""Private video bucket: signed URLs only, nothing public.

The phone uploads straight to the bucket with a signed PUT URL and the watch
page plays from a short-lived signed GET URL, so video bytes never pass
through this server. Signing is plain AWS Signature V4 (query-string form),
done here in ~60 lines instead of pulling in boto3 for three calls.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx

from .config import Config


@dataclass(frozen=True)
class StoredObject:
    size: int
    etag: str


class Storage(Protocol):
    def put_url(self, key: str, content_type: str, seconds: int) -> str: ...
    def get_url(self, key: str, seconds: int) -> str: ...
    async def head(self, key: str) -> StoredObject | None: ...
    async def delete(self, key: str) -> None: ...


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def presign(
    *,
    method: str,
    endpoint: str,
    bucket: str,
    key: str,
    region: str,
    access_key: str,
    secret_key: str,
    seconds: int,
    path_style: bool = False,
    signed_headers: dict[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """A SigV4 presigned URL for one object and one HTTP method.

    `signed_headers` (e.g. Content-Type on a PUT) become part of the
    signature: the request must send exactly those values or the bucket
    refuses it.
    """
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    parts = urlsplit(endpoint)
    scheme, base_host = parts.scheme or "https", parts.netloc
    if path_style:
        host, uri = base_host, f"/{bucket}/{quote(key, safe='/~')}"
    else:
        host, uri = f"{bucket}.{base_host}", f"/{quote(key, safe='/~')}"

    headers = {"host": host, **{k.lower(): v.strip() for k, v in (signed_headers or {}).items()}}
    header_names = ";".join(sorted(headers))
    scope = f"{day}/{region}/s3/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(seconds),
        "X-Amz-SignedHeaders": header_names,
    }
    canonical_query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items()))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join(
        [method, uri, canonical_query, canonical_headers, header_names, "UNSIGNED-PAYLOAD"]
    )
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signing_key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), "s3"), "aws4_request")
    signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{uri}?{canonical_query}&X-Amz-Signature={signature}"


class S3Storage:
    def __init__(self, cfg: Config, *, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._http = httpx.AsyncClient(transport=transport, timeout=20.0)

    def _url(self, method: str, key: str, seconds: int, headers: dict[str, str] | None = None) -> str:
        c = self.cfg
        return presign(
            method=method,
            endpoint=c.s3_endpoint,
            bucket=c.s3_bucket,
            key=key,
            region=c.s3_region,
            access_key=c.s3_access_key,
            secret_key=c.s3_secret_key,
            seconds=seconds,
            path_style=c.s3_path_style,
            signed_headers=headers,
        )

    def put_url(self, key: str, content_type: str, seconds: int) -> str:
        return self._url("PUT", key, seconds, {"content-type": content_type})

    def get_url(self, key: str, seconds: int) -> str:
        return self._url("GET", key, seconds)

    async def head(self, key: str) -> StoredObject | None:
        resp = await self._http.head(self._url("HEAD", key, 60))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return StoredObject(size=int(resp.headers.get("content-length", "0")), etag=resp.headers.get("etag", "").strip('"'))

    async def delete(self, key: str) -> None:
        resp = await self._http.delete(self._url("DELETE", key, 60))
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()

    async def aclose(self) -> None:
        await self._http.aclose()


class MemoryStorage:
    """Stand-in bucket for tests and for running locally with no bucket configured."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def put_url(self, key: str, content_type: str, seconds: int) -> str:
        return f"https://bucket.invalid/{key}?put=1&expires={seconds}"

    def get_url(self, key: str, seconds: int) -> str:
        return f"https://bucket.invalid/{key}?get=1&expires={seconds}"

    async def head(self, key: str) -> StoredObject | None:
        body = self.objects.get(key)
        if body is None:
            return None
        return StoredObject(size=len(body), etag=hashlib.md5(body, usedforsecurity=False).hexdigest())

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)
