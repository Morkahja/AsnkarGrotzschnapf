"""Production artistic direction checks for the daily path.

These checks reinforce Surreal Abstract + Painterly settings. They cannot
reliably judge gallery quality from metadata alone; they only catch clearly
unusable literal prompts before image generation.
"""

from __future__ import annotations

import re

from anskar.daily.errors import DailyError

PRODUCTION_ABSTRACTION = "surreal"
PRODUCTION_SUFFIX = (
    "Painterly fine-art surface with visible brushwork and symbolic atmosphere. "
    "Surreal abstract composition, dream logic, metaphor over literal depiction. "
    "Not a photograph, not photorealistic, no stock-photo look, no typography."
)

_LITERAL_CREATURE = re.compile(
    r"\b(lamb|sheep|ewe|ram|foal|horse|dog|cat|bird|child|infant|baby|"
    r"portrait|photograph|photorealistic|hyperrealistic|standing in a (?:field|meadow|pasture))\b",
    re.IGNORECASE,
)


def reinforce_production_prompt(visual_prompt: str) -> str:
    """Keep the interpreted prompt and append production surface constraints."""
    base = visual_prompt.strip()
    if not base:
        raise DailyError("The visual prompt is empty after interpretation.", kind="media")
    if PRODUCTION_SUFFIX.lower() in base.lower():
        return base
    return f"{base.rstrip('. ')}. {PRODUCTION_SUFFIX}"


def assert_usable_surreal_prompt(visual_prompt: str) -> None:
    """Refuse an obviously literal creature-in-landscape prompt for Surreal Abstract."""
    text = visual_prompt.strip()
    if not text:
        raise DailyError("The visual prompt is empty.", kind="media")
    # Ignore the production reinforcement clause; it mentions photograph only to forbid it.
    if PRODUCTION_SUFFIX in text:
        text = text.replace(PRODUCTION_SUFFIX, " ")
    hits = _LITERAL_CREATURE.findall(text)
    # Allow a metaphorical trace, but not a cluster of literal figurative cues.
    if len(hits) >= 2:
        raise DailyError(
            "The interpreted prompt stayed too literal for Surreal Abstract production "
            f"(matched: {', '.join(sorted({h.lower() for h in hits})[:5])}). "
            "The image was not generated.",
            kind="media",
        )
