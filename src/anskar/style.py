"""House-style presets and the ways they meet a selected text.

The selected text is never rewritten. A style is either left out, placed
before that text, or used as an instruction with the text in a labeled block.
"""

from dataclasses import dataclass

PRESETS: dict[str, str] = {
    "dreamlike": (
        "Create a poetic fine-art image inspired by the following text. "
        "Use a dreamlike, slightly abstract, luminous visual language with soft atmosphere, "
        "emotional subtlety, gentle surrealism, and elegant color harmony. "
        "Favor metaphor and mood over literal depiction. "
        "Avoid a plain photorealistic or stock-photo look."
    ),
    "painterly": (
        "Create a poetic fine-art image inspired by the following text. "
        "Use a painterly, softly textured, symbolic visual language with refined composition, "
        "subtle abstraction, and a gallery-art aesthetic. "
        "Favor emotional resonance, atmosphere, and visual poetry over literal depiction. "
        "Avoid a plain photorealistic or stock-photo look."
    ),
    "symbolic": (
        "Create a poetic fine-art image inspired by the following text. "
        "Interpret it in a symbolic, slightly abstract, emotionally evocative way. "
        "Focus on metaphor, atmosphere, and contemplative mood rather than direct illustration. "
        "Keep the result elegant, distinctive, and suitable as an art print."
    ),
}

COMPOSITIONS = ("text", "style_text", "wrapped")
PRODUCTION_PRESET = "painterly"
PRODUCTION_COMPOSITION = "wrapped"


@dataclass(frozen=True)
class StyleChoice:
    enabled: bool
    preset: str
    prompt: str
    composition: str


def production_style(
    preset: str = PRODUCTION_PRESET,
    composition: str = PRODUCTION_COMPOSITION,
) -> StyleChoice:
    """Style used by `anskar generate --pending`."""
    if preset == "none":
        return StyleChoice(False, "none", "", "text")
    if preset not in PRESETS:
        raise ValueError("Style must be dreamlike, painterly, symbolic, or none.")
    if composition not in COMPOSITIONS:
        raise ValueError("Composition must be text, style_text, or wrapped.")
    return StyleChoice(True, preset, PRESETS[preset], composition)


def lab_style(*, enabled: bool, preset: str, prompt: str, composition: str) -> StyleChoice:
    if not isinstance(enabled, bool):
        raise ValueError("House style must be true or false.")
    if preset not in (*PRESETS, "custom", "none"):
        raise ValueError("Unknown style preset.")
    if composition not in COMPOSITIONS:
        raise ValueError("Composition must be text, style_text, or wrapped.")
    if not isinstance(prompt, str):
        raise ValueError("Style prompt must be a string.")
    if len(prompt) > 20_000:
        raise ValueError("The style prompt is too long.")
    if enabled and preset in PRESETS and prompt != PRESETS[preset]:
        preset = "custom"
    if enabled and preset == "none":
        preset = "custom" if prompt.strip() else "none"
    return StyleChoice(enabled, preset, prompt, composition)


def apply_style(text: str, choice: StyleChoice) -> str:
    """Compose the prompt that is sent to the generator."""
    if choice.composition not in COMPOSITIONS:
        raise ValueError("Composition must be text, style_text, or wrapped.")
    if not choice.enabled or choice.composition == "text":
        return text
    style = choice.prompt.strip()
    if style == "":
        raise ValueError("Style prompt is empty.")
    if choice.composition == "style_text":
        return f"{style}\n\n{text}"
    return f"{style}\n\nThe following text:\n{text}"


def style_metadata(choice: StyleChoice, prompt: str) -> dict[str, object]:
    return {
        "house_style": choice.enabled,
        "style_preset": choice.preset,
        "style_prompt": choice.prompt,
        "style_composition": choice.composition,
        "prompt": prompt,
    }
