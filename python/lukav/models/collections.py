"""Manual collection-account model + communications log.

Plaid only sees live tradelines under the user's bank logins; once a
debt is sold to a collector it usually vanishes from Plaid. This module
is the user-typed alternative so Lukav can audit collection-account
behavior (call timing, post-validation contact, time-barred threats),
generate the right letter, and track follow-ups."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Optional

CollectionStatus = Literal[
    "in_collection",   # actively being collected
    "charged_off",     # creditor charged off, may or may not be with a collector
    "sold",            # sold to a debt buyer
    "disputed",        # validation/dispute in flight
    "settled",         # settled for less
    "paid",            # paid in full
    "removed",         # removed from credit report
]


CommKind = Literal["letter", "phone", "email", "text", "in_person"]


@dataclass
class CollectionAccount:
    collection_id: str
    collector_name: str
    collector_address: str
    original_creditor: str
    alleged_amount: float
    status: CollectionStatus = "in_collection"
    first_contact_date: Optional[date] = None
    last_activity_date: Optional[date] = None
    state: str = ""                # user's residence state, drives SOL
    account_mask: Optional[str] = None   # last 4 of original account if known
    notes: str = ""


@dataclass
class Communication:
    communication_id: str
    collection_account_id: str
    kind: CommKind
    occurred_at: datetime
    summary: str = ""
    threat_of_suit: bool = False
    third_party_disclosed: bool = False
    profanity_or_abuse: bool = False
    called_at_workplace: bool = False
    after_cease_demand: bool = False
