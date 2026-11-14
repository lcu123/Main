"""Private video bucket: signed URLs only, nothing public.

The phone uploads straight to the bucket with a signed PUT URL and the watch
page plays from a short-lived signed GET URL, so video bytes never pass
through this server. Signing is plain AWS Signature V4 (query-string form),
done here in ~60 lines instead of pulling in boto3 for three calls.
"""

from __future__ import annotations

import hashlib
import hmac
import re
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
    # What the answer called itself ("video/mp4" for a file a phone uploaded). Empty when
    # the storage did not say. It is how a page from whatever stands in front of the
    # bucket is told from the bucket's own answer about a file (`videos.file_check`).
    content_type: str = ""


def plain_etag(value: str) -> str:
    """An ETag header as the bare value: no quotes, and no `W/` in front. Some things
    that stand between this server and the bucket mark a file's ETag as "weak" that way.
    It is still the same file."""
    value = value.strip()
    if value[:2] in ("W/", "w/"):
        value = value[2:]
    return value.strip('"')


def words_not_a_file(content_type: str) -> bool:
    """Is an answer of this type a page of words (a web page, an error written out) and
    not a file? The bucket never answers a question about a video with one. Whatever
    stands in front of it does, on a bad minute, sometimes with a 200 on it."""
    kind = content_type.split(";")[0].strip().lower()
    return kind.startswith("text/") or kind in ("application/json", "application/xml", "application/xhtml+xml")


# What a bucket can say when asked about one object at start-up (`Storage.ask`), or
# whether it holds anything at all under a prefix (`Storage.look`).
FOUND = "found"  # the object is there
MISSING = "missing"  # the bucket itself took our keys and said, in its own words, that it has no such object
UNKNOWN = "unknown"  # it could not be reached, or gave an answer that settles nothing
# Answers that mean the settings are wrong, not that the bucket is having a bad minute.
# Each is the bucket's own error code. The words are for the person reading the log.
REFUSALS = {
    "NoSuchBucket": "The storage has no bucket with the name in VIDEO_S3_BUCKET.",
    "InvalidAccessKeyId": "The storage does not know the key in VIDEO_S3_ACCESS_KEY_ID.",
    "SignatureDoesNotMatch": (
        "The storage refused our signature: VIDEO_S3_SECRET_ACCESS_KEY does not go with "
        "VIDEO_S3_ACCESS_KEY_ID, or VIDEO_S3_REGION is wrong."
    ),
    "AuthorizationQueryParametersError": "The storage refused VIDEO_S3_REGION: the bucket is in another region.",
    "PermanentRedirect": "The bucket is not at the address in VIDEO_S3_ENDPOINT.",
}


class Storage(Protocol):
    def put_url(self, key: str, content_type: str, seconds: int) -> str: ...
    def get_url(self, key: str, seconds: int) -> str: ...
    async def head(self, key: str) -> StoredObject | None: ...
    async def read_range(self, key: str, start: int, length: int) -> bytes | None: ...
    async def delete(self, key: str) -> None: ...
    async def ask(self, key: str) -> str: ...
    async def look(self, prefix: str) -> str: ...


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
    query: dict[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """A SigV4 presigned URL for one object and one HTTP method.

    `signed_headers` (e.g. Content-Type on a PUT) become part of the
    signature: the request must send exactly those values or the bucket
    refuses it. So does `query`: the question a list call asks (with an
    empty `key`, the link is for the bucket itself).
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
        **(query or {}),
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

    def _url(
        self, method: str, key: str, seconds: int, headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
    ) -> str:
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
            query=query,
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
        return StoredObject(
            size=int(resp.headers.get("content-length", "0")),
            etag=plain_etag(resp.headers.get("etag", "")),
            content_type=resp.headers.get("content-type", "").split(";")[0].strip().lower(),
        )

    async def read_range(self, key: str, start: int, length: int) -> bytes | None:
        """A slice of an object (for a look at a video's header, never the whole file)."""
        end = max(start, start + length - 1)
        resp = await self._http.get(self._url("GET", key, 60), headers={"Range": f"bytes={start}-{end}"})
        if resp.status_code in (404, 416):
            return None
        resp.raise_for_status()
        return resp.content[:length]

    async def delete(self, key: str) -> None:
        resp = await self._http.delete(self._url("DELETE", key, 60))
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()

    async def ask(self, key: str) -> str:
        """One question for the start-up check: is this object there?

        Returns FOUND, MISSING, UNKNOWN, or one of the REFUSALS codes. It reads one byte
        with a GET, not a HEAD, because only a GET's answer carries the bucket's error
        code, and the code is what tells "no such object" from "no such bucket" (both
        are a 404). Never raises: a bucket that cannot be reached is UNKNOWN."""
        try:
            resp = await self._http.get(self._url("GET", key, 60), headers={"Range": "bytes=0-0"})
        except httpx.HTTPError:
            return UNKNOWN
        if resp.status_code in (200, 206):
            # A page that says "we will be right back" with a 200 is not the file.
            return UNKNOWN if words_not_a_file(resp.headers.get("content-type", "")) else FOUND
        found = re.search(r"<Code>\s*([A-Za-z]+)\s*</Code>", resp.text[:2000])
        code = found.group(1) if found else ""
        if code in REFUSALS:
            return code
        # "Not there" is only believed in the bucket's own words. A 404 with no code in it
        # is somebody else's page (whatever stands in front of the bucket, having a bad
        # minute), and it says nothing about the file.
        # 416: the object is there and empty, which no video is. Count it as not there.
        if (resp.status_code, code) in ((404, "NoSuchKey"), (416, "InvalidRange")):
            return MISSING
        return UNKNOWN

    async def look(self, prefix: str) -> str:
        """One more question for the start-up check: does the bucket hold anything at all
        under this prefix? FOUND, MISSING (it answered, and holds nothing there) or UNKNOWN.

        A list call, limited to one key. Only a proper list settles anything. Every other
        answer is UNKNOWN, a refusal included: nothing else in this server lists the
        bucket, so a refusal here may be this one call's own fault, and it must never be
        what stops a start. Never raises."""
        question = {"list-type": "2", "prefix": prefix, "max-keys": "1"}
        try:
            resp = await self._http.get(self._url("GET", "", 60, query=question))
        except httpx.HTTPError:
            return UNKNOWN
        text = resp.text[:4000]
        if resp.status_code != 200 or "<ListBucketResult" not in text:
            return UNKNOWN
        keys = re.findall(r"<Key>([^<]+)</Key>", text)
        if any(key.startswith(prefix) for key in keys):
            return FOUND
        return UNKNOWN if keys else MISSING  # keys from somewhere else: it did not keep to the question

    async def aclose(self) -> None:
        await self._http.aclose()


class MemoryStorage:
    """Stand-in bucket for tests and for running locally with no bucket configured.
    Everything in it is gone when the process stops, so a real deployment never gets
    one (config.check_start)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def ask(self, key: str) -> str:
        return FOUND if key in self.objects else MISSING

    async def look(self, prefix: str) -> str:
        return FOUND if any(key.startswith(prefix) for key in self.objects) else MISSING

    def put_url(self, key: str, content_type: str, seconds: int) -> str:
        return f"https://bucket.invalid/{key}?put=1&expires={seconds}"

    def get_url(self, key: str, seconds: int) -> str:
        return f"https://bucket.invalid/{key}?get=1&expires={seconds}"

    async def head(self, key: str) -> StoredObject | None:
        body = self.objects.get(key)
        if body is None:
            return None
        return StoredObject(size=len(body), etag=hashlib.md5(body, usedforsecurity=False).hexdigest())

    async def read_range(self, key: str, start: int, length: int) -> bytes | None:
        body = self.objects.get(key)
        return None if body is None else body[start:start + length]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)
