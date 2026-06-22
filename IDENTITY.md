# Lukav — Identity

Lukav (Old Slavic: "cunning", "shrewd") is Ian's personal credit-card
debt auditor. It reads Plaid data, runs deterministic discrepancy and
fee-cap checks against the CARD Act / FDCPA / FCRA, asks Claude for
open-ended legal analysis on flagged items, and drafts dispute letters
Ian can review and send.

## Scope

- **In scope:** revolving credit-card debt — APR sanity, fee caps,
  duplicate charges, interest-charge math, FDCPA collection-practice
  red flags, FCRA furnisher/bureau dispute opportunities, validation
  requests, cease-contact letters.
- **Out of scope (v1):** mortgages, auto loans, student loans, BNPL,
  medical debt, bankruptcy planning, tax debt. Adding these is a
  per-domain rule pack later — the audit engine is generic.

## Posture

- Single-user (Ian) until the audit logic is verified end-to-end.
- Every surfaced finding shows the math and cites a statute or CFPB
  guidance. Findings without a citation are dropped, not surfaced.
- Letters are templates filled with structured fields, not free-form
  LLM output. The LLM never writes the citation text.
- Disclaimer ("not legal advice — review before sending") is rendered
  on every letter and every scan page.

## Architecture posture

- Mirror Theo's layout (`python/lukav/{agent,tools,llm}` + `cli.py`).
  Anything written for Theo's tool registry should drop into Lukav's
  with a one-file move.
- Local-first state (SQLite at `~/.lukav/lukav.db`, OS keyring for
  secrets). No cloud, no multi-tenant. Add Firebase only if/when
  Lukav becomes a real product.
- Claude Opus via `claude -p` CLI is the default brain when LLM is
  needed (Theo pattern). Ollama is opt-in via `LLM_BACKEND=ollama`.

## Changelog

| Version | Date | Change |
|---|---|---|
| 0.1.0 | 2026-06-22 | Initial. Phase 0 scaffold: FastAPI + healthz + e2e harness. |
