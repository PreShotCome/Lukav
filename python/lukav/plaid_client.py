"""Thin wrapper around the plaid-python SDK.

Lazy-imports `plaid` so the rest of the app (and the test suite) doesn't
need it. Exposes one Protocol (`PlaidLike`) and one concrete client
(`PlaidClient`). The Protocol is what the web routes and the tools take
as a dependency — that lets tests inject a fake without monkeypatching.

For each call we convert Plaid SDK objects into our dataclasses (see
models.debt_models)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional, Protocol, Sequence

from lukav.models.debt_models import (
    Account, Apr, Item, Liability, Transaction,
)
from lukav.storage.secrets import get_secret, plaid_env


class PlaidLike(Protocol):
    def create_link_token(self, user_id: str) -> str: ...
    def exchange_public_token(self, public_token: str) -> tuple[str, str]: ...
    def get_institution_name(self, item_access_token: str) -> str: ...
    def get_accounts(self, item_access_token: str) -> list[Account]: ...
    def get_liabilities(self, item_access_token: str) -> list[Liability]: ...
    def get_transactions(
        self, item_access_token: str, start: date, end: date,
    ) -> list[Transaction]: ...


@dataclass
class _PlaidConfig:
    client_id: str
    secret: str
    env: str

    @property
    def host(self) -> str:
        return {
            "sandbox":    "https://sandbox.plaid.com",
            "development": "https://development.plaid.com",
            "production": "https://production.plaid.com",
        }.get(self.env, "https://sandbox.plaid.com")


def _load_config() -> _PlaidConfig:
    client_id = get_secret("PLAID_CLIENT_ID")
    secret = get_secret("PLAID_SECRET")
    if not client_id or not secret:
        raise RuntimeError(
            "Plaid credentials missing. Set PLAID_CLIENT_ID and PLAID_SECRET "
            "as env vars, or store them in the OS keyring under service "
            "'lukav'."
        )
    return _PlaidConfig(client_id=client_id, secret=secret, env=plaid_env())


class PlaidClient:
    """Concrete Plaid client. Construction itself does not call the API,
    so we can build the FastAPI app without creds present."""

    def __init__(self, config: Optional[_PlaidConfig] = None) -> None:
        self._config: Optional[_PlaidConfig] = config

    # --- internal lazy SDK access ---------------------------------------

    def _config_or_load(self) -> _PlaidConfig:
        if self._config is None:
            self._config = _load_config()
        return self._config

    def _client(self):
        cfg = self._config_or_load()
        try:
            from plaid.api import plaid_api
            from plaid.api_client import ApiClient
            from plaid.configuration import Configuration
        except ImportError as e:
            raise RuntimeError(
                "plaid-python not installed. Install with "
                "`pip install lukav[plaid]`."
            ) from e
        configuration = Configuration(
            host=cfg.host,
            api_key={"clientId": cfg.client_id, "secret": cfg.secret},
        )
        api_client = ApiClient(configuration)
        return plaid_api.PlaidApi(api_client)

    # --- public API ------------------------------------------------------

    def create_link_token(self, user_id: str) -> str:
        from plaid.model.country_code import CountryCode
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.products import Products

        req = LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id=user_id),
            client_name="Lukav",
            products=[Products("transactions"), Products("liabilities")],
            country_codes=[CountryCode("US")],
            language="en",
        )
        resp = self._client().link_token_create(req)
        return resp["link_token"]

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        from plaid.model.item_public_token_exchange_request import (
            ItemPublicTokenExchangeRequest,
        )
        req = ItemPublicTokenExchangeRequest(public_token=public_token)
        resp = self._client().item_public_token_exchange(req)
        return resp["access_token"], resp["item_id"]

    def get_institution_name(self, item_access_token: str) -> str:
        from plaid.model.country_code import CountryCode
        from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
        from plaid.model.item_get_request import ItemGetRequest

        client = self._client()
        item = client.item_get(ItemGetRequest(access_token=item_access_token))
        inst_id = item["item"]["institution_id"]
        if not inst_id:
            return "Unknown institution"
        inst = client.institutions_get_by_id(
            InstitutionsGetByIdRequest(
                institution_id=inst_id, country_codes=[CountryCode("US")],
            )
        )
        return inst["institution"]["name"]

    def get_accounts(self, item_access_token: str) -> list[Account]:
        from plaid.model.accounts_get_request import AccountsGetRequest

        resp = self._client().accounts_get(
            AccountsGetRequest(access_token=item_access_token)
        )
        item_id = resp["item"]["item_id"]
        out: list[Account] = []
        for raw in resp["accounts"]:
            if str(raw.get("type")) != "credit":
                continue
            balances = raw.get("balances", {})
            out.append(Account(
                account_id=raw["account_id"],
                item_id=item_id,
                name=raw.get("name") or "Card",
                official_name=raw.get("official_name"),
                mask=raw.get("mask"),
                subtype=str(raw.get("subtype")) if raw.get("subtype") else None,
                current_balance=balances.get("current"),
                available_balance=balances.get("available"),
                credit_limit=balances.get("limit"),
                iso_currency_code=balances.get("iso_currency_code") or "USD",
            ))
        return out

    def get_liabilities(self, item_access_token: str) -> list[Liability]:
        from plaid.model.liabilities_get_request import LiabilitiesGetRequest

        resp = self._client().liabilities_get(
            LiabilitiesGetRequest(access_token=item_access_token)
        )
        out: list[Liability] = []
        for raw in resp["liabilities"].get("credit") or []:
            aprs = [
                Apr(
                    apr_percentage=float(a["apr_percentage"]),
                    apr_type=str(a["apr_type"]),
                    balance_subject_to_apr=a.get("balance_subject_to_apr"),
                    interest_charge_amount=a.get("interest_charge_amount"),
                )
                for a in raw.get("aprs") or []
            ]
            out.append(Liability(
                account_id=raw["account_id"],
                is_overdue=raw.get("is_overdue"),
                last_payment_amount=raw.get("last_payment_amount"),
                last_payment_date=_to_date(raw.get("last_payment_date")),
                last_statement_balance=raw.get("last_statement_balance"),
                last_statement_issue_date=_to_date(raw.get("last_statement_issue_date")),
                minimum_payment_amount=raw.get("minimum_payment_amount"),
                next_payment_due_date=_to_date(raw.get("next_payment_due_date")),
                aprs=aprs,
            ))
        return out

    def get_transactions(
        self, item_access_token: str, start: date, end: date,
    ) -> list[Transaction]:
        from plaid.model.transactions_get_request import TransactionsGetRequest
        from plaid.model.transactions_get_request_options import (
            TransactionsGetRequestOptions,
        )

        resp = self._client().transactions_get(
            TransactionsGetRequest(
                access_token=item_access_token,
                start_date=start, end_date=end,
                options=TransactionsGetRequestOptions(count=500),
            )
        )
        out: list[Transaction] = []
        for raw in resp["transactions"]:
            out.append(Transaction(
                transaction_id=raw["transaction_id"],
                account_id=raw["account_id"],
                posted_date=_to_date(raw.get("date")) or date.today(),
                amount=float(raw.get("amount", 0.0)),
                name=raw.get("name") or "(unknown)",
                merchant_name=raw.get("merchant_name"),
                pending=bool(raw.get("pending")),
                category=", ".join(raw.get("category") or []) or None,
            ))
        return out


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def default_window() -> tuple[date, date]:
    end = date.today()
    return end - timedelta(days=90), end
