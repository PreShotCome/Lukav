"""Plaid tools exposed to the agent. Phase 3's legal_research tool calls
these to fetch fresh card context before asking Claude anything.

Each tool returns plain dicts/lists so the LLM transport (JSON) is happy."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date

from lukav.plaid_client import PlaidClient, default_window
from lukav.storage import db
from lukav.tools.base import Tool, ToolRegistry


def _plaid() -> PlaidClient:
    return PlaidClient()


def list_linked_cards() -> list[dict]:
    """Return every credit-card account currently in the local DB."""
    return [asdict(a) for a in db.list_accounts()]


def sync_item(item_id: str) -> dict:
    """Re-pull accounts + liabilities + last-90-days transactions for one Item."""
    items = {i.item_id: i for i in db.list_items()}
    item = items.get(item_id)
    if not item:
        return {"error": f"unknown item_id {item_id!r}"}
    client = _plaid()
    accounts = client.get_accounts(item.access_token)
    for a in accounts:
        db.upsert_account(a)
    for liab in client.get_liabilities(item.access_token):
        db.upsert_liability(liab)
    start, end = default_window()
    for txn in client.get_transactions(item.access_token, start, end):
        db.upsert_transaction(txn)
    return {
        "item_id": item_id,
        "accounts_synced": len(accounts),
    }


def get_account_snapshot(account_id: str) -> dict:
    """Bundle account + liability + recent transactions into one payload."""
    account = db.get_account(account_id)
    if not account:
        return {"error": f"unknown account_id {account_id!r}"}
    liab = db.get_liability(account_id)
    txns = db.list_transactions(account_id)
    return {
        "account": asdict(account),
        "liability": asdict(liab) if liab else None,
        "transactions": [asdict(t) for t in txns],
    }


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="list_linked_cards",
        description="List every credit card account linked to Lukav.",
        parameters_schema={"type": "object", "properties": {}, "required": []},
        handler=list_linked_cards,
    ))
    registry.register(Tool(
        name="sync_item",
        description="Pull fresh Plaid data (accounts, liabilities, last 90 days of "
                    "transactions) for one Item. Use after linking a new card or "
                    "before running a scan.",
        parameters_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "The Plaid item_id to sync."}
            },
            "required": ["item_id"],
        },
        handler=sync_item,
    ))
    registry.register(Tool(
        name="get_account_snapshot",
        description="Return account, liability (APR, statement balance, min payment, "
                    "due date), and recent transactions for one credit card account.",
        parameters_schema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "The Plaid account_id."}
            },
            "required": ["account_id"],
        },
        handler=get_account_snapshot,
    ))
