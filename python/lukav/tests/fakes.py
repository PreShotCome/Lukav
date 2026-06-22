"""In-memory fake Plaid client used by Phase 1+ tests."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from lukav.models.debt_models import Account, Apr, Liability, Transaction


class FakePlaid:
    def __init__(
        self,
        *,
        institution_name: str = "Test Bank",
        accounts: Optional[list[Account]] = None,
        liabilities: Optional[list[Liability]] = None,
        transactions: Optional[list[Transaction]] = None,
    ) -> None:
        self.institution_name = institution_name
        self.accounts = accounts or []
        self.liabilities = liabilities or []
        self.transactions = transactions or []
        self.created_link_tokens: list[str] = []
        self.exchanged: list[str] = []

    def create_link_token(self, user_id: str) -> str:
        tok = f"link-sandbox-{user_id}-{len(self.created_link_tokens)}"
        self.created_link_tokens.append(tok)
        return tok

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        self.exchanged.append(public_token)
        return f"access-{public_token}", f"item-{public_token}"

    def get_institution_name(self, item_access_token: str) -> str:
        return self.institution_name

    def get_accounts(self, item_access_token: str) -> list[Account]:
        return list(self.accounts)

    def get_liabilities(self, item_access_token: str) -> list[Liability]:
        return list(self.liabilities)

    def get_transactions(
        self, item_access_token: str, start: date, end: date,
    ) -> list[Transaction]:
        return [t for t in self.transactions if start <= t.posted_date <= end]


def make_sample_dataset(item_id: str = "item-test-1") -> tuple[
    list[Account], list[Liability], list[Transaction]
]:
    """A small realistic credit-card dataset used across tests."""
    today = date.today()
    account = Account(
        account_id="acct-test-1",
        item_id=item_id,
        name="Visa Signature",
        official_name="Big Bank Visa Signature",
        mask="4242",
        subtype="credit card",
        current_balance=2450.18,
        available_balance=2549.82,
        credit_limit=5000.00,
    )
    liability = Liability(
        account_id=account.account_id,
        is_overdue=False,
        last_payment_amount=150.00,
        last_payment_date=today - timedelta(days=20),
        last_statement_balance=2300.00,
        last_statement_issue_date=today - timedelta(days=25),
        minimum_payment_amount=75.00,
        next_payment_due_date=today + timedelta(days=5),
        aprs=[
            Apr(apr_percentage=24.99, apr_type="purchase_apr",
                balance_subject_to_apr=2300.00, interest_charge_amount=47.18),
            Apr(apr_percentage=29.99, apr_type="cash_advance_apr",
                balance_subject_to_apr=0.0, interest_charge_amount=0.0),
        ],
    )
    transactions = [
        Transaction(transaction_id=f"txn-{i}", account_id=account.account_id,
                    posted_date=today - timedelta(days=i),
                    amount=12.34 + i, name=f"Coffee {i}",
                    merchant_name="Local Cafe", pending=False,
                    category="Food and Drink")
        for i in range(5)
    ]
    return [account], [liability], transactions
