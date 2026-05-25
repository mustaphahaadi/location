"""
Vercel entrypoint — export the FastAPI `app` directly.
Vercel's Python runtime wraps ASGI apps natively; do not use Mangum here.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from location_server import app  # noqa: F401
