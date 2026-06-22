# Lukav

Personal credit-card debt auditor. Pulls card data via Plaid, scans
statements for math/fee discrepancies and FDCPA/FCRA red flags, and
drafts dispute letters.

**Single-user (Ian) for now.** Multi-user gating and disclaimers come
later, after the audit logic is verified against real data.

## Architecture

Mirrors `Tech-Support/python/agent/` (Theo) — same `tools/`, `llm/`,
`agent/` shape so anything written for one can move to the other:

```
python/lukav/
  cli.py                     # `python -m lukav` launcher
  web/                       # FastAPI + Jinja2 + HTMX UI on localhost
  agent/                     # chat loop + transcripts (added with legal_research)
  llm/                       # Claude/Ollama abstraction (added in Phase 3)
  tools/                     # Tool registry — discrepancy, FDCPA/FCRA, letters
  plaid_client.py            # plaid-python SDK wrapper (Phase 1)
  storage/                   # SQLite at ~/.lukav/lukav.db + OS keyring (Phase 1)
  legal/                     # YAML rule tables + Jinja2 letter templates
```

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate         # or .venv\Scripts\activate on Windows
pip install -e ".[dev,plaid,secrets]"
python -m lukav                   # opens http://127.0.0.1:8765
```

## End-to-end check

```bash
bash scripts/e2e.sh
```

Runs pytest, boots the server, hits `/healthz` and `/`, tears down.
Grows with each phase to also walk Plaid sandbox + scan + letter
generation.

## Phases

- **Phase 0 (current):** Scaffold, FastAPI hello + `/healthz`, e2e harness.
- **Phase 1:** Plaid link/exchange, dashboard listing cards with APR
  and minimum payment, SQLite storage, OS-keyring secrets.
- **Phase 2:** Deterministic discrepancy/fee/violation rules in
  `legal/rules/*.yaml`, `/scan/{account_id}` route.
- **Phase 3:** Claude-driven open-ended legal analysis (BYO `claude`
  CLI subscription) + Jinja2 dispute-letter PDFs.

## Disclaimer

Not legal or financial advice. Output is informational and meant for
the user to review before sending anything to a creditor or bureau.
