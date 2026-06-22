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

from lukav.audit_engine import (
    get_finding, init_findings, list_findings, load_context, save_context,
    scan_account,
)
from lukav.dispute_engine import (
    get_letter, get_profile, init_letters, list_letters, render_letter,
    save_letter, save_profile, template_for,
)
from lukav.plaid_client import PlaidClient, PlaidLike, default_window
from lukav.models.debt_models import Item
from lukav.storage import db
from lukav.storage.secrets import have_plaid_creds, plaid_env
from lukav.tools.legal_research import analyze_finding

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
    init_findings()
    init_letters()
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

    # ---- scan ---------------------------------------------------------

    @app.get("/scan/{account_id}", response_class=HTMLResponse)
    def scan_page(account_id: str, request: Request):
        account = db.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        findings = list_findings(account_id)
        context = load_context(account_id)
        return templates.TemplateResponse(
            request, "scan.html",
            {
                "title": f"Scan — {account.name}",
                "account": account,
                "findings": findings,
                "context": context,
            },
        )

    @app.post("/scan/{account_id}", response_class=RedirectResponse)
    def scan_run(account_id: str):
        account = db.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        scan_account(account_id)
        return RedirectResponse(url=f"/scan/{account_id}", status_code=303)

    @app.post("/scan/{account_id}/context", response_class=RedirectResponse)
    def scan_context_save(
        account_id: str,
        state: str = Form(""),
        last_activity_date: str = Form(""),
        collection_letter_received: str = Form(""),
        collection_letter_date: str = Form(""),
        credit_report_dispute_basis: str = Form(""),
    ):
        account = db.get_account(account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        payload = {
            "state": state.strip().upper() or None,
            "last_activity_date": last_activity_date.strip() or None,
            "collection_letter_received": bool(collection_letter_received),
            "collection_letter_date": collection_letter_date.strip() or None,
            "credit_report_dispute_basis": credit_report_dispute_basis.strip() or None,
        }
        save_context(account_id, payload)
        return RedirectResponse(url=f"/scan/{account_id}", status_code=303)

    # ---- profile (recipient info on dispute letters) ----------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return templates.TemplateResponse(
            request, "settings.html",
            {"title": "Settings", "profile": get_profile()},
        )

    @app.post("/settings", response_class=RedirectResponse)
    def settings_save(
        name: str = Form(""),
        address_line1: str = Form(""),
        address_line2: str = Form(""),
        city: str = Form(""),
        state: str = Form(""),
        zip: str = Form(""),
    ):
        save_profile({
            "name": name.strip(),
            "address_line1": address_line1.strip(),
            "address_line2": address_line2.strip(),
            "city": city.strip(),
            "state": state.strip().upper(),
            "zip": zip.strip(),
        })
        return RedirectResponse(url="/settings", status_code=303)

    # ---- letters ----------------------------------------------------

    @app.get("/letter/{finding_id}", response_class=HTMLResponse)
    def letter_preview(finding_id: str, request: Request):
        finding = get_finding(finding_id)
        if not finding:
            raise HTTPException(status_code=404, detail="finding not found")
        template_name = template_for(finding)
        if not template_name:
            raise HTTPException(
                status_code=400,
                detail=f"no dispute template wired for rule {finding.rule_id}",
            )
        body = render_letter(finding_id)
        profile_complete = bool(get_profile().get("name"))
        review = analyze_finding(finding_id) if profile_complete else None
        return templates.TemplateResponse(
            request, "letter.html",
            {
                "title": f"Letter — {finding.title}",
                "finding": finding,
                "template_name": template_name,
                "body": body,
                "profile_complete": profile_complete,
                "review": review,
            },
        )

    @app.post("/letter/{finding_id}/save", response_class=RedirectResponse)
    def letter_save(finding_id: str):
        finding = get_finding(finding_id)
        if not finding:
            raise HTTPException(status_code=404, detail="finding not found")
        body = render_letter(finding_id)
        if body is None:
            raise HTTPException(status_code=400, detail="cannot render letter")
        template_name = template_for(finding)
        letter_id = save_letter(finding_id, body, template_name or "")
        return RedirectResponse(url=f"/letters/{letter_id}", status_code=303)

    @app.get("/letter/{finding_id}/text")
    def letter_text(finding_id: str):
        body = render_letter(finding_id)
        if body is None:
            raise HTTPException(status_code=404, detail="finding not found")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            body, headers={
                "Content-Disposition": f'attachment; filename="lukav-letter-{finding_id}.txt"',
            },
        )

    @app.get("/letters", response_class=HTMLResponse)
    def letters_index(request: Request):
        return templates.TemplateResponse(
            request, "letters.html",
            {"title": "Letters", "letters": list_letters()},
        )

    @app.get("/letters/{letter_id}", response_class=HTMLResponse)
    def letter_view(letter_id: str, request: Request):
        letter = get_letter(letter_id)
        if not letter:
            raise HTTPException(status_code=404, detail="letter not found")
        return templates.TemplateResponse(
            request, "letter_saved.html",
            {"title": "Saved letter", "letter": letter},
        )

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
