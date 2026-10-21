"""Local-only browser page for prompt experiments."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from anskar.artwork.generator import GenerationError
from anskar.config import database_path
from anskar.artwork.settings import load_settings
from anskar.interpret import DEFAULT_ABSTRACTION, InterpreterError, OllamaInterpreter, require_abstraction
from anskar.printmaker import PrintError, approve_artwork
from anskar.lab.experiments import LabRequest, experiments_dir, list_experiments, resolve_media, run_experiment
from anskar.lab.prompts import compose_prompt, first_stanza
from anskar.models import SAMPLERS, SCHEDULERS, installed_models, require_model
from anskar.style import PRESETS, lab_style, production_style

PAGE = Path(__file__).with_name("page.html")
MAX_BODY = 1_000_000


class LabHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/defaults":
            settings = load_settings()
            choice = production_style()
            models = [profile.to_public() for profile in installed_models(settings)]
            self._json(
                200,
                {
                    "width": settings.width,
                    "height": settings.height,
                    "steps": settings.steps,
                    "cfg_scale": settings.cfg_scale,
                    "sampler": settings.sampling_method,
                    "house_style": choice.enabled,
                    "style_preset": choice.preset,
                    "style_prompt": choice.prompt,
                    "style_composition": choice.composition,
                    "presets": PRESETS,
                    "models": models,
                    "samplers": list(SAMPLERS),
                    "schedulers": list(SCHEDULERS),
                },
            )
            return
        if path == "/api/models":
            self._json(
                200,
                {"models": [profile.to_public() for profile in installed_models(self.server.settings)]},
            )
            return
        if path == "/api/experiments":
            self._json(200, {"experiments": list_experiments(self.server.experiments)})
            return
        if path.startswith("/media/"):
            relative = unquote(path.removeprefix("/media/"))
            try:
                image = resolve_media(self.server.experiments, relative)
            except ValueError as exc:
                self._json(404, {"error": str(exc)})
                return
            self._send(200, image.read_bytes(), "image/png")
            return
        self._json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        if path == "/api/preview":
            self._preview(payload)
            return
        if path == "/api/first-stanza":
            body = payload.get("body")
            if not isinstance(body, str):
                self._json(400, {"error": "Text must be a string."})
                return
            self._json(200, {"stanza": first_stanza(body)})
            return
        if path == "/api/generate":
            self._generate(payload)
            return
        if path == "/api/interpret":
            self._interpret(payload)
            return
        if path == "/api/print/approve":
            self._approve(payload)
            return
        self._json(404, {"error": "Not found."})

    def log_message(self, format: str, *args) -> None:
        return

    def _preview(self, payload: dict) -> None:
        from anskar.artwork.generator import prompt_hash
        from anskar.lab.prompts import compose_prompt
        from anskar.style import apply_style, lab_style

        try:
            choice = lab_style(**_style_fields(payload))
            source = compose_prompt(
                _text(payload, "mode"),
                _text(payload, "title", required=False),
                _text(payload, "body", required=False),
            )
            method = _method(payload)
            if method == "interpreted":
                prompt = _text(payload, "visual_prompt", required=False)
            else:
                prompt = apply_style(source, choice)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(
            200,
            {
                "prompt": prompt,
                "prompt_sha256": prompt_hash(prompt),
                "house_style": choice.enabled,
                "style_preset": choice.preset,
                "style_prompt": choice.prompt,
                "style_composition": choice.composition,
                "method": method,
                "source_text": source,
            },
        )

    def _interpret(self, payload: dict) -> None:
        interpreter = getattr(self.server, "interpreter", None)
        if interpreter is None:
            self._json(400, {"error": "Ollama is not available."})
            return
        try:
            choice = lab_style(**_style_fields(payload))
            source = compose_prompt(
                _text(payload, "mode"),
                _text(payload, "title", required=False),
                _text(payload, "body", required=False),
            )
            result = interpreter.interpret(source, choice, _abstraction(payload))
        except (ValueError, InterpreterError) as exc:
            self._json(400, {"error": str(exc)})
            return
        record = result.to_record()
        record["prompt"] = result.prompt
        self._json(200, record)

    def _generate(self, payload: dict) -> None:
        interpreter = getattr(self.server, "interpreter", None)
        if interpreter is not None:
            try:
                interpreter.release_for_image_generation()
            except InterpreterError as exc:
                self._json(400, {"error": str(exc)})
                return
        try:
            style = _style_fields(payload)
            model_id = _text(payload, "model_id", required=False) or "z-image-turbo"
            profile = require_model(model_id, self.server.settings)
            if "negative_enabled" in payload:
                negative_enabled = payload.get("negative_enabled")
                if not isinstance(negative_enabled, bool):
                    raise ValueError("Negative prompt must be true or false.")
                negative_prompt = _text(payload, "negative_prompt", required=False)
            else:
                negative_enabled = profile.negative_enabled
                negative_prompt = profile.negative_prompt if negative_enabled else ""
            request = LabRequest(
                title=_text(payload, "title", required=False),
                body=_text(payload, "body", required=False),
                mode=_text(payload, "mode"),
                experiment=_text(payload, "experiment", required=False),
                seed=_optional_int(payload.get("seed"), "Seed"),
                width=_required_int(payload.get("width"), "Width"),
                height=_required_int(payload.get("height"), "Height"),
                steps=_required_int(payload.get("steps"), "Steps"),
                cfg_scale=_required_float(payload.get("cfg_scale"), "CFG scale"),
                house_style=style["enabled"],
                style_preset=style["preset"],
                style_prompt=style["prompt"],
                style_composition=style["composition"],
                model_id=model_id,
                sampler=_text(payload, "sampler", required=False),
                scheduler=_scheduler_field(payload),
                clip_skip=_clip_skip_field(payload),
                clip_skip_set="clip_skip" in payload,
                negative_enabled=negative_enabled,
                negative_prompt=negative_prompt,
                method=_method(payload),
                visual_prompt=_text(payload, "visual_prompt", required=False),
                interpretation=_text(payload, "interpretation", required=False),
                source_text=_text(payload, "source_text", required=False),
                ollama=_ollama_field(payload),
                abstraction=_abstraction(payload),
            )
            result = run_experiment(
                request,
                settings=self.server.settings,
                runner=self.server.runner,
                directory=self.server.experiments,
            )
        except (ValueError, GenerationError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(
            200,
            {
                "title": result.title,
                "body": result.body,
                "prompt": result.prompt,
                "mode": result.mode,
                "seed": result.seed,
                "steps": result.steps,
                "cfg_scale": result.cfg_scale,
                "width": result.width,
                "height": result.height,
                "prompt_sha256": result.prompt_sha256,
                "duration_seconds": result.duration_seconds,
                "output_path": result.output_path,
                "metadata_path": result.metadata_path,
                "image_url": result.image_url,
                "experiment": result.experiment,
                "model_paths": result.model_paths,
                "house_style": result.house_style,
                "style_preset": result.style_preset,
                "style_prompt": result.style_prompt,
                "style_composition": result.style_composition,
                "model_id": result.model_id,
                "model_name": result.model_name,
                "model_path": result.model_path,
                "command_type": result.command_type,
                "negative_prompt": result.negative_prompt,
                "sampler": result.sampler,
                "scheduler": result.scheduler,
                "clip_skip": result.clip_skip,
                "method": result.method,
                "source_text": result.source_text,
                "source_sha256": result.source_sha256,
                "visual_prompt": result.visual_prompt,
                "interpretation": result.interpretation,
                "interpretation_sha256": result.interpretation_sha256,
                "interpretation_edited": result.interpretation_edited,
                "ollama": result.ollama,
                "abstraction": result.abstraction,
                "metadata_file": Path(result.metadata_path).resolve().relative_to(
                    Path(self.server.experiments).resolve()
                ).as_posix(),
            },
        )

    def _approve(self, payload: dict) -> None:
        interpreter = getattr(self.server, "interpreter", None)
        if interpreter is not None:
            try:
                interpreter.release_for_image_generation()
            except InterpreterError as exc:
                self._json(400, {"error": str(exc)})
                return
        relative = _text(payload, "metadata_file")
        try:
            metadata = _experiment_metadata(self.server.experiments, relative)
            manifest = approve_artwork(
                metadata,
                experiments=self.server.experiments,
                database=self.server.database,
                settings=self.server.settings,
                runner=self.server.runner,
            )
        except (ValueError, PrintError, GenerationError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, manifest)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY:
            raise ValueError("Request is too large.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request must be JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request must be a JSON object.")
        return payload

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(
    host: str,
    port: int,
    *,
    settings=None,
    runner=None,
    experiments: Path | None = None,
    interpreter=None,
):
    if host != "127.0.0.1":
        raise ValueError("The prompt lab binds to 127.0.0.1 only.")

    class BoundHandler(LabHandler):
        pass

    httpd = ThreadingHTTPServer((host, port), BoundHandler)
    httpd.settings = settings if settings is not None else load_settings()
    httpd.runner = runner
    httpd.experiments = experiments if experiments is not None else experiments_dir()
    httpd.interpreter = interpreter
    httpd.database = database_path()
    return httpd


def serve(port: int) -> None:
    httpd = make_server("127.0.0.1", port, interpreter=OllamaInterpreter())
    address, bound_port = httpd.server_address
    print(f"Prompt lab: http://{address}:{bound_port}/", flush=True)
    print(f"Experiments: {httpd.experiments}", flush=True)
    print("The poetry archive is not modified.", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def _scheduler_field(payload: dict) -> str | None:
    if "scheduler" not in payload:
        return None
    value = payload.get("scheduler")
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("Scheduler must be a string.")
    return value


def _clip_skip_field(payload: dict) -> int | None:
    if "clip_skip" not in payload:
        return None
    return _optional_int(payload.get("clip_skip"), "Clip skip")


def _abstraction(payload: dict) -> str:
    value = payload.get("abstraction", DEFAULT_ABSTRACTION)
    if value is None or value == "":
        value = DEFAULT_ABSTRACTION
    if not isinstance(value, str):
        raise ValueError("Abstraction must be a string.")
    return require_abstraction(value)


def _experiment_metadata(experiments: Path, relative: str) -> Path:
    if relative == "" or relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        raise ValueError("Experiment file is not available.")
    root = experiments.resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ValueError("Experiment file is not available.")
    if path.suffix.lower() != ".json" or not path.is_file():
        raise ValueError("Experiment file is not available.")
    return path


def _method(payload: dict) -> str:
    value = payload.get("method", "direct")
    if value is None or value == "":
        value = "direct"
    if value not in ("direct", "interpreted"):
        raise ValueError("Method must be direct or interpreted.")
    return value


def _ollama_field(payload: dict) -> dict | None:
    if "ollama" not in payload or payload.get("ollama") is None:
        return None
    value = payload.get("ollama")
    if not isinstance(value, dict):
        raise ValueError("Ollama settings must be an object.")
    kept = {}
    for key in (
        "model",
        "host",
        "temperature",
        "num_ctx",
        "num_predict",
        "think",
        "truncate",
        "context_length",
        "prompt_eval_count",
        "eval_count",
        "created_at",
        "duration_seconds",
    ):
        if key in value:
            kept[key] = value[key]
    return kept


def _style_fields(payload: dict) -> dict[str, object]:
    enabled = payload.get("house_style", False)
    preset = payload.get("style_preset", "none")
    prompt = payload.get("style_prompt", "")
    composition = payload.get("style_composition", "text")
    if not isinstance(enabled, bool):
        raise ValueError("House style must be true or false.")
    if not isinstance(preset, str):
        raise ValueError("Style preset must be a string.")
    if not isinstance(prompt, str):
        raise ValueError("Style prompt must be a string.")
    if not isinstance(composition, str):
        raise ValueError("Composition must be a string.")
    return {
        "enabled": enabled,
        "preset": preset,
        "prompt": prompt,
        "composition": composition,
    }


def _text(payload: dict, key: str, *, required: bool = True) -> str:
    value = payload.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string.")
    if required and value.strip() == "":
        raise ValueError(f"{key} is required.")
    return value


def _required_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer.")
    return value


def _optional_int(value: object, label: str) -> int | None:
    if value is None or value == "":
        return None
    return _required_int(value, label)


def _required_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number.")
    return float(value)
