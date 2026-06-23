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

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lukav.audit_engine import (
    get_finding, init_findings, list_findings, load_context, save_context,
    scan_account,
)
from lukav.collections_engine import (
    COLLECTION_RULE_TO_TEMPLATE, PICKABLE_TEMPLATES, add_collection,
    add_communication, audit_collection, audit_diagnostics,
    delete_collection, get_collection, get_collection_finding,
    get_collection_letter, init_collections, list_collection_findings,
    list_collection_letters, list_collections, list_communications,
    render_collection_letter, save_collection_letter, scan_collection,
    update_collection,
)
from lukav.dispute_engine import (
    get_letter, get_profile, init_letters, list_letters, render_letter,
    save_letter, save_profile, template_for,
)
from lukav.ingest import (
    ExtractedLetter, ingest, ingest_text, to_collection_payload,
    to_communication_payload,
)
from lukav.ingest_credit_report import (
    CreditReportExtraction, ingest_credit_report,
    to_collection_payload as tradeline_to_payload,
)
from lukav.cfpb_lookup import lookup as cfpb_lookup
from lukav.legal import debt_buyers
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
    init_collections()
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

    # ---- collections (manual debt entries) -------------------------

    @app.get("/collections", response_class=HTMLResponse)
    def collections_index(request: Request):
        return templates.TemplateResponse(
            request, "collections.html",
            {"title": "Collections", "collections": list_collections()},
        )

    @app.get("/collections/new", response_class=HTMLResponse)
    def collections_new(request: Request):
        return templates.TemplateResponse(
            request, "collection_new.html",
            {"title": "Add collection", "collection": None},
        )

    @app.post("/collections", response_class=RedirectResponse)
    def collections_create(
        collector_name: str = Form(""),
        collector_address: str = Form(""),
        original_creditor: str = Form(""),
        alleged_amount: str = Form("0"),
        status: str = Form("in_collection"),
        first_contact_date: str = Form(""),
        last_activity_date: str = Form(""),
        state: str = Form(""),
        account_mask: str = Form(""),
        notes: str = Form(""),
    ):
        coll_id = add_collection({
            "collector_name": collector_name.strip(),
            "collector_address": collector_address.strip(),
            "original_creditor": original_creditor.strip(),
            "alleged_amount": alleged_amount or 0,
            "status": status,
            "first_contact_date": first_contact_date.strip() or None,
            "last_activity_date": last_activity_date.strip() or None,
            "state": state.strip().upper(),
            "account_mask": account_mask.strip() or None,
            "notes": notes.strip(),
        })
        return RedirectResponse(url=f"/collections/{coll_id}", status_code=303)

    @app.get("/collections/{coll_id}", response_class=HTMLResponse)
    def collections_detail(coll_id: str, request: Request):
        coll = get_collection(coll_id)
        if not coll:
            raise HTTPException(status_code=404, detail="collection not found")
        return templates.TemplateResponse(
            request, "collection_detail.html",
            {
                "title": coll.collector_name or "Collection",
                "collection": coll,
                "communications": list_communications(coll_id),
                "findings": list_collection_findings(coll_id),
                "letters": list_collection_letters(coll_id),
                "rule_to_template": COLLECTION_RULE_TO_TEMPLATE,
                "pickable_templates": PICKABLE_TEMPLATES,
                "buyer": debt_buyers.match(coll.collector_name or ""),
                "diagnostic": audit_diagnostics(coll_id),
            },
        )

    @app.post("/collections/{coll_id}", response_class=RedirectResponse)
    def collections_update(
        coll_id: str,
        collector_name: str = Form(""),
        collector_address: str = Form(""),
        original_creditor: str = Form(""),
        alleged_amount: str = Form("0"),
        status: str = Form("in_collection"),
        first_contact_date: str = Form(""),
        last_activity_date: str = Form(""),
        state: str = Form(""),
        account_mask: str = Form(""),
        notes: str = Form(""),
    ):
        if not get_collection(coll_id):
            raise HTTPException(status_code=404, detail="collection not found")
        update_collection(coll_id, {
            "collector_name": collector_name.strip(),
            "collector_address": collector_address.strip(),
            "original_creditor": original_creditor.strip(),
            "alleged_amount": alleged_amount or 0,
            "status": status,
            "first_contact_date": first_contact_date.strip() or None,
            "last_activity_date": last_activity_date.strip() or None,
            "state": state.strip().upper(),
            "account_mask": account_mask.strip() or None,
            "notes": notes.strip(),
        })
        return RedirectResponse(url=f"/collections/{coll_id}", status_code=303)

    @app.post("/collections/{coll_id}/delete", response_class=RedirectResponse)
    def collections_delete(coll_id: str):
        delete_collection(coll_id)
        return RedirectResponse(url="/collections", status_code=303)

    @app.post("/collections/{coll_id}/communication", response_class=RedirectResponse)
    def communications_create(
        coll_id: str,
        kind: str = Form("phone"),
        occurred_at: str = Form(""),
        summary: str = Form(""),
        threat_of_suit: str = Form(""),
        third_party_disclosed: str = Form(""),
        profanity_or_abuse: str = Form(""),
        called_at_workplace: str = Form(""),
        after_cease_demand: str = Form(""),
        mini_miranda_present: str = Form(""),
    ):
        if not get_collection(coll_id):
            raise HTTPException(status_code=404, detail="collection not found")
        add_communication(coll_id, {
            "kind": kind,
            "occurred_at": occurred_at.strip() or None,
            "summary": summary.strip(),
            "threat_of_suit": bool(threat_of_suit),
            "third_party_disclosed": bool(third_party_disclosed),
            "profanity_or_abuse": bool(profanity_or_abuse),
            "called_at_workplace": bool(called_at_workplace),
            "after_cease_demand": bool(after_cease_demand),
            "mini_miranda_present": mini_miranda_present,
        })
        return RedirectResponse(url=f"/collections/{coll_id}", status_code=303)

    @app.post("/collections/{coll_id}/scan", response_class=RedirectResponse)
    def collections_scan(coll_id: str):
        if not get_collection(coll_id):
            raise HTTPException(status_code=404, detail="collection not found")
        scan_collection(coll_id)
        return RedirectResponse(url=f"/collections/{coll_id}", status_code=303)

    @app.get("/collections/{coll_id}/letter/{template_name}",
             response_class=HTMLResponse)
    def collection_letter_preview(coll_id: str, template_name: str,
                                  request: Request,
                                  finding_id: str = ""):
        coll = get_collection(coll_id)
        if not coll:
            raise HTTPException(status_code=404, detail="collection not found")
        body = render_collection_letter(coll_id, template_name,
                                        finding_id or None)
        if body is None:
            raise HTTPException(status_code=400, detail="cannot render letter")
        profile = get_profile()
        return templates.TemplateResponse(
            request, "collection_letter.html",
            {
                "title": template_name,
                "collection": coll,
                "template_name": template_name,
                "body": body,
                "finding_id": finding_id,
                "profile_complete": bool(profile.get("name")),
            },
        )

    @app.post("/collections/{coll_id}/letter/{template_name}/save",
              response_class=RedirectResponse)
    def collection_letter_save(coll_id: str, template_name: str,
                               finding_id: str = Form("")):
        body = render_collection_letter(coll_id, template_name,
                                        finding_id or None)
        if body is None:
            raise HTTPException(status_code=400, detail="cannot render letter")
        save_collection_letter(coll_id, finding_id or None,
                               template_name, body)
        return RedirectResponse(url=f"/collections/{coll_id}", status_code=303)

    @app.get("/collections/{coll_id}/letter/{template_name}/text")
    def collection_letter_text(coll_id: str, template_name: str,
                               finding_id: str = ""):
        body = render_collection_letter(coll_id, template_name,
                                        finding_id or None)
        if body is None:
            raise HTTPException(status_code=404, detail="cannot render letter")
        from fastapi.responses import PlainTextResponse
        safe_name = template_name.replace(".j2", "")
        return PlainTextResponse(
            body, headers={
                "Content-Disposition":
                    f'attachment; filename="lukav-{safe_name}-{coll_id}.txt"',
            },
        )

    # ---- ingest (PDF / image / paste -> collection or communication) ----

    @app.get("/ingest", response_class=HTMLResponse)
    def ingest_form(request: Request):
        return templates.TemplateResponse(
            request, "ingest_form.html",
            {"title": "Ingest letter", "collection": None,
             "collections": list_collections()},
        )

    @app.post("/ingest", response_class=HTMLResponse)
    async def ingest_submit(request: Request,
                            collection_id: str = Form(""),
                            state: str = Form(""),
                            pasted_text: str = Form(""),
                            file: Optional[UploadFile] = File(None)):
        result: ExtractedLetter
        if file is not None and file.filename:
            data = await file.read()
            result = ingest(data, file.filename)
        else:
            result = ingest_text(pasted_text)
        return templates.TemplateResponse(
            request, "ingest_preview.html",
            {
                "title": "Ingest preview",
                "result": result,
                "raw_text": result.raw_text,
                "collection_id": collection_id or "",
                "state": state.strip().upper(),
                "collections": list_collections(),
            },
        )

    @app.post("/ingest/save", response_class=RedirectResponse)
    def ingest_save(
        action: str = Form(...),
        collection_id: str = Form(""),
        state: str = Form(""),
        collector_name: str = Form(""),
        collector_address: str = Form(""),
        original_creditor: str = Form(""),
        alleged_amount: str = Form("0"),
        letter_date: str = Form(""),
        summary: str = Form(""),
        threat_of_suit: str = Form(""),
        time_bar_disclosure: str = Form(""),
    ):
        letter = ExtractedLetter(
            collector_name=collector_name.strip(),
            collector_address=collector_address.strip(),
            original_creditor=original_creditor.strip(),
            alleged_amount=float(alleged_amount or 0),
            letter_date=letter_date.strip() or None,
            summary=summary.strip(),
            threat_of_suit=bool(threat_of_suit),
            time_bar_disclosure=bool(time_bar_disclosure),
        )
        target_id = collection_id.strip()

        if action == "new":
            new_id = add_collection(to_collection_payload(
                letter, fallback_state=state.strip().upper(),
            ))
            add_communication(new_id, to_communication_payload(letter))
            return RedirectResponse(url=f"/collections/{new_id}",
                                    status_code=303)

        if action == "attach":
            if not target_id or not get_collection(target_id):
                raise HTTPException(
                    status_code=400,
                    detail="pick an existing collection to attach to",
                )
            # Populate empty fields on the existing collection.
            existing = get_collection(target_id)
            update_collection(target_id, {
                "collector_name": existing.collector_name or letter.collector_name,
                "collector_address": existing.collector_address or letter.collector_address,
                "original_creditor": existing.original_creditor or letter.original_creditor,
                "alleged_amount": existing.alleged_amount or letter.alleged_amount,
                "status": existing.status,
                "first_contact_date": (
                    existing.first_contact_date.isoformat()
                    if existing.first_contact_date else letter.letter_date
                ),
                "last_activity_date": (
                    existing.last_activity_date.isoformat()
                    if existing.last_activity_date else None
                ),
                "state": existing.state or state.strip().upper(),
                "account_mask": existing.account_mask,
                "notes": existing.notes,
            })
            add_communication(target_id, to_communication_payload(letter))
            return RedirectResponse(url=f"/collections/{target_id}",
                                    status_code=303)

        raise HTTPException(status_code=400, detail=f"unknown action {action!r}")

    # ---- Phase 8: credit-report bulk ingest --------------------------

    @app.get("/credit-report", response_class=HTMLResponse)
    def credit_report_form(request: Request):
        return templates.TemplateResponse(
            request, "credit_report_form.html",
            {"title": "Credit report ingest"},
        )

    @app.post("/credit-report", response_class=HTMLResponse)
    async def credit_report_extract(request: Request,
                                    state: str = Form(""),
                                    file: Optional[UploadFile] = File(None)):
        if file is None or not file.filename:
            raise HTTPException(status_code=400, detail="upload a PDF")
        data = await file.read()
        result: CreditReportExtraction = ingest_credit_report(data, file.filename)
        return templates.TemplateResponse(
            request, "credit_report_preview.html",
            {
                "title": "Credit report preview",
                "result": result,
                "state": state.strip().upper(),
            },
        )

    @app.post("/credit-report/save", response_class=RedirectResponse)
    async def credit_report_save(request: Request):
        form = await request.form()
        state = (form.get("state") or "").strip().upper()
        rows = int(form.get("row_count") or 0)
        created: list[str] = []
        for i in range(rows):
            if not form.get(f"keep_{i}"):
                continue
            from lukav.ingest_credit_report import Tradeline
            t = Tradeline(
                collector_name=(form.get(f"collector_name_{i}") or "").strip(),
                original_creditor=(form.get(f"original_creditor_{i}") or "").strip(),
                alleged_amount=float(form.get(f"alleged_amount_{i}") or 0),
                account_mask=(form.get(f"account_mask_{i}") or "").strip() or None,
                date_of_first_delinquency=(
                    (form.get(f"date_of_first_delinquency_{i}") or "").strip()
                    or None
                ),
                last_activity_date=(
                    (form.get(f"last_activity_date_{i}") or "").strip()
                    or None
                ),
                status=(form.get(f"status_{i}") or "in_collection").strip(),
                bureau=(form.get(f"bureau_{i}") or "").strip(),
                notes=(form.get(f"notes_{i}") or "").strip(),
            )
            coll_id = add_collection(tradeline_to_payload(t, fallback_state=state))
            created.append(coll_id)
        # If only one created, jump straight to it; else list view.
        if len(created) == 1:
            return RedirectResponse(url=f"/collections/{created[0]}",
                                    status_code=303)
        return RedirectResponse(url="/collections", status_code=303)

    @app.get("/collections/{coll_id}/cfpb", response_class=HTMLResponse)
    def collections_cfpb(coll_id: str, request: Request):
        coll = get_collection(coll_id)
        if not coll:
            raise HTTPException(status_code=404, detail="collection not found")
        result = cfpb_lookup(coll.collector_name)
        return templates.TemplateResponse(
            request, "cfpb_lookup.html",
            {
                "title": f"CFPB complaints — {coll.collector_name}",
                "collection": coll,
                "result": result,
            },
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
