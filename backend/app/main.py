"""FastAPI entrypoint.

Run from the ``backend`` directory:

    uvicorn app.main:app --reload --port 8000

Serves the API under ``/api`` and, when a production build exists in
``backend/static``, the compiled frontend as well.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import APP_NAME, APP_VERSION, BASE_DIR
from .core.alerts import scheduler
from .core.store import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

_HAS_BUILD = (STATIC_DIR / "index.html").exists()

if _HAS_BUILD:
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        """Serve the built SPA, letting client-side routing handle deep links."""
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")

else:

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        """API-only mode (frontend not built yet)."""
        return {"app": APP_NAME, "version": APP_VERSION, "docs": "/docs", "api": "/api/health"}
