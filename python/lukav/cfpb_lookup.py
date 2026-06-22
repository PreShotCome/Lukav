"""CFPB public consumer-complaint database lookup.

API docs:
  https://cfpb.github.io/api/ccdb/api.html

We query by company name (free-text) and product=Debt collection,
ordering by date_received desc. Returns aggregated counts + a few
recent narratives so the user can see what pattern of conduct other
consumers have reported."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx


CFPB_SEARCH_URL = (
    "https://www.consumerfinance.gov/data-research/"
    "consumer-complaints/search/api/v1/"
)


@dataclass
class CfpbHit:
    company: str
    issue: str
    sub_issue: str
    state: str
    date_received: str
    narrative: str = ""


@dataclass
class CfpbResult:
    company_query: str
    total_hits: int = 0
    top_issues: list[tuple[str, int]] = field(default_factory=list)
    recent: list[CfpbHit] = field(default_factory=list)
    error: Optional[str] = None


def lookup(company: str, *, limit: int = 5,
           timeout: float = 8.0) -> CfpbResult:
    result = CfpbResult(company_query=company)
    company = (company or "").strip()
    if not company:
        return result
    params = {
        "search_term": company,
        "product": "Debt collection",
        "size": limit,
        "sort": "created_date_desc",
        "no_aggs": "false",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(CFPB_SEARCH_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        result.error = f"CFPB lookup failed: {e}"
        return result

    hits = (data.get("hits") or {})
    result.total_hits = int(hits.get("total", {}).get("value", 0) or 0)

    for h in hits.get("hits", [])[:limit]:
        src = h.get("_source") or {}
        result.recent.append(CfpbHit(
            company=src.get("company") or "",
            issue=src.get("issue") or "",
            sub_issue=src.get("sub_issue") or "",
            state=src.get("state") or "",
            date_received=(src.get("date_received") or "")[:10],
            narrative=(src.get("complaint_what_happened") or "")[:600],
        ))

    aggs = (data.get("aggregations") or {})
    issue_agg = (aggs.get("issue") or {}).get("issue") or {}
    buckets = (issue_agg.get("buckets") or [])[:5]
    result.top_issues = [
        (str(b.get("key") or ""), int(b.get("doc_count") or 0))
        for b in buckets if b.get("key")
    ]
    return result
