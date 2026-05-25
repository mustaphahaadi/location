"""Minimal Vercel Blob client (stdlib only) for storing locations JSON."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

BLOB_API = "https://blob.vercel-storage.com"
BLOB_API_VERSION = "10"
BLOB_PATHNAME = "location-consent/locations.json"


def blob_configured() -> bool:
    return bool(os.getenv("BLOB_READ_WRITE_TOKEN", "").strip())


def _token() -> str:
    token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN is not set")
    return token


def _request(method: str, url: str, *, data: bytes | None = None, headers: dict | None = None) -> dict:
    hdrs = dict(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Blob API {exc.code}: {detail}") from exc


def blob_put(data: bytes) -> dict:
    qs = urllib.parse.urlencode({"pathname": BLOB_PATHNAME})
    return _request(
        "PUT",
        f"{BLOB_API}/?{qs}",
        data=data,
        headers={
            "authorization": f"Bearer {_token()}",
            "x-api-version": BLOB_API_VERSION,
            "x-content-type": "application/json",
            "x-allow-overwrite": "1",
            "access": "public",
        },
    )


def _blob_list() -> list[dict]:
    qs = urllib.parse.urlencode({"prefix": "location-consent/", "limit": "100"})
    result = _request(
        "GET",
        f"{BLOB_API}/?{qs}",
        headers={"authorization": f"Bearer {_token()}"},
    )
    return result.get("blobs") or []


def _download_url(blob_url: str) -> str:
    qs = urllib.parse.urlencode({"url": blob_url})
    meta = _request(
        "GET",
        f"{BLOB_API}/?{qs}",
        headers={
            "authorization": f"Bearer {_token()}",
            "x-api-version": BLOB_API_VERSION,
        },
    )
    return meta.get("downloadUrl") or blob_url


def blob_get() -> dict | None:
    blobs = _blob_list()
    target = next((b for b in blobs if b.get("pathname") == BLOB_PATHNAME), None)
    if not target:
        return None
    url = target.get("url")
    if not url:
        return None
    download = _download_url(url)
    with urllib.request.urlopen(download, timeout=30) as resp:
        raw = resp.read()
    if not raw:
        return None
    parsed = json.loads(raw.decode())
    return parsed if isinstance(parsed, dict) else None
