"""Collections layer — storage, audit, letter generation.

Kept in one module so Phase 0-3 code stays untouched. The data tables
(`collection_accounts`, `communications`, `collection_findings`,
`collection_letters`) live in the same SQLite database and reuse the
same `connect()` helper as the rest of Lukav."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from lukav.dispute_engine import get_profile, init_letters
from lukav.legal.rules import load_rules
from lukav.models.collections import (
    CollectionAccount, CollectionStatus, Communication, CommKind,
)
from lukav.models.findings import Finding, now_iso
from lukav.storage.db import connect as _connect
from lukav.tools.fdcpa_fcra import STATE_SOL_YEARS

SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_accounts (
    collection_id        TEXT PRIMARY KEY,
    collector_name       TEXT NOT NULL,
    collector_address    TEXT NOT NULL DEFAULT '',
    original_creditor    TEXT NOT NULL DEFAULT '',
    alleged_amount       REAL NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'in_collection',
    first_contact_date   TEXT,
    last_activity_date   TEXT,
    state                TEXT NOT NULL DEFAULT '',
    account_mask         TEXT,
    notes                TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS communications (
    communication_id        TEXT PRIMARY KEY,
    collection_account_id   TEXT NOT NULL REFERENCES collection_accounts(collection_id) ON DELETE CASCADE,
    kind                    TEXT NOT NULL,
    occurred_at             TEXT NOT NULL,
    summary                 TEXT NOT NULL DEFAULT '',
    threat_of_suit          INTEGER NOT NULL DEFAULT 0,
    third_party_disclosed   INTEGER NOT NULL DEFAULT 0,
    profanity_or_abuse      INTEGER NOT NULL DEFAULT 0,
    called_at_workplace     INTEGER NOT NULL DEFAULT 0,
    after_cease_demand      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS collection_findings (
    finding_id     TEXT PRIMARY KEY,
    collection_id  TEXT NOT NULL REFERENCES collection_accounts(collection_id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    severity       TEXT NOT NULL,
    rule_id        TEXT NOT NULL,
    title          TEXT NOT NULL,
    description    TEXT NOT NULL,
    citation       TEXT NOT NULL,
    evidence       TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_letters (
    letter_id      TEXT PRIMARY KEY,
    finding_id     TEXT NOT NULL,
    collection_id  TEXT NOT NULL,
    template       TEXT NOT NULL,
    body           TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""


def init_collections(db_path: Optional[Path] = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Phase 6 migration: mini_miranda_present added after the initial
        # ship. SQLite has no IF NOT EXISTS for ALTER; we test for it.
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(communications)"
        ).fetchall()}
        if "mini_miranda_present" not in cols:
            conn.execute(
                "ALTER TABLE communications "
                "ADD COLUMN mini_miranda_present INTEGER NOT NULL DEFAULT -1"
            )


# ---- helpers ------------------------------------------------------------

def _iso_d(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if isinstance(d, date) and not isinstance(d, datetime) else (
        d.isoformat() if isinstance(d, datetime) else d
    )


def _to_date(s) -> Optional[date]:
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, datetime):
        return s.date()
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _to_dt(s) -> Optional[datetime]:
    if isinstance(s, datetime):
        return s
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---- CRUD ---------------------------------------------------------------

def add_collection(payload: dict, *, db_path: Optional[Path] = None) -> str:
    init_collections(db_path)
    coll_id = _new_id("col")
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO collection_accounts (
              collection_id, collector_name, collector_address,
              original_creditor, alleged_amount, status,
              first_contact_date, last_activity_date, state,
              account_mask, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                coll_id,
                payload.get("collector_name", ""),
                payload.get("collector_address", ""),
                payload.get("original_creditor", ""),
                float(payload.get("alleged_amount") or 0),
                payload.get("status") or "in_collection",
                _iso_d(_to_date(payload.get("first_contact_date"))),
                _iso_d(_to_date(payload.get("last_activity_date"))),
                (payload.get("state") or "").upper().strip(),
                payload.get("account_mask") or None,
                payload.get("notes") or "",
            ),
        )
    return coll_id


def update_collection(coll_id: str, payload: dict,
                      *, db_path: Optional[Path] = None) -> None:
    init_collections(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE collection_accounts SET
              collector_name = ?,
              collector_address = ?,
              original_creditor = ?,
              alleged_amount = ?,
              status = ?,
              first_contact_date = ?,
              last_activity_date = ?,
              state = ?,
              account_mask = ?,
              notes = ?
            WHERE collection_id = ?
            """,
            (
                payload.get("collector_name", ""),
                payload.get("collector_address", ""),
                payload.get("original_creditor", ""),
                float(payload.get("alleged_amount") or 0),
                payload.get("status") or "in_collection",
                _iso_d(_to_date(payload.get("first_contact_date"))),
                _iso_d(_to_date(payload.get("last_activity_date"))),
                (payload.get("state") or "").upper().strip(),
                payload.get("account_mask") or None,
                payload.get("notes") or "",
                coll_id,
            ),
        )


def get_collection(coll_id: str,
                   *, db_path: Optional[Path] = None) -> Optional[CollectionAccount]:
    init_collections(db_path)
    with _connect(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM collection_accounts WHERE collection_id = ?",
            (coll_id,),
        ).fetchone()
    if not r:
        return None
    return CollectionAccount(
        collection_id=r["collection_id"],
        collector_name=r["collector_name"],
        collector_address=r["collector_address"],
        original_creditor=r["original_creditor"],
        alleged_amount=r["alleged_amount"],
        status=r["status"],
        first_contact_date=_to_date(r["first_contact_date"]),
        last_activity_date=_to_date(r["last_activity_date"]),
        state=r["state"],
        account_mask=r["account_mask"],
        notes=r["notes"],
    )


def list_collections(*, db_path: Optional[Path] = None) -> list[CollectionAccount]:
    init_collections(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT collection_id FROM collection_accounts ORDER BY collector_name"
        ).fetchall()
    return [get_collection(r["collection_id"], db_path=db_path) for r in rows]  # type: ignore


def delete_collection(coll_id: str, *, db_path: Optional[Path] = None) -> None:
    init_collections(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM collection_accounts WHERE collection_id = ?",
            (coll_id,),
        )


def add_communication(coll_id: str, payload: dict,
                      *, db_path: Optional[Path] = None) -> str:
    init_collections(db_path)
    comm_id = _new_id("comm")
    occurred = payload.get("occurred_at")
    dt = _to_dt(occurred) or datetime.utcnow()
    mini = payload.get("mini_miranda_present")
    if mini is None or mini == "":
        mini_val = -1
    elif isinstance(mini, str):
        mini_val = {"yes": 1, "no": 0, "unknown": -1}.get(mini.lower(), -1)
    else:
        mini_val = 1 if bool(mini) else 0
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO communications (
              communication_id, collection_account_id, kind, occurred_at,
              summary, threat_of_suit, third_party_disclosed,
              profanity_or_abuse, called_at_workplace, after_cease_demand,
              mini_miranda_present
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                comm_id, coll_id,
                payload.get("kind") or "phone",
                dt.isoformat(),
                payload.get("summary") or "",
                int(bool(payload.get("threat_of_suit"))),
                int(bool(payload.get("third_party_disclosed"))),
                int(bool(payload.get("profanity_or_abuse"))),
                int(bool(payload.get("called_at_workplace"))),
                int(bool(payload.get("after_cease_demand"))),
                mini_val,
            ),
        )
    return comm_id


def list_communications(coll_id: str,
                        *, db_path: Optional[Path] = None) -> list[Communication]:
    init_collections(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM communications WHERE collection_account_id = ? "
            "ORDER BY occurred_at DESC",
            (coll_id,),
        ).fetchall()
    return [
        Communication(
            communication_id=r["communication_id"],
            collection_account_id=r["collection_account_id"],
            kind=r["kind"],
            occurred_at=_to_dt(r["occurred_at"]) or datetime.utcnow(),
            summary=r["summary"],
            threat_of_suit=bool(r["threat_of_suit"]),
            third_party_disclosed=bool(r["third_party_disclosed"]),
            profanity_or_abuse=bool(r["profanity_or_abuse"]),
            called_at_workplace=bool(r["called_at_workplace"]),
            after_cease_demand=bool(r["after_cease_demand"]),
            mini_miranda_present=(
                r["mini_miranda_present"]
                if "mini_miranda_present" in r.keys() else -1
            ),
        )
        for r in rows
    ]


# ---- audit --------------------------------------------------------------

def _new(rule_id: str) -> str:
    return f"{rule_id}.{uuid.uuid4().hex[:10]}"


def audit_collection(coll_id: str,
                     *, db_path: Optional[Path] = None) -> list[Finding]:
    coll = get_collection(coll_id, db_path=db_path)
    if not coll:
        return []
    comms = list_communications(coll_id, db_path=db_path)
    rules = load_rules("fdcpa")
    fcra = load_rules("fcra")
    state_rules = load_rules("state")
    findings: list[Finding] = []
    findings.extend(_flag_validation_opportunity(coll, comms, rules))
    findings.extend(_flag_time_barred(coll, rules))
    findings.extend(_flag_outside_hours(coll, comms, rules))
    findings.extend(_flag_third_party_disclosure(coll, comms, rules))
    findings.extend(_flag_workplace_calls(coll, comms, rules))
    findings.extend(_flag_harassment(coll, comms, rules))
    findings.extend(_flag_threat_on_time_barred(coll, comms, rules))
    findings.extend(_flag_contact_after_cease(coll, comms, rules))
    findings.extend(_flag_reg_f_seven_in_seven(coll, comms, rules))
    findings.extend(_flag_reg_f_seven_after_conversation(coll, comms, rules))
    findings.extend(_flag_mini_miranda_missing(coll, comms, rules))
    findings.extend(_flag_obsolete_information(coll, fcra))
    findings.extend(_flag_state_extension(coll, state_rules))
    findings.extend(_flag_fcra_bureau_dispute(coll, fcra))
    findings.extend(_flag_fcra_direct_furnisher_dispute(coll, fcra))
    return [f for f in findings if f.citation]


_NEGATIVE_STATUSES = {"in_collection", "charged_off", "sold", "disputed"}


def _flag_fcra_bureau_dispute(
    coll: CollectionAccount, fcra: dict,
) -> Iterable[Finding]:
    """Any negative tradeline reported to a CRA carries a §1681i dispute
    right. We fire this unconditionally for in_collection / charged_off /
    sold / disputed accounts so the user always sees the option."""
    if coll.status not in _NEGATIVE_STATUSES:
        return
    rule = fcra.get("fcra.bureau_dispute_opportunity")
    if not rule:
        return
    yield Finding(
        finding_id=_new(rule["rule_id"]),
        account_id=coll.collection_id,
        kind="violation",
        severity=rule["severity"],
        rule_id=rule["rule_id"],
        title=rule["title"],
        description=(
            f"This account is reported as {coll.status}. You may file a "
            f"§1681i dispute with each consumer reporting agency where it "
            f"appears. The CRA must investigate within 30 days. Use the "
            f"FCRA dispute letter."
        ),
        citation=rule["citation"],
        evidence={"status": coll.status},
        created_at=now_iso(),
    )


def _flag_fcra_direct_furnisher_dispute(
    coll: CollectionAccount, fcra: dict,
) -> Iterable[Finding]:
    """§1681s-2(a)(8) direct dispute with the furnisher — same logic."""
    if coll.status not in _NEGATIVE_STATUSES:
        return
    rule = fcra.get("fcra.direct_furnisher_dispute")
    if not rule:
        return
    yield Finding(
        finding_id=_new(rule["rule_id"]),
        account_id=coll.collection_id,
        kind="violation",
        severity=rule["severity"],
        rule_id=rule["rule_id"],
        title=rule["title"],
        description=(
            f"Status is {coll.status}. You may dispute inaccurate "
            f"information directly with {coll.collector_name or 'the furnisher'} "
            f"under §1681s-2(a)(8). The furnisher must conduct a "
            f"reasonable investigation."
        ),
        citation=rule["citation"],
        evidence={"status": coll.status,
                  "furnisher": coll.collector_name or ""},
        created_at=now_iso(),
    )


def audit_diagnostics(coll_id: str,
                      *, db_path: Optional[Path] = None) -> dict:
    """Return a per-account diagnostic explaining which rule categories
    fired and which were skipped for lack of data. Powers the
    "0 findings — here's why" panel."""
    coll = get_collection(coll_id, db_path=db_path)
    if not coll:
        return {}
    comms = list_communications(coll_id, db_path=db_path)
    notes: list[str] = []
    if not coll.state:
        notes.append(
            "State of residence is empty: time-barred SOL, Rosenthal (CA), "
            "and other state-law rules are skipped. Set it in the edit "
            "form above."
        )
    if not coll.last_activity_date:
        notes.append(
            "Last activity date is empty: time-barred SOL and the §1681c "
            "obsolete-info check (7-year reporting limit) are skipped."
        )
    if not coll.first_contact_date and coll.status in _NEGATIVE_STATUSES:
        notes.append(
            "First-contact date is empty: the §1692g 30-day validation "
            "window math is skipped. If you ever received written "
            "communication from the collector, fill this in."
        )
    if not comms:
        notes.append(
            "Communications log is empty: every FDCPA conduct rule "
            "(outside-hours calls, third-party disclosure, harassment, "
            "Reg F 7-in-7, mini-Miranda, contact after cease, threats on "
            "time-barred debt) needs at least one logged interaction "
            "before it can fire."
        )
    return {"notes": notes}


def _flag_validation_opportunity(
    coll: CollectionAccount, comms: list[Communication], rules: dict,
) -> Iterable[Finding]:
    if not coll.first_contact_date:
        return
    rule = rules["fdcpa.validation_opportunity"]
    days_since = (date.today() - coll.first_contact_date).days
    yield Finding(
        finding_id=_new(rule["rule_id"]),
        account_id=coll.collection_id,
        kind="violation",
        severity=rule["severity"],
        rule_id=rule["rule_id"],
        title=rule["title"],
        description=(
            f"First contact from {coll.collector_name or 'this collector'} "
            f"was {coll.first_contact_date} ({days_since} days ago). Within "
            f"30 days of that first written communication you may send a "
            f"written validation request; collection must pause until "
            f"validation is provided."
        ),
        citation=rule["citation"],
        evidence={
            "first_contact_date": coll.first_contact_date.isoformat(),
            "days_since": days_since,
            "within_30_day_window": days_since <= 30,
        },
        created_at=now_iso(),
    )


def _flag_time_barred(coll: CollectionAccount, rules: dict) -> Iterable[Finding]:
    state = (coll.state or "").upper()
    if not state or state not in STATE_SOL_YEARS or not coll.last_activity_date:
        return
    sol_years = STATE_SOL_YEARS[state]
    last = coll.last_activity_date
    expiry = date(last.year + sol_years, last.month, min(last.day, 28))
    if date.today() < expiry:
        return
    rule = rules["fdcpa.time_barred_debt"]
    years_past = (date.today() - expiry).days // 365
    yield Finding(
        finding_id=_new(rule["rule_id"]),
        account_id=coll.collection_id,
        kind="violation",
        severity=rule["severity"],
        rule_id=rule["rule_id"],
        title=rule["title"],
        description=(
            f"Last activity {last}. {state} SOL on credit-card debt is "
            f"approximately {sol_years} years; it expired around {expiry} "
            f"({years_past} years ago). A collector cannot sue or threaten "
            f"suit, and certain communications must include the CFPB "
            f"time-barred disclosure."
        ),
        citation=rule["citation"],
        evidence={
            "state": state,
            "approx_sol_years": sol_years,
            "last_activity_date": last.isoformat(),
            "approx_sol_expiry": expiry.isoformat(),
        },
        created_at=now_iso(),
    )


def _flag_outside_hours(
    coll: CollectionAccount, comms: list[Communication], rules: dict,
) -> Iterable[Finding]:
    rule = rules["fdcpa.communication_outside_hours"]
    for c in comms:
        if c.kind not in ("phone", "text", "in_person"):
            continue
        t = c.occurred_at.time()
        if t < time(8, 0) or t >= time(21, 0):
            yield Finding(
                finding_id=_new(rule["rule_id"]),
                account_id=coll.collection_id,
                kind="violation",
                severity=rule["severity"],
                rule_id=rule["rule_id"],
                title=rule["title"],
                description=(
                    f"{c.kind.title()} contact at {c.occurred_at} is outside "
                    f"the 8:00 AM – 9:00 PM window the FDCPA presumes "
                    f"convenient. Each instance is a separate violation."
                ),
                citation=rule["citation"],
                evidence={
                    "communication_id": c.communication_id,
                    "occurred_at": c.occurred_at.isoformat(),
                    "kind": c.kind,
                },
                created_at=now_iso(),
            )


def _flag_third_party_disclosure(
    coll: CollectionAccount, comms: list[Communication], rules: dict,
) -> Iterable[Finding]:
    rule = rules["fdcpa.third_party_disclosure"]
    for c in comms:
        if not c.third_party_disclosed:
            continue
        yield Finding(
            finding_id=_new(rule["rule_id"]),
            account_id=coll.collection_id,
            kind="violation",
            severity=rule["severity"],
            rule_id=rule["rule_id"],
            title=rule["title"],
            description=(
                f"On {c.occurred_at.date()} the collector discussed the debt "
                f"with a third party (not you, your spouse, or your attorney). "
                f"That is a per-instance FDCPA violation."
            ),
            citation=rule["citation"],
            evidence={
                "communication_id": c.communication_id,
                "occurred_at": c.occurred_at.isoformat(),
                "summary": c.summary,
            },
            created_at=now_iso(),
        )


def _flag_workplace_calls(
    coll: CollectionAccount, comms: list[Communication], rules: dict,
) -> Iterable[Finding]:
    rule = rules["fdcpa.workplace_communication"]
    for c in comms:
        if not c.called_at_workplace:
            continue
        yield Finding(
            finding_id=_new(rule["rule_id"]),
            account_id=coll.collection_id,
            kind="violation",
            severity=rule["severity"],
            rule_id=rule["rule_id"],
            title=rule["title"],
            description=(
                f"On {c.occurred_at.date()} the collector contacted you at "
                f"your workplace. Once a collector knows the employer "
                f"prohibits such calls (and you may notify them in writing), "
                f"continued workplace contact violates the FDCPA."
            ),
            citation=rule["citation"],
            evidence={"communication_id": c.communication_id},
            created_at=now_iso(),
        )


def _flag_harassment(
    coll: CollectionAccount, comms: list[Communication], rules: dict,
) -> Iterable[Finding]:
    rule = rules["fdcpa.harassment"]
    abusive = [c for c in comms if c.profanity_or_abuse]
    # Repeated phone calls in a single day are also harassment evidence.
    by_day: dict[date, int] = {}
    for c in comms:
        if c.kind != "phone":
            continue
        day = c.occurred_at.date()
        by_day[day] = by_day.get(day, 0) + 1
    too_many_days = [(d, n) for d, n in by_day.items() if n >= 4]
    for c in abusive:
        yield Finding(
            finding_id=_new(rule["rule_id"]),
            account_id=coll.collection_id,
            kind="violation",
            severity=rule["severity"],
            rule_id=rule["rule_id"],
            title=rule["title"],
            description=(
                f"On {c.occurred_at.date()} the contact involved profane or "
                f"abusive language. Use of obscene, profane, or abusive "
                f"language is a per-instance FDCPA violation."
            ),
            citation=rule["citation"],
            evidence={"communication_id": c.communication_id,
                      "summary": c.summary},
            created_at=now_iso(),
        )
    for day, count in too_many_days:
        yield Finding(
            finding_id=_new(rule["rule_id"]),
            account_id=coll.collection_id,
            kind="violation",
            severity=rule["severity"],
            rule_id=rule["rule_id"],
            title="Repeated phone calls in a single day",
            description=(
                f"{count} phone contacts on {day}. Causing a phone to ring "
                f"repeatedly with intent to annoy or harass is an FDCPA "
                f"violation under §1692d."
            ),
            citation=rule["citation"],
            evidence={"day": day.isoformat(), "calls": count},
            created_at=now_iso(),
        )


def _flag_threat_on_time_barred(
    coll: CollectionAccount, comms: list[Communication], rules: dict,
) -> Iterable[Finding]:
    rule = rules["fdcpa.threat_on_time_barred"]
    # Only meaningful if the debt is time-barred.
    state = (coll.state or "").upper()
    if state not in STATE_SOL_YEARS or not coll.last_activity_date:
        return
    sol_years = STATE_SOL_YEARS[state]
    last = coll.last_activity_date
    expiry = date(last.year + sol_years, last.month, min(last.day, 28))
    if date.today() < expiry:
        return
    for c in comms:
        if not c.threat_of_suit:
            continue
        yield Finding(
            finding_id=_new(rule["rule_id"]),
            account_id=coll.collection_id,
            kind="violation",
            severity=rule["severity"],
            rule_id=rule["rule_id"],
            title=rule["title"],
            description=(
                f"On {c.occurred_at.date()} the collector threatened a "
                f"lawsuit on a debt that is past {state}'s SOL "
                f"(~{sol_years} years; last activity {last}). Threatening "
                f"action that cannot legally be taken is an FDCPA violation."
            ),
            citation=rule["citation"],
            evidence={
                "communication_id": c.communication_id,
                "state": state, "approx_sol_years": sol_years,
                "last_activity_date": last.isoformat(),
            },
            created_at=now_iso(),
        )


def _flag_reg_f_seven_in_seven(
    coll: CollectionAccount, comms: list[Communication], rules: dict,
) -> Iterable[Finding]:
    rule = rules.get("fdcpa.reg_f_seven_in_seven_calls")
    if not rule:
        return
    phones = sorted([c for c in comms if c.kind == "phone"],
                    key=lambda c: c.occurred_at)
    if len(phones) < 8:
        return
    # Rolling 7-day window: for each phone call, count how many phone
    # calls land in the [c - 7d, c] interval. >7 anywhere = flag.
    flagged_dates: set[date] = set()
    for i, anchor in enumerate(phones):
        window_start = anchor.occurred_at - timedelta(days=7)
        in_window = [
            c for c in phones
            if window_start <= c.occurred_at <= anchor.occurred_at
        ]
        if len(in_window) > 7:
            day = anchor.occurred_at.date()
            if day in flagged_dates:
                continue
            flagged_dates.add(day)
            yield Finding(
                finding_id=_new(rule["rule_id"]),
                account_id=coll.collection_id,
                kind="violation",
                severity=rule["severity"],
                rule_id=rule["rule_id"],
                title=rule["title"],
                description=(
                    f"{len(in_window)} phone calls in the 7-day window "
                    f"ending {anchor.occurred_at}. Regulation F presumes "
                    f"that more than 7 calls in 7 days violates §1692d."
                ),
                citation=rule["citation"],
                evidence={
                    "window_end": anchor.occurred_at.isoformat(),
                    "calls_in_window": len(in_window),
                    "communication_ids": [c.communication_id for c in in_window],
                },
                created_at=now_iso(),
            )


def _flag_reg_f_seven_after_conversation(
    coll: CollectionAccount, comms: list[Communication], rules: dict,
) -> Iterable[Finding]:
    rule = rules.get("fdcpa.reg_f_seven_days_after_conversation")
    if not rule:
        return
    phones = sorted([c for c in comms if c.kind == "phone"],
                    key=lambda c: c.occurred_at)
    # A "conversation" is a phone communication where the user noted the
    # collector spoke with them — we use any phone entry whose summary is
    # nonempty as a conservative proxy. (User can mark explicitly later.)
    for i, conv in enumerate(phones):
        if not conv.summary.strip():
            continue
        for nxt in phones[i + 1:]:
            gap = nxt.occurred_at - conv.occurred_at
            if gap <= timedelta(days=7) and gap > timedelta(0):
                yield Finding(
                    finding_id=_new(rule["rule_id"]),
                    account_id=coll.collection_id,
                    kind="violation",
                    severity=rule["severity"],
                    rule_id=rule["rule_id"],
                    title=rule["title"],
                    description=(
                        f"Phone conversation on {conv.occurred_at} was "
                        f"followed by another phone contact on "
                        f"{nxt.occurred_at} — within the 7-day "
                        f"post-conversation cool-down Regulation F imposes."
                    ),
                    citation=rule["citation"],
                    evidence={
                        "conversation_at": conv.occurred_at.isoformat(),
                        "next_call_at": nxt.occurred_at.isoformat(),
                        "communication_ids": [conv.communication_id, nxt.communication_id],
                    },
                    created_at=now_iso(),
                )
                break    # only flag the next call once per conversation


def _flag_mini_miranda_missing(
    coll: CollectionAccount, comms: list[Communication], rules: dict,
) -> Iterable[Finding]:
    rule = rules.get("fdcpa.mini_miranda_required")
    if not rule:
        return
    written = sorted([c for c in comms if c.kind in ("letter", "email")],
                     key=lambda c: c.occurred_at)
    if not written:
        return
    first = written[0]
    if first.mini_miranda_present == 0:
        yield Finding(
            finding_id=_new(rule["rule_id"]),
            account_id=coll.collection_id,
            kind="violation",
            severity=rule["severity"],
            rule_id=rule["rule_id"],
            title=rule["title"],
            description=(
                f"The initial written communication on {first.occurred_at} "
                f"did not contain the §1692e(11) Mini-Miranda disclosure "
                f"that the sender is a debt collector attempting to "
                f"collect a debt, and that any information will be used "
                f"for that purpose."
            ),
            citation=rule["citation"],
            evidence={
                "communication_id": first.communication_id,
                "occurred_at": first.occurred_at.isoformat(),
            },
            created_at=now_iso(),
        )


def _flag_obsolete_information(
    coll: CollectionAccount, fcra: dict,
) -> Iterable[Finding]:
    rule = fcra.get("fcra.obsolete_information")
    if not rule or not coll.last_activity_date:
        return
    seven_years = date(
        coll.last_activity_date.year + 7,
        coll.last_activity_date.month,
        min(coll.last_activity_date.day, 28),
    )
    if date.today() < seven_years:
        return
    years_past = (date.today() - seven_years).days // 365
    yield Finding(
        finding_id=_new(rule["rule_id"]),
        account_id=coll.collection_id,
        kind="violation",
        severity=rule["severity"],
        rule_id=rule["rule_id"],
        title=rule["title"],
        description=(
            f"Last activity {coll.last_activity_date}. Under §1681c(a) "
            f"most adverse items must be removed from consumer reports "
            f"seven years from the date of first delinquency — that "
            f"window closed around {seven_years} ({years_past} years "
            f"ago). The tradeline should be removable from all three "
            f"bureaus."
        ),
        citation=rule["citation"],
        evidence={
            "last_activity_date": coll.last_activity_date.isoformat(),
            "seven_year_expiry": seven_years.isoformat(),
            "years_past_expiry": years_past,
        },
        created_at=now_iso(),
    )


def _flag_state_extension(
    coll: CollectionAccount, state_rules: dict,
) -> Iterable[Finding]:
    state = (coll.state or "").upper()
    if not state:
        return
    for rule in state_rules.values():
        applies = rule.get("applies_to") or []
        if state in applies:
            yield Finding(
                finding_id=_new(rule["rule_id"]),
                account_id=coll.collection_id,
                kind="violation",
                severity=rule["severity"],
                rule_id=rule["rule_id"],
                title=rule["title"],
                description=rule["description"],
                citation=rule["citation"],
                evidence={"state": state, "applies_to": applies},
                created_at=now_iso(),
            )


def _flag_contact_after_cease(
    coll: CollectionAccount, comms: list[Communication], rules: dict,
) -> Iterable[Finding]:
    rule = rules["fdcpa.cease_contact"]
    for c in comms:
        if not c.after_cease_demand:
            continue
        yield Finding(
            finding_id=_new("fdcpa.contact_after_cease"),
            account_id=coll.collection_id,
            kind="violation",
            severity="high",
            rule_id="fdcpa.contact_after_cease",
            title="Contact after written cease demand",
            description=(
                f"You marked the {c.occurred_at.date()} contact as occurring "
                f"after you sent a written cease-of-contact demand. After "
                f"receipt of that demand the collector may only contact you "
                f"to confirm receipt or to notify of specific action."
            ),
            citation=rule["citation"],
            evidence={"communication_id": c.communication_id},
            created_at=now_iso(),
        )


# ---- persistence + scan -------------------------------------------------

def scan_collection(coll_id: str,
                    *, db_path: Optional[Path] = None) -> list[Finding]:
    init_collections(db_path)
    findings = list(audit_collection(coll_id, db_path=db_path))
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM collection_findings WHERE collection_id = ?",
                     (coll_id,))
        for f in findings:
            conn.execute(
                """
                INSERT INTO collection_findings (
                  finding_id, collection_id, kind, severity, rule_id,
                  title, description, citation, evidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f.finding_id, coll_id, f.kind, f.severity, f.rule_id,
                    f.title, f.description, f.citation,
                    json.dumps(f.evidence, default=str), f.created_at or "",
                ),
            )
    return findings


def list_collection_findings(coll_id: str,
                             *, db_path: Optional[Path] = None) -> list[Finding]:
    init_collections(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM collection_findings WHERE collection_id = ? "
            "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
            "WHEN 'low' THEN 2 ELSE 3 END",
            (coll_id,),
        ).fetchall()
    return [
        Finding(
            finding_id=r["finding_id"], account_id=r["collection_id"],
            kind=r["kind"], severity=r["severity"], rule_id=r["rule_id"],
            title=r["title"], description=r["description"],
            citation=r["citation"],
            evidence=json.loads(r["evidence"]) if r["evidence"] else {},
            created_at=r["created_at"],
        )
        for r in rows
    ]


def get_collection_finding(finding_id: str,
                           *, db_path: Optional[Path] = None) -> Optional[Finding]:
    init_collections(db_path)
    with _connect(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM collection_findings WHERE finding_id = ?",
            (finding_id,),
        ).fetchone()
    if not r:
        return None
    return Finding(
        finding_id=r["finding_id"], account_id=r["collection_id"],
        kind=r["kind"], severity=r["severity"], rule_id=r["rule_id"],
        title=r["title"], description=r["description"],
        citation=r["citation"],
        evidence=json.loads(r["evidence"]) if r["evidence"] else {},
        created_at=r["created_at"],
    )


# ---- letters ------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).resolve().parent / "legal" / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(default=False),
    trim_blocks=False, lstrip_blocks=False,
)

# rule_id → collection-specific template. Most rules map naturally to one
# of these; the user can also pick a different template manually from the
# scan page (Phase 4.1+).
COLLECTION_RULE_TO_TEMPLATE: dict[str, str] = {
    "fdcpa.validation_opportunity":     "collection_validation.j2",
    "fdcpa.time_barred_debt":           "collection_cease_contact.j2",
    "fdcpa.cease_contact":              "collection_cease_contact.j2",
    "fdcpa.contact_after_cease":        "collection_cease_contact.j2",
    "fdcpa.communication_outside_hours": "collection_cease_contact.j2",
    "fdcpa.third_party_disclosure":     "collection_cease_contact.j2",
    "fdcpa.workplace_communication":    "collection_workplace_cease.j2",
    "fdcpa.harassment":                 "collection_cease_contact.j2",
    "fdcpa.threat_on_time_barred":      "collection_cease_contact.j2",
    "fdcpa.reg_f_seven_in_seven_calls":     "collection_cease_contact.j2",
    "fdcpa.reg_f_seven_days_after_conversation": "collection_cease_contact.j2",
    "fdcpa.mini_miranda_required":      "collection_validation.j2",
    "fcra.obsolete_information":        "obsolete_info_removal.j2",
    "fcra.bureau_dispute_opportunity":  "fcra_dispute.j2",
    "fcra.direct_furnisher_dispute":    "direct_dispute.j2",
    "state.rosenthal_act":              "collection_cease_contact.j2",
    "state.nycdcr_reg":                 "collection_validation.j2",
    "state.texas_finance_chapter_392":  "collection_validation.j2",
}

# Manually-pickable templates (the user can request any of these regardless
# of which finding triggered the action).
PICKABLE_TEMPLATES: list[tuple[str, str]] = [
    ("collection_validation.j2",      "Debt validation request (§1692g)"),
    ("collection_cease_contact.j2",   "Cease-of-contact demand (§1692c(c))"),
    ("pay_for_delete.j2",             "Pay-for-delete offer"),
    ("method_of_verification.j2",     "Method-of-verification follow-up (§1681i(a)(7))"),
    ("goodwill_letter.j2",            "Goodwill removal request (original creditor)"),
    ("collection_workplace_cease.j2", "Workplace-contact cease (§1692c(a)(3))"),
    ("obsolete_info_removal.j2",      "Obsolete-info removal demand (§1681c(a))"),
]


def render_collection_letter(coll_id: str, template_name: str,
                             finding_id: Optional[str] = None) -> Optional[str]:
    coll = get_collection(coll_id)
    if not coll:
        return None
    profile = get_profile()
    finding = get_collection_finding(finding_id) if finding_id else None
    template = _env.get_template(template_name)
    return template.render(
        profile=profile,
        recipient=_recipient_from(coll),
        collection=coll,
        finding=finding,
        today=date.today().strftime("%B %d, %Y"),
    )


def _recipient_from(coll: CollectionAccount) -> dict:
    # Letter templates expect a recipient dict with name / address lines.
    # Collector address is stored as a free-form string; expose it as line1.
    return {
        "name": coll.collector_name,
        "address_line1": coll.collector_address.replace("\n", ", "),
        "address_line2": "",
        "city": "", "state": "", "zip": "",
    }


def save_collection_letter(coll_id: str, finding_id: Optional[str],
                           template_name: str, body: str) -> str:
    init_collections()
    letter_id = _new_id("collet")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO collection_letters (
              letter_id, finding_id, collection_id, template, body, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (letter_id, finding_id or "", coll_id, template_name, body,
             now_iso()),
        )
    return letter_id


def list_collection_letters(coll_id: Optional[str] = None) -> list[dict]:
    init_collections()
    with _connect() as conn:
        if coll_id:
            rows = conn.execute(
                "SELECT letter_id, finding_id, collection_id, template, "
                "created_at FROM collection_letters WHERE collection_id = ? "
                "ORDER BY created_at DESC",
                (coll_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT letter_id, finding_id, collection_id, template, "
                "created_at FROM collection_letters ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_collection_letter(letter_id: str) -> Optional[dict]:
    init_collections()
    with _connect() as conn:
        r = conn.execute(
            "SELECT * FROM collection_letters WHERE letter_id = ?",
            (letter_id,),
        ).fetchone()
    return dict(r) if r else None
