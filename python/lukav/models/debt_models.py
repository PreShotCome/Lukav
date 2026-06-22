"""Dataclasses for the credit-card data Lukav scans.

These map 1:1 to SQLite rows and to what Plaid's /liabilities/get and
/transactions/get return for credit products. Loan/depository fields
are intentionally omitted — Lukav's scope is credit cards in v1."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class Item:
    """One Plaid Item = one bank login. Holds the access token."""
    item_id: str
    institution_name: str
    access_token: str
    cursor: Optional[str] = None
    active: bool = True


@dataclass
class Account:
    """A single credit card account on an Item."""
    account_id: str
    item_id: str
    name: str
    official_name: Optional[str]
    mask: Optional[str]
    subtype: Optional[str]
    current_balance: Optional[float]
    available_balance: Optional[float]
    credit_limit: Optional[float]
    iso_currency_code: str = "USD"


@dataclass
class Liability:
    """Plaid /liabilities/get credit-card payload. The interest /
    statement / APR fields are the ones the discrepancy engine reads."""
    account_id: str
    is_overdue: Optional[bool] = None
    last_payment_amount: Optional[float] = None
    last_payment_date: Optional[date] = None
    last_statement_balance: Optional[float] = None
    last_statement_issue_date: Optional[date] = None
    minimum_payment_amount: Optional[float] = None
    next_payment_due_date: Optional[date] = None
    # APRs are a list in Plaid (purchase / balance transfer / cash advance).
    aprs: list["Apr"] = field(default_factory=list)


@dataclass
class Apr:
    apr_percentage: float
    apr_type: str          # 'purchase_apr', 'balance_transfer_apr', etc.
    balance_subject_to_apr: Optional[float] = None
    interest_charge_amount: Optional[float] = None


@dataclass
class Transaction:
    transaction_id: str
    account_id: str
    posted_date: date
    amount: float          # Plaid: positive = outflow (charge), negative = credit/payment
    name: str
    merchant_name: Optional[str] = None
    pending: bool = False
    category: Optional[str] = None


@dataclass
class Statement:
    statement_id: str
    account_id: str
    issue_date: date
    pdf_path: Optional[str] = None
