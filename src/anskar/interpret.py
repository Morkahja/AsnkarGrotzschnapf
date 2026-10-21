"""Turn a source text into one visual prompt with the local Ollama model.

The source text is sent in full. A text that does not fit the context window
is refused, and nothing is cut down to make it fit.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from anskar.artwork.generator import prompt_hash
from anskar.style import StyleChoice

MODEL = "qwen3:8b"
TEMPERATURE = 0.7
NUM_PREDICT = 380
DEFAULT_NUM_CTX = 4096
RESPONSE_RESERVE = 400
TEMPLATE_RESERVE = 64
CHAT_TIMEOUT = 180.0

DEFAULT_ABSTRACTION = "surreal"
ABSTRACTIONS = ("gentle", "surreal", "experimental")

_IDENTITY = """You construct pictures for Anskar Grotzschnapf.

Read the entire source text. Transform its emotional meaning into one imaginative visual concept. Do not illustrate the text as a photograph, a scenic landscape, a centered subject, or a literal scene.

Use dream logic, unusual forms, unexpected metaphors, expressive brushwork, and inventive color. Let the house style change the surface temperament. Do not let it choose the same motif, palette, or arrangement for every text.

The picture must not contain typography, lettering, captions, quotations, or written words.
"""

_CONSTRUCTION = {
    "gentle": """Construction: Gentle.

Start with one subject a viewer can still name, placed in a readable space. Then change the picture's structure in one way that a camera could not record: scale, gravity, material, or time. Keep every other relation believable. Paint the surface with visible strokes and air. Do not add a second impossible system, and do not pose the subject as a studio portrait. Do not stand that subject in an ordinary field under a moon.
""",
    "surreal": """Construction: Surreal Abstract.

Do not follow the text object by object. Find its emotional contradiction and invent one metaphor that can be seen. The poem's creature, child, or main object must not appear as a figure standing in a landscape. Turn the feeling into an impossible object, architecture, or event, and place it off the center. Join a few recognizable fragments to forms that have no everyday name. Refuse a scenic horizon.
""",
    "experimental": """Construction: Experimental.

Do not begin with a person, animal, object, or place. Do not draw the poem's creature, a face, or a horizon. Translate the emotion into intervals of color, colliding shapes, texture, and movement. A recognizable trace may appear inside that field, and the picture must still hold if the trace is removed. There is no default focal point.
""",
}

_CLOSING = """Write one coherent image prompt of about 80 to 150 words. Include the concept, the composition, the forms, the surreal or abstract event, the colors, the light, the texture, and the emotional atmosphere.

Do not repeat the original text. Do not quote its lines. Do not mention that a source text existed. Never describe letters, words, captions, or marks that could be read.

Reply with the image prompt only. No title, no explanation, and no markdown.
"""

_REVISION = {
    "surreal": (
        "Rewrite the draft. Remove every creature, child, and standing figure that comes from the source. "
        "Keep one impossible object or room, away from the center. "
        "Return only the image prompt."
    ),
    "experimental": (
        "Rewrite the draft. Remove every creature, face, and horizon. "
        "Keep the picture as color, shape, texture, and movement. "
        "Return only the image prompt."
    ),
}


_USER_METHOD = {
    "gentle": (
        "Keep one nameable subject and break realism in a single structural way. "
        "Change its material or its gravity. Do not put it in a meadow under a sky."
    ),
    "surreal": (
        "Invent a visible metaphor instead of illustrating the text in order. "
        "Do not depict the text's creature as a figure in a landscape. "
        "Build an impossible object or room, and keep it away from the center."
    ),
    "experimental": (
        "Start from color, shape, and movement, not from a depicted subject. "
        "Do not draw the text's creature, a face, or a horizon."
    ),
}


def require_abstraction(value: str) -> str:
    if value not in ABSTRACTIONS:
        raise InterpreterError("Abstraction must be gentle, surreal, or experimental.")
    return value


def system_instruction(abstraction: str = DEFAULT_ABSTRACTION) -> str:
    level = require_abstraction(abstraction)
    return f"{_IDENTITY}\n{_CONSTRUCTION[level]}\n{_CLOSING}"


SYSTEM_INSTRUCTION = system_instruction(DEFAULT_ABSTRACTION)

_CONTEXT_ERROR = re.compile(
    r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)"
)


class InterpreterError(Exception):
    """The interpreter could not produce a visual prompt."""


class OllamaUnavailable(InterpreterError):
    """The local Ollama server is not accepting connections."""


class ContextWindowError(InterpreterError):
    """The source text does not fit, and it was not truncated."""


@dataclass(frozen=True)
class Interpretation:
    source_text: str
    prompt: str
    style_preset: str
    style_prompt: str
    house_style: bool
    abstraction: str
    model: str
    created_at: str
    duration_seconds: float
    source_sha256: str
    prompt_sha256: str
    ollama: dict[str, object]

    def to_record(self) -> dict[str, object]:
        return {
            "source_text": self.source_text,
            "interpretation": self.prompt,
            "interpretation_sha256": self.prompt_sha256,
            "source_sha256": self.source_sha256,
            "style_preset": self.style_preset,
            "style_prompt": self.style_prompt,
            "house_style": self.house_style,
            "abstraction": self.abstraction,
            "created_at": self.created_at,
            "duration_seconds": self.duration_seconds,
            "ollama": self.ollama,
        }


def user_message(
    source: str,
    choice: StyleChoice,
    abstraction: str = DEFAULT_ABSTRACTION,
) -> str:
    """House style, the construction rule, and the complete source text."""
    level = require_abstraction(abstraction)
    if choice.enabled and choice.prompt.strip():
        style = f"House style: {choice.preset}\n\n{choice.prompt.strip()}"
    else:
        style = (
            "House style: none. Choose a surface that belongs to this text. "
            "Do not apply the Dreamlike, Painterly, or Symbolic formulas."
        )
    return (
        f"{style}\n\n"
        f"Source text:\n{source}\n\n"
        "Now construct the picture. The house style changes only the surface.\n"
        f"{_USER_METHOD[level]}\n"
        "The picture contains no writing, letters, captions, or readable marks."
    )


def estimate_tokens(text: str) -> int:
    """Overestimate a Qwen-sized token count so a shortfall is visible early."""
    if text == "":
        return 0
    return max(len(text.split()), (len(text) + 3) // 4) + 1


def context_budget(*parts: str, model_context: int) -> int:
    """Return the context size to request, or refuse a text that cannot fit."""
    if model_context < RESPONSE_RESERVE + TEMPLATE_RESERVE + 32:
        raise ContextWindowError(
            f"The Ollama context window is {model_context} tokens. "
            "The source text was not truncated."
        )
    needed = sum(estimate_tokens(part) for part in parts) + RESPONSE_RESERVE + TEMPLATE_RESERVE
    if needed > model_context:
        raise ContextWindowError(
            f"The source text needs about {needed} tokens and the Ollama context window "
            f"is {model_context} tokens. The source text was not truncated."
        )
    rounded = ((needed + 255) // 256) * 256
    return min(model_context, max(DEFAULT_NUM_CTX, rounded))


def clean_visual_prompt(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        cleaned = cleaned[1:-1].strip()
    for label in ("Image prompt:", "Prompt:"):
        if cleaned.startswith(label):
            cleaned = cleaned[len(label) :].strip()
    return cleaned


class OllamaInterpreter:
    def __init__(
        self,
        *,
        host: str | None = None,
        model: str = MODEL,
        transport=None,
        context_length: int | None = None,
    ) -> None:
        self.host = _local_host(host if host is not None else os.environ.get("ANSKAR_OLLAMA_HOST", "http://127.0.0.1:11434"))
        self.model = model
        self._transport = transport or _http_json
        self._context_length = context_length

    def interpret(
        self,
        source: str,
        choice: StyleChoice,
        abstraction: str = DEFAULT_ABSTRACTION,
    ) -> Interpretation:
        level = require_abstraction(abstraction)
        if source.strip() == "":
            raise InterpreterError("There is no text to interpret.")
        if choice.enabled and choice.prompt.strip() == "":
            raise InterpreterError("The house style prompt is empty.")
        instruction = system_instruction(level)
        self._ensure_model()
        context_length = self.context_length()
        message = user_message(source, choice, level)
        num_ctx = context_budget(instruction, message, model_context=context_length)
        started = time.monotonic()
        revised = False
        try:
            payload, num_ctx = self._chat(message, num_ctx, context_length, instruction)
            content = payload.get("message", {}).get("content", "")
            if not isinstance(content, str):
                raise InterpreterError("Ollama returned an unreadable response.")
            if level in _REVISION and content.strip():
                revised_text = self._revise(message, content, num_ctx, instruction, level)
                if revised_text != content:
                    revised = True
                    content = revised_text
        finally:
            self._unload_quietly()
        prompt = clean_visual_prompt(content)
        if prompt == "":
            raise InterpreterError("Ollama returned an empty visual prompt.")
        created = payload.get("created_at")
        if not isinstance(created, str) or created == "":
            created = datetime.now(timezone.utc).isoformat()
        settings = {
            "model": self.model,
            "host": self.host,
            "temperature": TEMPERATURE,
            "num_ctx": num_ctx,
            "num_predict": NUM_PREDICT,
            "think": False,
            "truncate": False,
            "context_length": context_length,
            "abstraction": level,
            "revised": revised,
            "prompt_eval_count": payload.get("prompt_eval_count"),
            "eval_count": payload.get("eval_count"),
            "created_at": created,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        return Interpretation(
            source_text=source,
            prompt=prompt,
            style_preset=choice.preset,
            style_prompt=choice.prompt,
            house_style=choice.enabled,
            abstraction=level,
            model=self.model,
            created_at=created,
            duration_seconds=float(settings["duration_seconds"]),
            source_sha256=prompt_hash(source),
            prompt_sha256=prompt_hash(prompt),
            ollama=settings,
        )

    def release_for_image_generation(self) -> None:
        """Unload the interpreter model so image generation can use the GPU."""
        try:
            running = self._request("GET", "/api/ps", None, 5)
        except OllamaUnavailable:
            return
        if not _model_loaded(running, self.model):
            return
        self._unload()
        again = self._request("GET", "/api/ps", None, 5)
        if _model_loaded(again, self.model):
            raise InterpreterError(
                f"Ollama still has {self.model} loaded. Image generation was not started."
            )

    def context_length(self) -> int:
        if self._context_length is not None:
            return self._context_length
        shown = self._request("POST", "/api/show", {"model": self.model}, 15)
        info = shown.get("model_info", {})
        if not isinstance(info, dict):
            info = {}
        lengths = [value for key, value in info.items() if key.endswith("context_length") and isinstance(value, int)]
        if not lengths:
            raise InterpreterError(f"Ollama did not report a context length for {self.model}.")
        self._context_length = lengths[0]
        return self._context_length

    def _ensure_model(self) -> None:
        tags = self._request("GET", "/api/tags", None, 5)
        names = _model_names(tags)
        if self.model not in names:
            raise InterpreterError(f"Ollama model {self.model} is not installed.")

    def _chat(self, message: str, num_ctx: int, model_context: int, instruction: str) -> tuple[dict, int]:
        try:
            return (
                self._request(
                    "POST",
                    "/api/chat",
                    _chat_body(self.model, message, num_ctx, instruction),
                    CHAT_TIMEOUT,
                ),
                num_ctx,
            )
        except ContextWindowError as exc:
            match = _CONTEXT_ERROR.search(str(exc))
            if match is None:
                raise
            request_tokens = int(match.group(1))
            needed = request_tokens + RESPONSE_RESERVE
            expanded = min(model_context, ((needed + 255) // 256) * 256)
            if needed > model_context or expanded <= num_ctx:
                raise ContextWindowError(
                    f"The source text uses {request_tokens} tokens and the Ollama context window "
                    f"is {model_context} tokens. The source text was not truncated."
                ) from exc
            try:
                payload = self._request(
                    "POST",
                    "/api/chat",
                    _chat_body(self.model, message, expanded, instruction),
                    CHAT_TIMEOUT,
                )
            except ContextWindowError as second:
                raise ContextWindowError(
                    f"The source text uses {request_tokens} tokens and the Ollama context window "
                    f"is {model_context} tokens. The source text was not truncated."
                ) from second
            return payload, expanded

    def _revise(self, message: str, draft: str, num_ctx: int, instruction: str, level: str) -> str:
        body = _chat_body(self.model, message, num_ctx, instruction)
        body["messages"].append({"role": "assistant", "content": draft})
        body["messages"].append({"role": "user", "content": _REVISION[level]})
        payload = self._request("POST", "/api/chat", body, CHAT_TIMEOUT)
        content = payload.get("message", {}).get("content", "")
        if not isinstance(content, str) or content.strip() == "":
            return draft
        return content

    def _unload(self) -> None:
        self._request("POST", "/api/generate", {"model": self.model, "keep_alive": 0}, 30)

    def _unload_quietly(self) -> None:
        try:
            self._unload()
        except InterpreterError:
            return

    def _request(self, method: str, path: str, payload: dict | None, timeout: float) -> dict:
        try:
            return self._transport(method, f"{self.host}{path}", payload, timeout)
        except ContextWindowError:
            raise
        except OllamaUnavailable:
            raise
        except InterpreterError:
            raise
        except Exception as exc:
            raise InterpreterError(f"Ollama request failed: {exc}") from exc


def _chat_body(model: str, message: str, num_ctx: int, instruction: str) -> dict:
    return {
        "model": model,
        "stream": False,
        "think": False,
        "truncate": False,
        "keep_alive": 0,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": message},
        ],
        "options": {
            "temperature": TEMPERATURE,
            "num_predict": NUM_PREDICT,
            "num_ctx": num_ctx,
        },
    }


def _model_names(payload: dict) -> set[str]:
    models = payload.get("models", [])
    if not isinstance(models, list):
        return set()
    names = set()
    for item in models:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.add(item["name"])
    return names


def _model_loaded(payload: dict, model: str) -> bool:
    return model in _model_names(payload)


def _local_host(host: str) -> str:
    parsed = urlparse(host)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise InterpreterError("Ollama must stay on this computer.")
    if parsed.port is None:
        return f"{parsed.scheme}://{parsed.hostname}"
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


def _http_json(method: str, url: str, payload: dict | None, timeout: float) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(detail).get("error") or detail
        except json.JSONDecodeError:
            message = detail
        if _CONTEXT_ERROR.search(str(message)):
            raise ContextWindowError(str(message)) from exc
        if exc.code == 404:
            raise InterpreterError(f"Ollama could not find the model. {message}".strip()) from exc
        raise InterpreterError(f"Ollama returned status {exc.code}. {message}".strip()) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        host = urlparse(url)
        where = host.hostname or "127.0.0.1"
        port = host.port or 11434
        raise OllamaUnavailable(f"Ollama is not running at {where}:{port}.") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InterpreterError("Ollama returned an unreadable response.") from exc
    if not isinstance(parsed, dict):
        raise InterpreterError("Ollama returned an unreadable response.")
    if parsed.get("error"):
        message = str(parsed["error"])
        if _CONTEXT_ERROR.search(message):
            raise ContextWindowError(message)
        raise InterpreterError(message)
    return parsed
