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

import uvicorn
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
USE_TUNNEL = os.getenv("USE_TUNNEL", os.getenv("USE_NGROK", "1")).lower() in ("1", "true", "yes")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
NGROK_BIN = os.getenv("NGROK_BIN", "ngrok")
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "").strip()
CLOUDFLARED_BIN = os.getenv("CLOUDFLARED_BIN", str(ROOT / "bin" / "cloudflared") if (ROOT / "bin" / "cloudflared").exists() else "cloudflared")

location_store: dict[str, dict] = {}
_tunnel_process: subprocess.Popen | None = None
CF_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_store() -> None:
    global location_store
    if DATA_FILE.exists():
        try:
            raw = json.loads(DATA_FILE.read_text())
            if isinstance(raw, dict):
                location_store = raw
            elif isinstance(raw, list):
                location_store = {e["ref"]: e for e in raw if e.get("ref")}
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] Could not load {DATA_FILE.name}: {exc}")


def save_store() -> None:
    DATA_FILE.write_text(json.dumps(location_store, indent=2))


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

    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and (
        request.headers.get("host", "").endswith(".ngrok-free.app")
        or request.headers.get("host", "").endswith(".trycloudflare.com")
    ):
        return "https://" + request.headers["host"]
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
    if USE_TUNNEL and not PUBLIC_BASE_URL:
        start_tunnel(PORT)
    print_banner()
    yield
    save_store()
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
    return {
        "status": "ok",
        "public_url": PUBLIC_BASE_URL or None,
        "stored_locations": len(location_store),
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
        "auth_configured": ngrok_token_ok() if TUNNEL == "ngrok" else True,
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

    location_store[ref] = entry
    save_store()
    print(f"\n[location received] {json.dumps(entry, indent=2)}\n")

    return JSONResponse({"status": "ok", "ref": ref, "maps_url": entry["maps_url"]})


@app.get("/location/{ref}")
async def get_location(ref: str):
    entry = location_store.get(ref)
    if not entry:
        return JSONResponse({"error": f"No location for ref '{ref}'"}, status_code=404)
    return JSONResponse(entry)


@app.get("/locations")
async def list_locations():
    items = sorted(location_store.values(), key=lambda x: x.get("received_at", ""), reverse=True)
    return JSONResponse({"count": len(items), "locations": items})


@app.delete("/location/{ref}")
async def delete_location(ref: str):
    if ref not in location_store:
        return JSONResponse({"error": "Not found"}, status_code=404)
    del location_store[ref]
    save_store()
    return JSONResponse({"status": "deleted", "ref": ref})


def print_banner() -> None:
    local = f"http://127.0.0.1:{PORT}"
    public = PUBLIC_BASE_URL or f"(start tunnel — TUNNEL={TUNNEL} in .env)"

    print("\n" + "═" * 52)
    print("  Location Consent Server")
    print("═" * 52)
    print(f"  Tunnel provider     : {TUNNEL}")
    print(f"  Operator dashboard : {local}/dashboard")
    print(f"  Generate link API  : {local}/link?service=My+Co&ref=JOB-001")
    print(f"  Public URL (share) : {public}")
    if PUBLIC_BASE_URL:
        print(f"  Customer link      : {PUBLIC_BASE_URL}/link?service=My+Co&ref=JOB-001")
    print(f"  Stored data file   : {DATA_FILE.name}")
    print("═" * 52 + "\n")


if __name__ == "__main__":
    reload = os.getenv("RELOAD", "0").lower() in ("1", "true", "yes")
    uvicorn.run("location_server:app", host=HOST, port=PORT, reload=reload)
