"""FastAPI app.

Phase 0: /, /healthz
Phase 1: /link, /exchange, /sync, dashboard listing cards
Phase 2: /scan/{account_id}
Phase 3: /letter/{kind}/{target_id}, /letters
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lukav.plaid_client import PlaidClient, PlaidLike, default_window
from lukav.models.debt_models import Item
from lukav.storage import db
from lukav.storage.secrets import have_plaid_creds, plaid_env

_HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

USER_ID = "ian"


def create_app(plaid: Optional[PlaidLike] = None) -> FastAPI:
    app = FastAPI(title="Lukav", version="0.1.0")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    db.init_db()
    plaid_client: PlaidLike = plaid or PlaidClient()

    # ---- core ---------------------------------------------------------

    @app.get("/healthz", response_class=JSONResponse)
    def healthz() -> dict:
        return {"status": "ok", "service": "lukav"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        items = db.list_items(active_only=True)
        cards = []
        for acct in db.list_accounts():
            liab = db.get_liability(acct.account_id)
            cards.append({"account": acct, "liability": liab})
        return templates.TemplateResponse(
            request, "index.html",
            {
                "title": "Lukav",
                "items": items,
                "cards": cards,
                "plaid_env": plaid_env(),
                "have_creds": have_plaid_creds(),
            },
        )

    # ---- plaid link flow ---------------------------------------------

    @app.get("/link", response_class=HTMLResponse)
    def link_page(request: Request):
        if not have_plaid_creds():
            return templates.TemplateResponse(
                request, "link_needs_creds.html",
                {"title": "Link a card", "plaid_env": plaid_env()},
            )
        try:
            link_token = plaid_client.create_link_token(USER_ID)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Plaid link_token error: {e}")
        return templates.TemplateResponse(
            request, "link.html",
            {"title": "Link a card", "link_token": link_token},
        )

    @app.post("/exchange", response_class=JSONResponse)
    def exchange(public_token: str = Form(...)):
        try:
            access_token, item_id = plaid_client.exchange_public_token(public_token)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Plaid exchange error: {e}")
        try:
            institution_name = plaid_client.get_institution_name(access_token)
        except Exception:
            institution_name = "Unknown institution"
        db.upsert_item(Item(
            item_id=item_id,
            institution_name=institution_name,
            access_token=access_token,
        ))
        synced = _sync_item_inner(plaid_client, item_id, access_token)
        return {"ok": True, "item_id": item_id, "synced": synced}

    @app.post("/sync/{item_id}", response_class=JSONResponse)
    def sync_one(item_id: str):
        items = {i.item_id: i for i in db.list_items()}
        item = items.get(item_id)
        if not item:
            raise HTTPException(status_code=404, detail=f"unknown item {item_id}")
        synced = _sync_item_inner(plaid_client, item_id, item.access_token)
        return {"ok": True, "synced": synced}

    @app.post("/sync", response_class=JSONResponse)
    def sync_all():
        total = {"items": 0, "accounts": 0}
        for item in db.list_items():
            res = _sync_item_inner(plaid_client, item.item_id, item.access_token)
            total["items"] += 1
            total["accounts"] += res.get("accounts", 0)
        return {"ok": True, **total}

    @app.get("/account/{account_id}", response_class=HTMLResponse)
    def account_page(account_id: str, request: Request):
        account = db.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        liab = db.get_liability(account_id)
        txns = db.list_transactions(account_id)
        return templates.TemplateResponse(
            request, "account.html",
            {
                "title": f"{account.name} •{account.mask or '?'}",
                "account": account,
                "liability": liab,
                "transactions": txns,
            },
        )

    @app.get("/relink/{item_id}")
    def relink(item_id: str):
        return RedirectResponse(url=f"/link?item_id={item_id}")

    return app


def _sync_item_inner(client: PlaidLike, item_id: str, access_token: str) -> dict:
    accounts = client.get_accounts(access_token)
    for acct in accounts:
        # Plaid returns account.item_id, but pin it to the linked item
        # we already persisted so the FK never drifts.
        acct.item_id = item_id
        db.upsert_account(acct)
    for liab in client.get_liabilities(access_token):
        db.upsert_liability(liab)
    start, end = default_window()
    txn_count = 0
    for txn in client.get_transactions(access_token, start, end):
        db.upsert_transaction(txn)
        txn_count += 1
    return {"accounts": len(accounts), "transactions": txn_count}
