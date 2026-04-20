"""CHECKS Terminal — FastAPI web server."""

import sys
from pathlib import Path

# Add src to path
ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT / "src"))

import uvicorn

from jason_checks.web.app import create_app

if __name__ == "__main__":
    app = create_app()
    print("🚀 CHECKS Terminal Web Server")
    print("📍 http://localhost:8000")
    print("Press Ctrl+C to stop\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
