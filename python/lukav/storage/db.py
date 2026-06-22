"""SQLite store at ~/.lukav/lukav.db (override via LUKAV_DB env var).

Schema is idempotent — running `init_db()` repeatedly is safe. Each
connection enables foreign keys. CRUD helpers stay close to the
dataclasses in models.debt_models so callers don't have to write SQL."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

from lukav.models.debt_models import (
    Account, Apr, Item, Liability, Statement, Transaction,
)


def default_db_path() -> Path:
    override = os.environ.get("LUKAV_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".lukav" / "lukav.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id           TEXT PRIMARY KEY,
    institution_name  TEXT NOT NULL,
    access_token      TEXT NOT NULL,
    cursor            TEXT,
    active            INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id          TEXT PRIMARY KEY,
    item_id             TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    official_name       TEXT,
    mask                TEXT,
    subtype             TEXT,
    current_balance     REAL,
    available_balance   REAL,
    credit_limit        REAL,
    iso_currency_code   TEXT NOT NULL DEFAULT 'USD'
);

CREATE TABLE IF NOT EXISTS liabilities (
    account_id                   TEXT PRIMARY KEY REFERENCES accounts(account_id) ON DELETE CASCADE,
    is_overdue                   INTEGER,
    last_payment_amount          REAL,
    last_payment_date            TEXT,
    last_statement_balance       REAL,
    last_statement_issue_date    TEXT,
    minimum_payment_amount       REAL,
    next_payment_due_date        TEXT,
    aprs_json                    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id      TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    posted_date         TEXT NOT NULL,
    amount              REAL NOT NULL,
    name                TEXT NOT NULL,
    merchant_name       TEXT,
    pending             INTEGER NOT NULL DEFAULT 0,
    category            TEXT
);

CREATE TABLE IF NOT EXISTS statements (
    statement_id   TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    issue_date     TEXT NOT NULL,
    pdf_path       TEXT
);
"""


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Optional[Path] = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


# --- helpers --------------------------------------------------------------

def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if isinstance(d, date) else d


def _from_iso(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


# --- items ----------------------------------------------------------------

def upsert_item(item: Item, *, db_path: Optional[Path] = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO items (item_id, institution_name, access_token, cursor, active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
              institution_name = excluded.institution_name,
              access_token = excluded.access_token,
              cursor = excluded.cursor,
              active = excluded.active
            """,
            (item.item_id, item.institution_name, item.access_token,
             item.cursor, 1 if item.active else 0),
        )


def list_items(*, active_only: bool = True, db_path: Optional[Path] = None) -> list[Item]:
    with connect(db_path) as conn:
        sql = "SELECT * FROM items"
        if active_only:
            sql += " WHERE active = 1"
        rows = conn.execute(sql).fetchall()
    return [
        Item(
            item_id=r["item_id"],
            institution_name=r["institution_name"],
            access_token=r["access_token"],
            cursor=r["cursor"],
            active=bool(r["active"]),
        )
        for r in rows
    ]


# --- accounts -------------------------------------------------------------

def upsert_account(acct: Account, *, db_path: Optional[Path] = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO accounts (
              account_id, item_id, name, official_name, mask, subtype,
              current_balance, available_balance, credit_limit, iso_currency_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
              item_id = excluded.item_id,
              name = excluded.name,
              official_name = excluded.official_name,
              mask = excluded.mask,
              subtype = excluded.subtype,
              current_balance = excluded.current_balance,
              available_balance = excluded.available_balance,
              credit_limit = excluded.credit_limit,
              iso_currency_code = excluded.iso_currency_code
            """,
            (
                acct.account_id, acct.item_id, acct.name, acct.official_name,
                acct.mask, acct.subtype, acct.current_balance,
                acct.available_balance, acct.credit_limit, acct.iso_currency_code,
            ),
        )


def list_accounts(*, db_path: Optional[Path] = None) -> list[Account]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM accounts").fetchall()
    return [
        Account(
            account_id=r["account_id"], item_id=r["item_id"], name=r["name"],
            official_name=r["official_name"], mask=r["mask"], subtype=r["subtype"],
            current_balance=r["current_balance"],
            available_balance=r["available_balance"],
            credit_limit=r["credit_limit"],
            iso_currency_code=r["iso_currency_code"],
        )
        for r in rows
    ]


def get_account(account_id: str, *, db_path: Optional[Path] = None) -> Optional[Account]:
    with connect(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
    if not r:
        return None
    return Account(
        account_id=r["account_id"], item_id=r["item_id"], name=r["name"],
        official_name=r["official_name"], mask=r["mask"], subtype=r["subtype"],
        current_balance=r["current_balance"],
        available_balance=r["available_balance"],
        credit_limit=r["credit_limit"],
        iso_currency_code=r["iso_currency_code"],
    )


# --- liabilities ----------------------------------------------------------

def upsert_liability(liab: Liability, *, db_path: Optional[Path] = None) -> None:
    aprs_json = json.dumps([apr.__dict__ for apr in liab.aprs])
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO liabilities (
              account_id, is_overdue,
              last_payment_amount, last_payment_date,
              last_statement_balance, last_statement_issue_date,
              minimum_payment_amount, next_payment_due_date, aprs_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
              is_overdue = excluded.is_overdue,
              last_payment_amount = excluded.last_payment_amount,
              last_payment_date = excluded.last_payment_date,
              last_statement_balance = excluded.last_statement_balance,
              last_statement_issue_date = excluded.last_statement_issue_date,
              minimum_payment_amount = excluded.minimum_payment_amount,
              next_payment_due_date = excluded.next_payment_due_date,
              aprs_json = excluded.aprs_json
            """,
            (
                liab.account_id,
                None if liab.is_overdue is None else int(liab.is_overdue),
                liab.last_payment_amount, _iso(liab.last_payment_date),
                liab.last_statement_balance, _iso(liab.last_statement_issue_date),
                liab.minimum_payment_amount, _iso(liab.next_payment_due_date),
                aprs_json,
            ),
        )


def get_liability(account_id: str, *, db_path: Optional[Path] = None) -> Optional[Liability]:
    with connect(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM liabilities WHERE account_id = ?", (account_id,)
        ).fetchone()
    if not r:
        return None
    aprs_raw = json.loads(r["aprs_json"]) if r["aprs_json"] else []
    return Liability(
        account_id=r["account_id"],
        is_overdue=None if r["is_overdue"] is None else bool(r["is_overdue"]),
        last_payment_amount=r["last_payment_amount"],
        last_payment_date=_from_iso(r["last_payment_date"]),
        last_statement_balance=r["last_statement_balance"],
        last_statement_issue_date=_from_iso(r["last_statement_issue_date"]),
        minimum_payment_amount=r["minimum_payment_amount"],
        next_payment_due_date=_from_iso(r["next_payment_due_date"]),
        aprs=[Apr(**a) for a in aprs_raw],
    )


# --- transactions ---------------------------------------------------------

def upsert_transaction(txn: Transaction, *, db_path: Optional[Path] = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO transactions (
              transaction_id, account_id, posted_date, amount, name,
              merchant_name, pending, category
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transaction_id) DO UPDATE SET
              account_id = excluded.account_id,
              posted_date = excluded.posted_date,
              amount = excluded.amount,
              name = excluded.name,
              merchant_name = excluded.merchant_name,
              pending = excluded.pending,
              category = excluded.category
            """,
            (
                txn.transaction_id, txn.account_id, _iso(txn.posted_date),
                txn.amount, txn.name, txn.merchant_name,
                1 if txn.pending else 0, txn.category,
            ),
        )


def list_transactions(account_id: str, *, db_path: Optional[Path] = None) -> list[Transaction]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE account_id = ? ORDER BY posted_date DESC",
            (account_id,),
        ).fetchall()
    return [
        Transaction(
            transaction_id=r["transaction_id"], account_id=r["account_id"],
            posted_date=_from_iso(r["posted_date"]) or date.today(),
            amount=r["amount"], name=r["name"], merchant_name=r["merchant_name"],
            pending=bool(r["pending"]), category=r["category"],
        )
        for r in rows
    ]


# --- statements -----------------------------------------------------------

def upsert_statement(s: Statement, *, db_path: Optional[Path] = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO statements (statement_id, account_id, issue_date, pdf_path)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(statement_id) DO UPDATE SET
              account_id = excluded.account_id,
              issue_date = excluded.issue_date,
              pdf_path = excluded.pdf_path
            """,
            (s.statement_id, s.account_id, _iso(s.issue_date), s.pdf_path),
        )
