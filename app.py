"""Vercel ASGI entrypoint (root app.py is auto-detected by Vercel Python)."""
from location_server import app

__all__ = ["app"]
