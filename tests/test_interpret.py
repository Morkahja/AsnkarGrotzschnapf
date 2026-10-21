import unittest

from anskar.interpret import (
    MODEL,
    SYSTEM_INSTRUCTION,
    ContextWindowError,
    Interpretation,
    InterpreterError,
    OllamaInterpreter,
    OllamaUnavailable,
    clean_visual_prompt,
    context_budget,
    system_instruction,
    user_message,
)
from anskar.style import PRESETS, lab_style

POEM = "Little lamb, who made thee?\nDoes thou know who made thee?"


class BudgetTests(unittest.TestCase):
    def test_a_short_text_uses_the_default_context(self):
        choice = lab_style(enabled=True, preset="painterly", prompt=PRESETS["painterly"], composition="wrapped")
        message = user_message(POEM, choice)
        self.assertIn(POEM, message)
        self.assertEqual(message.count(POEM), 1)
        self.assertIn("painterly", message)
        size = context_budget(SYSTEM_INSTRUCTION, message, model_context=40960)
        self.assertEqual(size, 4096)

    def test_a_text_past_the_window_is_refused_whole(self):
        original = "word " * 5000
        with self.assertRaises(ContextWindowError) as caught:
            context_budget(SYSTEM_INSTRUCTION, original, model_context=512)
        self.assertIn("not truncated", str(caught.exception))
        self.assertEqual(original, "word " * 5000)

    def test_the_instruction_keeps_the_source_out_of_the_picture(self):
        self.assertIn("entire source text", SYSTEM_INSTRUCTION)
        self.assertIn("80 to 150 words", SYSTEM_INSTRUCTION)
        self.assertIn("Do not repeat the original text", SYSTEM_INSTRUCTION)
        self.assertIn("typography", SYSTEM_INSTRUCTION)
        self.assertIn("dream logic", SYSTEM_INSTRUCTION)

    def test_abstraction_levels_change_the_construction(self):
        gentle = system_instruction("gentle")
        surreal = system_instruction("surreal")
        experimental = system_instruction("experimental")
        self.assertIn("one subject a viewer can still name", gentle)
        self.assertIn("Do not follow the text object by object", surreal)
        self.assertIn("Do not begin with a person", experimental)
        self.assertNotIn("Do not begin with a person", gentle)
        self.assertNotIn("one subject a viewer can still name", experimental)
        choice = lab_style(enabled=True, preset="painterly", prompt=PRESETS["painterly"], composition="wrapped")
        message = user_message(POEM, choice, "experimental")
        self.assertIn(POEM, message)
        self.assertIn("Start from color, shape, and movement", message)
        self.assertNotIn("Keep one nameable subject", message)
        self.assertEqual(system_instruction("surreal"), SYSTEM_INSTRUCTION)


class InterpreterTests(unittest.TestCase):
    def test_mocked_ollama_returns_an_editable_prompt(self):
        transport = _Transport()
        interpreter = OllamaInterpreter(transport=transport, context_length=40960)
        choice = lab_style(enabled=True, preset="painterly", prompt=PRESETS["painterly"], composition="wrapped")
        result = interpreter.interpret(POEM, choice)
        self.assertIsInstance(result, Interpretation)
        self.assertEqual(result.prompt, "A small lamb stands in gold grass beneath a quiet sky.")
        self.assertEqual(result.source_text, POEM)
        self.assertEqual(result.model, MODEL)
        self.assertEqual(result.ollama["think"], False)
        self.assertEqual(result.ollama["truncate"], False)
        self.assertEqual(result.ollama["temperature"], 0.7)
        chat = [payload for method, url, payload in transport.calls if url.endswith("/api/chat")]
        self.assertEqual(len(chat), 2)
        self.assertIn("Remove every creature", chat[1]["messages"][3]["content"])
        self.assertEqual(chat[0]["truncate"], False)
        self.assertEqual(chat[0]["think"], False)
        self.assertEqual(chat[0]["keep_alive"], 0)
        self.assertIn(POEM, chat[0]["messages"][1]["content"])
        self.assertTrue(any(url.endswith("/api/generate") for _method, url, _payload in transport.calls))

    def test_context_overflow_does_not_call_the_model(self):
        transport = _Transport()
        interpreter = OllamaInterpreter(transport=transport, context_length=256)
        choice = lab_style(enabled=True, preset="symbolic", prompt=PRESETS["symbolic"], composition="wrapped")
        with self.assertRaises(ContextWindowError):
            interpreter.interpret("word " * 400, choice)
        self.assertFalse(any(url.endswith("/api/chat") for _method, url, _payload in transport.calls))

    def test_a_reported_overflow_can_expand_once_and_then_stops(self):
        transport = _Transport(overflow_once=True)
        interpreter = OllamaInterpreter(transport=transport, context_length=8192)
        choice = lab_style(enabled=False, preset="none", prompt="", composition="text")
        result = interpreter.interpret(POEM, choice)
        chats = [payload for _method, url, payload in transport.calls if url.endswith("/api/chat")]
        self.assertEqual(len(chats), 3)
        self.assertGreater(chats[1]["options"]["num_ctx"], chats[0]["options"]["num_ctx"])
        self.assertEqual(result.ollama["num_ctx"], chats[1]["options"]["num_ctx"])
        self.assertIn("House style: none", chats[0]["messages"][1]["content"])

    def test_remote_hosts_are_refused(self):
        with self.assertRaises(InterpreterError):
            OllamaInterpreter(host="https://example.com")

    def test_a_closed_port_says_ollama_is_not_running(self):
        from anskar.interpret import _http_json

        with self.assertRaises(OllamaUnavailable) as caught:
            _http_json("GET", "http://127.0.0.1:1/api/tags", None, 2)
        self.assertIn("Ollama is not running at 127.0.0.1:1", str(caught.exception))

    def test_fences_are_removed_and_the_prompt_is_kept(self):
        self.assertEqual(
            clean_visual_prompt("```\nA quiet field.\n```"),
            "A quiet field.",
        )


class _Transport:
    def __init__(self, overflow_once: bool = False):
        self.calls = []
        self.overflow_once = overflow_once
        self.chats = 0

    def __call__(self, method, url, payload, timeout):
        self.calls.append((method, url, payload))
        if url.endswith("/api/tags"):
            return {"models": [{"name": MODEL}]}
        if url.endswith("/api/ps"):
            return {"models": []}
        if url.endswith("/api/generate"):
            return {"done": True}
        if url.endswith("/api/chat"):
            self.chats += 1
            if self.overflow_once and self.chats == 1:
                raise ContextWindowError(
                    "request (5000 tokens) exceeds the available context size (4096 tokens)"
                )
            return {
                "created_at": "2026-10-21T01:00:00Z",
                "message": {"role": "assistant", "content": "A small lamb stands in gold grass beneath a quiet sky."},
                "prompt_eval_count": 30,
                "eval_count": 12,
            }
        raise AssertionError(url)


if __name__ == "__main__":
    unittest.main()
