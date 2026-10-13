"""Start the agent in DEMO mode: offline fakes for the model, the brand website and WordPress.

    python scripts/run_demo.py            # then open http://localhost:8000/demo

No API key, no network, no secrets. Every control (redaction, scope-pinned crawl, stage
gates, segregation of duties, audit chain, SEO/AEO/GEO scoring) runs for real; only the
three external systems are simulated.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["AGENT_ENV"] = "demo"
os.environ.setdefault("AGENT_DATABASE_URL", "sqlite:///./demo.db")

import uvicorn  # noqa: E402

from app.main import create_app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Demo UI:   http://localhost:{port}/demo\nAPI docs:  http://localhost:{port}/docs")
    uvicorn.run(create_app(), host="127.0.0.1", port=port)
