"""FastAPI app. v1 routes will grow phase by phase:
  Phase 0: /, /healthz
  Phase 1: /link, /exchange, /accounts, /sync
  Phase 2: /scan/{account_id}
  Phase 3: /letter/{kind}/{id}, /letters
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="Lukav", version="0.1.0")

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/healthz", response_class=JSONResponse)
    def healthz() -> dict:
        return {"status": "ok", "service": "lukav"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request, "index.html", {"title": "Lukav"},
        )

    return app
