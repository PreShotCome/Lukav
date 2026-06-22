"""LLM-driven open-ended analysis of one Finding.

Hard rule: the assistant's commentary is treated as commentary, NOT as
a new finding or a new citation. The Finding's own `citation` field
(populated by the rule pack) is the only authoritative source. The LLM
output is rendered verbatim above the dispute letter for the user to
sanity-check; it never overwrites a citation."""
from __future__ import annotations

from dataclasses import dataclass

from lukav.audit_engine import get_finding
from lukav.llm import ChatMessage, build_default_client
from lukav.tools.base import Tool, ToolRegistry

SYSTEM = (
    "You are a consumer-protection assistant helping a non-lawyer "
    "review a credit-card finding their auditor produced. You will be "
    "given the finding's title, description, citation, and the evidence "
    "the auditor recorded.\n\n"
    "Respond in 3 short paragraphs:\n"
    "  1) Plain-English summary of what the finding says.\n"
    "  2) What the citation means and what the user can do about it.\n"
    "  3) One paragraph titled 'Caveats' listing what the user should "
    "verify before acting (e.g. confirm the latest CFPB cap, confirm "
    "their state's SOL, confirm the collector wasn't authorized).\n\n"
    "Hard rules:\n"
    "  - Do NOT invent new statute citations. If you want to mention "
    "one, mention only the citation already provided to you.\n"
    "  - Do NOT give legal advice. Use phrases like 'you may', 'it "
    "is generally', 'consult a licensed attorney for'.\n"
    "  - Keep under 250 words.\n"
)


@dataclass
class LegalReview:
    finding_id: str
    commentary: str
    backend: str           # 'claude' / 'ollama' / 'none'


def analyze_finding(finding_id: str) -> LegalReview:
    finding = get_finding(finding_id)
    if not finding:
        return LegalReview(finding_id=finding_id,
                           commentary="(finding not found)",
                           backend="none")

    client = build_default_client()
    if client is None:
        return LegalReview(
            finding_id=finding_id, backend="none",
            commentary=(
                "LLM analysis is disabled or no `claude` CLI is available. "
                "The structured finding below stands on its own — the "
                "citation is the authoritative reference. Install Claude "
                "Code or set LUKAV_LLM_BACKEND=ollama to enable narrative "
                "commentary."
            ),
        )

    user_msg = (
        f"Finding title: {finding.title}\n"
        f"Description: {finding.description}\n"
        f"Citation (do not introduce others): {finding.citation}\n"
        f"Evidence the auditor recorded: {finding.evidence}\n"
        f"Severity: {finding.severity}\n"
        f"Kind: {finding.kind}\n"
    )
    try:
        msg = client.chat(
            messages=[
                ChatMessage(role="system", content=SYSTEM),
                ChatMessage(role="user", content=user_msg),
            ],
            temperature=0.2,
        )
        commentary = msg.content.strip() or "(empty model response)"
        backend = client.__class__.__name__
    except Exception as e:
        commentary = f"(LLM call failed: {e})"
        backend = "error"
    return LegalReview(finding_id=finding_id, commentary=commentary,
                       backend=backend)


# ---- tool wiring --------------------------------------------------------

def _tool(finding_id: str) -> dict:
    review = analyze_finding(finding_id)
    return review.__dict__


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="analyze_finding",
        description="Ask the configured LLM to write a plain-English review "
                    "of one finding. Returns commentary text; the finding's "
                    "own citation remains authoritative.",
        parameters_schema={
            "type": "object",
            "properties": {"finding_id": {"type": "string"}},
            "required": ["finding_id"],
        },
        handler=_tool,
    ))
