"""Vercel Blob storage — one JSON blob per location ref (works across serverless invocations)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

BLOB_API = "https://blob.vercel-storage.com"
BLOB_API_VERSION = "10"
BLOB_PREFIX = "location-consent/entries/"


def blob_configured() -> bool:
    return bool(os.getenv("BLOB_READ_WRITE_TOKEN", "").strip())


def _token() -> str:
    token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN is not set — connect Blob in Vercel → Storage")
    return token


def _auth_headers() -> dict[str, str]:
    return {
        "authorization": f"Bearer {_token()}",
        "x-api-version": BLOB_API_VERSION,
    }


def _put_headers(*, content_type: str = "application/json") -> dict[str, str]:
    return {
        **_auth_headers(),
        "access": "public",
        "x-content-type": content_type,
        "x-allow-overwrite": "1",
    }


def _request(method: str, url: str, *, data: bytes | None = None, headers: dict | None = None) -> dict:
    hdrs = dict(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:800]
        raise RuntimeError(f"Blob API {exc.code}: {detail}") from exc


def _pathname_for_ref(ref: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in ref.strip())
    return f"{BLOB_PREFIX}{safe or 'unknown'}.json"


def blob_put_entry(ref: str, entry: dict) -> dict:
    pathname = _pathname_for_ref(ref)
    qs = urllib.parse.urlencode({"pathname": pathname})
    return _request(
        "PUT",
        f"{BLOB_API}/?{qs}",
        data=json.dumps(entry).encode(),
        headers=_put_headers(),
    )


def blob_delete_entry(ref: str) -> None:
    pathname = _pathname_for_ref(ref)
    blobs = _blob_list()
    target = next((b for b in blobs if b.get("pathname") == pathname), None)
    if not target or not target.get("url"):
        return
    _request(
        "POST",
        f"{BLOB_API}/delete",
        data=json.dumps({"urls": [target["url"]]}).encode(),
        headers={**_auth_headers(), "content-type": "application/json"},
    )


def _blob_list() -> list[dict]:
    qs = urllib.parse.urlencode({
        "prefix": BLOB_PREFIX,
        "limit": "1000",
        "mode": "expanded",
    })
    result = _request("GET", f"{BLOB_API}/?{qs}", headers=_auth_headers())
    return result.get("blobs") or []


def _fetch_bytes(url: str) -> bytes:
    meta = _request(
        "GET",
        f"{BLOB_API}/?{urllib.parse.urlencode({'url': url})}",
        headers=_auth_headers(),
    )
    download = meta.get("downloadUrl") or url
    with urllib.request.urlopen(download, timeout=30) as resp:
        return resp.read()


def blob_load_all() -> dict[str, dict]:
    store: dict[str, dict] = {}
    for blob in _blob_list():
        pathname = blob.get("pathname") or ""
        if not pathname.startswith(BLOB_PREFIX) or not pathname.endswith(".json"):
            continue
        url = blob.get("url")
        if not url:
            continue
        try:
            raw = _fetch_bytes(url)
            entry = json.loads(raw.decode())
            if not isinstance(entry, dict):
                continue
            ref = entry.get("ref") or pathname[len(BLOB_PREFIX) : -5]
            store[ref] = entry
        except Exception as exc:
            print(f"[warn] Could not read blob {pathname}: {exc}")
    return store
