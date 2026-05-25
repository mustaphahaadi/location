#!/usr/bin/env python3
"""
Location consent service — generate shareable links, collect GPS with customer consent.

Run:  ./run.sh
      (starts uvicorn + ngrok tunnel for HTTPS public links)

Operator UI:  /dashboard
Customer page: /locate?service=...&callback=...&ref=...
"""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

load_dotenv(override=True)

ROOT = Path(__file__).parent
CONSENT_HTML = ROOT / "location_consent.html"
DASHBOARD_HTML = ROOT / "operator_dashboard.html"
DATA_FILE = ROOT / "locations.json"

PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")
TUNNEL = os.getenv("TUNNEL", "cloudflare").lower()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
LOCATION_STORAGE = os.getenv("LOCATION_STORAGE", "auto").lower()
IS_SERVERLESS = bool(
    os.getenv("VERCEL") or os.getenv("VERCEL_ENV") or os.getenv("AWS_LAMBDA_FUNCTION_NAME")
)


def detect_public_base_url() -> str:
    """Infer HTTPS base URL from common hosting platform env vars."""
    candidates: list[str] = []
    for key in (
        "PUBLIC_BASE_URL",
        "VERCEL_PROJECT_PRODUCTION_URL",
        "RENDER_EXTERNAL_URL",
        "RAILWAY_STATIC_URL",
        "RAILWAY_PUBLIC_DOMAIN",
        "VESSL_SERVICE_URL",
        "VERCEL_URL",
    ):
        val = os.getenv(key, "").strip().rstrip("/")
        if val:
            candidates.append(val)
    fly = os.getenv("FLY_APP_NAME", "").strip()
    if fly:
        candidates.append(f"{fly}.fly.dev")

    for raw in candidates:
        if not raw:
            continue
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw.rstrip("/")
        return f"https://{raw}"
    return ""


_detected_public = detect_public_base_url()
if _detected_public and not PUBLIC_BASE_URL:
    PUBLIC_BASE_URL = _detected_public

_use_tunnel_env = os.getenv("USE_TUNNEL", os.getenv("USE_NGROK", "")).strip().lower()
if _use_tunnel_env:
    USE_TUNNEL = _use_tunnel_env in ("1", "true", "yes")
else:
    # Deployed with a stable public URL: no local cloudflared/ngrok process.
    USE_TUNNEL = not bool(PUBLIC_BASE_URL)
NGROK_BIN = os.getenv("NGROK_BIN", "ngrok")
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "").strip()
CLOUDFLARED_BIN = os.getenv("CLOUDFLARED_BIN", str(ROOT / "bin" / "cloudflared") if (ROOT / "bin" / "cloudflared").exists() else "cloudflared")

location_store: dict[str, dict] = {}
_tunnel_process: subprocess.Popen | None = None
CF_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
KV_STORE_KEY = "location_consent:store"
_redis_client = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def kv_configured() -> bool:
    return bool(os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL"))


def blob_configured() -> bool:
    from blob_storage import blob_configured as _blob_ok

    return _blob_ok()


def use_kv_storage() -> bool:
    if LOCATION_STORAGE == "kv":
        return kv_configured()
    if LOCATION_STORAGE in ("memory", "file", "blob"):
        return False
    return kv_configured()


def use_blob_storage() -> bool:
    if LOCATION_STORAGE == "blob":
        return blob_configured()
    if LOCATION_STORAGE in ("memory", "file", "kv"):
        return False
    # auto: prefer Blob on Vercel (native storage), else Upstash from Marketplace
    if blob_configured():
        return True
    return False


def use_file_storage() -> bool:
    """JSON file when developing locally with a tunnel."""
    if LOCATION_STORAGE == "file":
        return True
    if LOCATION_STORAGE in ("memory", "kv", "blob"):
        return False
    return USE_TUNNEL and not use_kv_storage() and not use_blob_storage()


def storage_kind() -> str:
    if use_file_storage():
        return "file"
    if use_kv_storage():
        return "kv"
    if use_blob_storage():
        return "blob"
    return "memory"


def _get_redis():
    global _redis_client
    if _redis_client is None:
        from upstash_redis import Redis

        _redis_client = Redis.from_env()
    return _redis_client


def reload_store() -> None:
    global location_store
    kind = storage_kind()
    if kind == "file":
        if DATA_FILE.exists():
            try:
                raw = json.loads(DATA_FILE.read_text())
                if isinstance(raw, dict):
                    location_store = raw
                elif isinstance(raw, list):
                    location_store = {e["ref"]: e for e in raw if e.get("ref")}
            except (json.JSONDecodeError, OSError) as exc:
                print(f"[warn] Could not load {DATA_FILE.name}: {exc}")
        return
    if kind == "kv":
        try:
            raw = _get_redis().get(KV_STORE_KEY)
            if not raw:
                location_store = {}
                return
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            location_store = parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            print(f"[warn] Could not load KV store: {exc}")
            location_store = {}
        return
    if kind == "blob":
        try:
            from blob_storage import blob_load_all

            location_store = blob_load_all()
        except Exception as exc:
            print(f"[warn] Could not load Blob store: {exc}")
            location_store = {}


def persist_entry(ref: str, entry: dict) -> str | None:
    """Save one location. Returns an error message on failure, else None."""
    kind = storage_kind()
    try:
        if kind == "file":
            DATA_FILE.write_text(json.dumps(location_store, indent=2))
        elif kind == "kv":
            _get_redis().set(KV_STORE_KEY, json.dumps(location_store))
        elif kind == "blob":
            from blob_storage import blob_put_entry

            blob_put_entry(ref, entry)
        return None
    except Exception as exc:
        msg = f"{kind} save failed: {exc}"
        print(f"[warn] persist_entry: {msg}")
        return msg


def persist_store() -> None:
    kind = storage_kind()
    try:
        if kind == "file":
            DATA_FILE.write_text(json.dumps(location_store, indent=2))
        elif kind == "kv":
            _get_redis().set(KV_STORE_KEY, json.dumps(location_store))
        elif kind == "blob":
            from blob_storage import blob_put_entry

            for ref, entry in location_store.items():
                blob_put_entry(ref, entry)
    except Exception as exc:
        print(f"[warn] persist_store ({kind}): {exc}")


def delete_entry(ref: str) -> None:
    if ref in location_store:
        del location_store[ref]
    kind = storage_kind()
    if kind == "file":
        persist_store()
    elif kind == "kv":
        persist_store()
    elif kind == "blob":
        try:
            from blob_storage import blob_delete_entry

            blob_delete_entry(ref)
        except Exception as exc:
            print(f"[warn] blob delete: {exc}")


def load_store() -> None:
    reload_store()
    kind = storage_kind()
    if kind == "kv":
        print("[storage] Upstash Redis (Marketplace).")
    elif kind == "blob":
        print("[storage] Vercel Blob store.")
    elif kind == "file":
        print(f"[storage] Local file {DATA_FILE.name}.")
    else:
        print("[storage] In-memory cache (resets when the process restarts).")


def save_store() -> None:
    if storage_kind() == "memory":
        return
    persist_store()


def fetch_ngrok_public_url() -> str | None:
    """Read HTTPS public URL from ngrok local agent API."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None

    tunnels = data.get("tunnels", [])
    https_url = next((t.get("public_url") for t in tunnels if t.get("proto") == "https"), None)
    if https_url:
        return https_url.rstrip("/")
    if tunnels:
        return tunnels[0].get("public_url", "").rstrip("/") or None
    return None


def ngrok_token_ok() -> bool:
    return bool(NGROK_AUTHTOKEN) and NGROK_AUTHTOKEN != "your_token_here" and len(NGROK_AUTHTOKEN) >= 20


def configure_ngrok_auth() -> bool:
    if not ngrok_token_ok():
        print("[ngrok] ERROR: NGROK_AUTHTOKEN is missing or still the placeholder.")
        print("         1. Open .env and paste your token from:")
        print("            https://dashboard.ngrok.com/get-started/your-authtoken")
        print("         2. Save the file (Ctrl+S), then restart ./run.sh")
        return False

    result = subprocess.run(
        [NGROK_BIN, "config", "add-authtoken", NGROK_AUTHTOKEN],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[ngrok] Could not save authtoken: {result.stderr.strip() or result.stdout.strip()}")
        return False
    return True


def start_ngrok(port: int) -> str | None:
    global _tunnel_process, PUBLIC_BASE_URL

    if not configure_ngrok_auth():
        return None

    existing = fetch_ngrok_public_url()
    if existing:
        PUBLIC_BASE_URL = existing
        print(f"[ngrok] Using existing tunnel: {PUBLIC_BASE_URL}")
        print("[ngrok] Note: free ngrok shows a warning page — set TUNNEL=cloudflare in .env to skip it.")
        return PUBLIC_BASE_URL

    env = os.environ.copy()
    env["NGROK_AUTHTOKEN"] = NGROK_AUTHTOKEN

    try:
        _tunnel_process = subprocess.Popen(
            [NGROK_BIN, "http", str(port), "--log=stderr"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        print("[ngrok] Not found. Install: https://ngrok.com/download")
        print("         Or set TUNNEL=cloudflare in .env (no warning page, no token needed).")
        return None

    atexit.register(stop_tunnel)

    for _ in range(60):
        time.sleep(0.5)
        if _tunnel_process.poll() is not None:
            err = (_tunnel_process.stderr.read() or b"").decode(errors="replace")
            print("[ngrok] Failed to start tunnel:")
            if "ERR_NGROK_105" in err or "does not look like a proper ngrok authtoken" in err:
                print("         Invalid token format — paste a fresh token from:")
                print("         https://dashboard.ngrok.com/get-started/your-authtoken")
            elif "ERR_NGROK_107" in err or "it is invalid" in err:
                print("         Your authtoken was rejected by ngrok (expired, reset, or revoked).")
                print("         Copy a NEW token from your dashboard and update .env, then restart.")
            else:
                for line in err.strip().splitlines()[-4:]:
                    if "Your authtoken:" not in line:
                        print(f"         {line}")
            return None
        url = fetch_ngrok_public_url()
        if url:
            PUBLIC_BASE_URL = url
            print(f"[ngrok] Public URL: {PUBLIC_BASE_URL}")
            print("[ngrok] Note: free ngrok shows a warning page — set TUNNEL=cloudflare in .env to skip it.")
            return url

    print("[ngrok] Tunnel slow to start — check http://127.0.0.1:4040 or restart ./run.sh")
    return None


def start_cloudflare(port: int) -> str | None:
    global _tunnel_process, PUBLIC_BASE_URL

    bin_path = CLOUDFLARED_BIN
    if not Path(bin_path).exists() and bin_path != "cloudflared":
        bin_path = "cloudflared"

    try:
        _tunnel_process = subprocess.Popen(
            [bin_path, "tunnel", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("[cloudflare] cloudflared not found. Run ./run.sh to download it automatically.")
        return None

    atexit.register(stop_tunnel)

    deadline = time.time() + 45
    while time.time() < deadline:
        if _tunnel_process.poll() is not None:
            print("[cloudflare] Tunnel exited before URL was ready.")
            return None
        line = _tunnel_process.stdout.readline() if _tunnel_process.stdout else ""
        if not line:
            time.sleep(0.2)
            continue
        line = line.rstrip()
        if "trycloudflare.com" in line or "INF" in line:
            print(f"[cloudflare] {line}")
        match = CF_URL_RE.search(line)
        if match:
            PUBLIC_BASE_URL = match.group(0).rstrip("/")
            print(f"[cloudflare] Public URL (no warning page): {PUBLIC_BASE_URL}")
            return PUBLIC_BASE_URL

    print("[cloudflare] Tunnel slow to start — restart ./run.sh")
    return None


def start_tunnel(port: int) -> str | None:
    if TUNNEL == "ngrok":
        return start_ngrok(port)
    if TUNNEL == "cloudflare":
        return start_cloudflare(port)
    print(f"[tunnel] Unknown TUNNEL={TUNNEL!r}. Use 'cloudflare' or 'ngrok'.")
    return None


def stop_tunnel() -> None:
    global _tunnel_process
    if _tunnel_process and _tunnel_process.poll() is None:
        _tunnel_process.terminate()
        try:
            _tunnel_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _tunnel_process.kill()
    _tunnel_process = None


def stop_ngrok() -> None:
    stop_tunnel()


def resolve_base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL

    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}".rstrip("/")

    host = request.headers.get("host", "")
    base = str(request.base_url).rstrip("/")
    https_hosts = (
        ".ngrok-free.app",
        ".trycloudflare.com",
        ".vessl.ai",
        ".onrender.com",
        ".railway.app",
        ".fly.dev",
        ".vercel.app",
    )
    if base.startswith("http://") and any(host.endswith(suffix) for suffix in https_hosts):
        return "https://" + host
    return base


def append_query(url: str, extra: dict[str, str]) -> str:
    parsed = urlparse(url)
    from urllib.parse import parse_qs

    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    qs.update({k: v for k, v in extra.items() if v})
    new_query = urlencode(qs)
    return urlunparse(parsed._replace(query=new_query))


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_store()
    if not IS_SERVERLESS and USE_TUNNEL and not PUBLIC_BASE_URL:
        start_tunnel(PORT)
    if not IS_SERVERLESS:
        print_banner()
    yield
    try:
        save_store()
    except Exception as exc:
        print(f"[warn] save_store on shutdown: {exc}")
    if not IS_SERVERLESS:
        stop_tunnel()


app = FastAPI(title="Location Consent Service", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    reload_store()
    kind = storage_kind()
    warning = None
    if IS_SERVERLESS and kind == "memory":
        warning = "Connect Vercel Blob (Storage) or Upstash Redis — in-memory storage does not work on serverless."
    return {
        "status": "ok" if not warning else "degraded",
        "public_url": PUBLIC_BASE_URL or None,
        "stored_locations": len(location_store),
        "storage": kind,
        "kv_configured": kv_configured(),
        "blob_configured": blob_configured(),
        "use_tunnel": USE_TUNNEL,
        "warning": warning,
    }


@app.get("/api/tunnel")
async def tunnel_info():
    global PUBLIC_BASE_URL
    url = PUBLIC_BASE_URL or fetch_ngrok_public_url()
    if url:
        PUBLIC_BASE_URL = url
    return {
        "public_url": url,
        "tunnel": TUNNEL,
        "use_tunnel": USE_TUNNEL,
        "storage": storage_kind(),
        "kv_configured": kv_configured(),
        "blob_configured": blob_configured(),
        "auth_configured": ngrok_token_ok() if TUNNEL == "ngrok" else True,
        "deployed": bool(url) and not USE_TUNNEL,
    }


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    if not DASHBOARD_HTML.exists():
        return HTMLResponse("<h2>operator_dashboard.html missing</h2>", status_code=500)
    return HTMLResponse(DASHBOARD_HTML.read_text())


@app.get("/locate", response_class=HTMLResponse)
@app.get("/consent", response_class=HTMLResponse)
async def consent_page():
    if not CONSENT_HTML.exists():
        return HTMLResponse("<h2>location_consent.html missing</h2>", status_code=500)
    return HTMLResponse(CONSENT_HTML.read_text())


@app.get("/link")
async def generate_link(
    request: Request,
    service: str = "Service Provider",
    ref: str = "",
):
    base = resolve_base_url(request)
    callback_url = append_query(f"{base}/receive", {"ref": ref} if ref else {})

    params = urlencode({
        "service": service,
        "callback": callback_url,
        "ref": ref,
    })
    link = f"{base}/locate?{params}"

    return JSONResponse({
        "link": link,
        "ref": ref or None,
        "service": service,
        "public_base": base,
        "callback": callback_url,
        "instructions": "Send this HTTPS link to the customer (SMS/WhatsApp). They tap Share and GPS is saved on your server.",
        "dashboard": f"{base}/dashboard",
        "view_location": f"{base}/location/{ref}" if ref else None,
    })


@app.post("/receive")
async def receive_location(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    ref = request.query_params.get("ref") or body.get("ref") or str(uuid.uuid4())[:8].upper()

    entry = {
        "ref": ref,
        "latitude": body.get("latitude"),
        "longitude": body.get("longitude"),
        "accuracy_m": body.get("accuracy"),
        "address": body.get("address", ""),
        "timestamp": body.get("timestamp", utc_now()),
        "received_at": utc_now(),
        "maps_url": None,
    }

    lat, lng = entry.get("latitude"), entry.get("longitude")
    if lat is not None and lng is not None:
        entry["maps_url"] = f"https://www.google.com/maps?q={lat},{lng}"

    kind = storage_kind()
    if IS_SERVERLESS and kind == "memory":
        return JSONResponse(
            {
                "error": "Storage not configured. In Vercel → Storage → create Blob and connect it to this project.",
            },
            status_code=503,
        )

    reload_store()
    location_store[ref] = entry
    err = persist_entry(ref, entry)
    if err:
        return JSONResponse({"error": err, "ref": ref}, status_code=500)

    print(f"\n[location received] {json.dumps(entry, indent=2)}\n")

    return JSONResponse({
        "status": "ok",
        "ref": ref,
        "maps_url": entry["maps_url"],
        "storage": kind,
    })


@app.get("/location/{ref}")
async def get_location(ref: str):
    reload_store()
    entry = location_store.get(ref)
    if not entry:
        return JSONResponse({"error": f"No location for ref '{ref}'"}, status_code=404)
    return JSONResponse(entry)


@app.get("/locations")
async def list_locations():
    reload_store()
    items = sorted(location_store.values(), key=lambda x: x.get("received_at", ""), reverse=True)
    kind = storage_kind()
    warning = None
    if IS_SERVERLESS and kind == "memory":
        warning = "Connect Vercel Blob in Storage — locations cannot persist on serverless without it."
    return JSONResponse({
        "count": len(items),
        "locations": items,
        "storage": kind,
        "warning": warning,
    })


@app.delete("/location/{ref}")
async def delete_location(ref: str):
    reload_store()
    if ref not in location_store:
        return JSONResponse({"error": "Not found"}, status_code=404)
    delete_entry(ref)
    return JSONResponse({"status": "deleted", "ref": ref})


def print_banner() -> None:
    local = f"http://127.0.0.1:{PORT}"
    if PUBLIC_BASE_URL:
        public = PUBLIC_BASE_URL
    elif USE_TUNNEL:
        public = f"(starting tunnel — TUNNEL={TUNNEL})"
    else:
        public = "(set PUBLIC_BASE_URL or open via your host HTTPS URL)"

    storage_labels = {
        "file": f"{DATA_FILE.name} (disk)",
        "kv": "Upstash Redis (Marketplace)",
        "blob": "Vercel Blob store",
        "memory": "in-memory cache",
    }
    storage = storage_labels[storage_kind()]

    print("\n" + "═" * 52)
    print("  Location Consent Server")
    print("═" * 52)
    print(f"  Mode               : {'local + tunnel' if USE_TUNNEL else 'deployed (HTTPS)'}")
    if USE_TUNNEL:
        print(f"  Tunnel provider    : {TUNNEL}")
    print(f"  Storage            : {storage}")
    print(f"  Operator dashboard : {local}/dashboard")
    print(f"  Generate link API  : {local}/link?service=My+Co&ref=JOB-001")
    print(f"  Public URL (share) : {public}")
    if PUBLIC_BASE_URL:
        print(f"  Customer link      : {PUBLIC_BASE_URL}/link?service=My+Co&ref=JOB-001")
    print("═" * 52 + "\n")


if __name__ == "__main__":
    import uvicorn

    reload = os.getenv("RELOAD", "0").lower() in ("1", "true", "yes")
    uvicorn.run("location_server:app", host=HOST, port=PORT, reload=reload)
