"""House-style presets and prompt composition."""

import unittest

from anskar.style import PRESETS, apply_style, lab_style, production_style


LINE = "Little lamb, who made thee?"


class StyleTests(unittest.TestCase):
    def test_presets_are_the_house_texts(self):
        self.assertEqual(
            PRESETS["dreamlike"],
            "Create a poetic fine-art image inspired by the following text. "
            "Use a dreamlike, slightly abstract, luminous visual language with soft atmosphere, "
            "emotional subtlety, gentle surrealism, and elegant color harmony. "
            "Favor metaphor and mood over literal depiction. "
            "Avoid a plain photorealistic or stock-photo look.",
        )
        self.assertEqual(
            PRESETS["painterly"],
            "Create a poetic fine-art image inspired by the following text. "
            "Use a painterly, softly textured, symbolic visual language with refined composition, "
            "subtle abstraction, and a gallery-art aesthetic. "
            "Favor emotional resonance, atmosphere, and visual poetry over literal depiction. "
            "Avoid a plain photorealistic or stock-photo look.",
        )
        self.assertEqual(
            PRESETS["symbolic"],
            "Create a poetic fine-art image inspired by the following text. "
            "Interpret it in a symbolic, slightly abstract, emotionally evocative way. "
            "Focus on metaphor, atmosphere, and contemplative mood rather than direct illustration. "
            "Keep the result elegant, distinctive, and suitable as an art print.",
        )

    def test_three_compositions_leave_the_selected_text_intact(self):
        style = PRESETS["painterly"]
        self.assertEqual(
            apply_style(LINE, lab_style(enabled=True, preset="painterly", prompt=style, composition="text")),
            LINE,
        )
        self.assertEqual(
            apply_style(
                LINE,
                lab_style(enabled=True, preset="painterly", prompt=style, composition="style_text"),
            ),
            f"{style}\n\n{LINE}",
        )
        wrapped = apply_style(
            LINE,
            lab_style(enabled=True, preset="painterly", prompt=style, composition="wrapped"),
        )
        self.assertEqual(wrapped, f"{style}\n\nThe following text:\n{LINE}")
        self.assertTrue(wrapped.endswith(LINE))

    def test_disabled_house_style_sends_the_text_alone(self):
        choice = lab_style(
            enabled=False,
            preset="symbolic",
            prompt=PRESETS["symbolic"],
            composition="wrapped",
        )
        self.assertEqual(apply_style(LINE, choice), LINE)
        self.assertFalse(choice.enabled)
        self.assertEqual(choice.prompt, PRESETS["symbolic"])

    def test_edited_preset_is_recorded_as_custom(self):
        edited = PRESETS["dreamlike"] + " Keep the moon pale."
        choice = lab_style(enabled=True, preset="dreamlike", prompt=edited, composition="style_text")
        self.assertEqual(choice.preset, "custom")
        self.assertEqual(choice.prompt, edited)
        self.assertEqual(apply_style(LINE, choice), f"{edited}\n\n{LINE}")

    def test_empty_style_is_refused_when_it_would_be_sent(self):
        choice = lab_style(enabled=True, preset="custom", prompt="  ", composition="wrapped")
        with self.assertRaises(ValueError):
            apply_style(LINE, choice)

    def test_production_default_is_painterly_wrapped(self):
        choice = production_style()
        self.assertTrue(choice.enabled)
        self.assertEqual(choice.preset, "painterly")
        self.assertEqual(choice.prompt, PRESETS["painterly"])
        self.assertEqual(choice.composition, "wrapped")
        self.assertIn(LINE, apply_style(LINE, choice))
        plain = production_style("none")
        self.assertFalse(plain.enabled)
        self.assertEqual(apply_style(LINE, plain), LINE)


if __name__ == "__main__":
    unittest.main()
