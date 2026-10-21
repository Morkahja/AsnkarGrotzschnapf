"""Generate one PNG from the pending poem with the local sd-cli."""

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anskar.archive.models import ArchiveError, NoPendingSelectionError, Selection
from anskar.archive.repository import get_pending, utc_now
from anskar.artwork.png import PRINT_LONG_EDGE, intended_print, png_size, print_sized
from anskar.artwork.settings import GeneratorSettings
from anskar.db import connect
from anskar.models import PRODUCTION_MODEL_ID, ModelProfile, production_model, require_model
from anskar.style import StyleChoice, apply_style, production_style, style_metadata

SEED_LIMIT = 2**31


class GenerationError(ArchiveError):
    """The local generator did not produce an artwork."""


class DuplicateArtworkError(ArchiveError):
    """This poem already has an artwork, or a generation is still running."""


@dataclass(frozen=True)
class ArtworkRecord:
    id: int
    poem_id: int
    selection_id: int
    title: str
    author: str
    prompt_sha256: str
    model: str
    seed: int
    width: int
    height: int
    duration_seconds: float | None
    settings_json: str
    output_path: str
    status: str
    error: str | None


@dataclass(frozen=True)
class GenerationPlan:
    selection: Selection
    prompt: str
    prompt_sha256: str
    settings: GeneratorSettings
    style: StyleChoice
    profile: ModelProfile
    source_text: str
    method: str
    abstraction: str | None
    ollama: dict[str, object] | None
    print_suitable: bool


class SubprocessRunner:
    """Run sd-cli from an argument list. The shell is never involved."""

    def run(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )


def plan_pending(
    database: Path,
    settings: GeneratorSettings,
    *,
    style: StyleChoice | None = None,
    interpreter=None,
    abstraction: str = "surreal",
    model_id: str = PRODUCTION_MODEL_ID,
    width: int | None = None,
    height: int | None = None,
) -> GenerationPlan:
    choice = production_style() if style is None else style
    active, profile = _settings_for_request(settings, model_id, width, height)
    _validate_settings(active)
    _require_installed_files(active)
    with connect(database) as connection:
        selection = _pending(connection)
        _clear_stale_running(connection, selection.poem.id, active.timeout_seconds)
        _reject_existing(connection, selection.poem.id)
    prompt, method, used_abstraction, ollama = _image_prompt(
        selection.poem.body,
        choice,
        interpreter,
        abstraction,
    )
    return GenerationPlan(
        selection,
        prompt,
        prompt_hash(prompt),
        active,
        choice,
        profile,
        selection.poem.body,
        method,
        used_abstraction,
        ollama,
        print_sized(active.width, active.height),
    )


def generate_pending(
    database: Path,
    settings: GeneratorSettings,
    *,
    runner: SubprocessRunner | None = None,
    seed: int | None = None,
    style: StyleChoice | None = None,
    interpreter=None,
    abstraction: str = "surreal",
    model_id: str = PRODUCTION_MODEL_ID,
    width: int | None = None,
    height: int | None = None,
) -> ArtworkRecord:
    plan = plan_pending(
        database,
        settings,
        style=style,
        interpreter=interpreter,
        abstraction=abstraction,
        model_id=model_id,
        width=width,
        height=height,
    )
    chosen_seed = secrets.randbelow(SEED_LIMIT) if seed is None else seed
    _validate_seed(chosen_seed)
    output = artwork_path(plan.settings.artworks_dir, plan.selection, chosen_seed)
    metadata = output.with_suffix(".json")
    if output.exists():
        raise GenerationError(f"Refusing to overwrite an existing artwork: {output}")
    if metadata.exists():
        raise GenerationError(f"Refusing to overwrite artwork metadata: {metadata}")
    output.parent.mkdir(parents=True, exist_ok=True)
    saved = plan.settings.to_record(seed=chosen_seed, prompt_sha256=plan.prompt_sha256)
    saved.update(style_metadata(plan.style, plan.prompt))
    saved.update(_profile_record(plan))
    saved.update(
        {
            "source_text": plan.source_text,
            "source_sha256": prompt_hash(plan.source_text),
            "method": plan.method,
            "abstraction": plan.abstraction,
            "visual_prompt": plan.prompt if plan.method == "interpreted" else "",
            "interpretation": plan.prompt if plan.method == "interpreted" else "",
            "ollama": plan.ollama,
            "print_suitable": plan.print_suitable,
            "negative_prompt": _negative_prompt(plan.profile),
        }
    )
    settings_json = json.dumps(saved, sort_keys=True)
    with connect(database) as connection:
        _clear_stale_running(connection, plan.selection.poem.id, plan.settings.timeout_seconds)
        artwork_id = _insert_running(
            connection,
            plan,
            seed=chosen_seed,
            output=output,
            settings_json=settings_json,
        )
    command = _image_command(plan, chosen_seed, output)
    started = time.monotonic()
    try:
        completed = (runner or SubprocessRunner()).run(command, timeout=plan.settings.timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        message = (
            f"Generation timed out after {plan.settings.timeout_seconds} seconds. "
            f"{_output_text(exc.stderr)}"
        )
        _finish(database, artwork_id, status="failed", error=message, duration=time.monotonic() - started)
        raise GenerationError(message) from exc
    except OSError as exc:
        message = f"Could not start the generator: {exc}"
        _finish(database, artwork_id, status="failed", error=message, duration=time.monotonic() - started)
        raise GenerationError(message) from exc
    duration = time.monotonic() - started
    if completed.returncode != 0:
        message = f"Generator exited with status {completed.returncode}. {_output_text(completed.stderr)}"
        _finish(database, artwork_id, status="failed", error=message, duration=duration)
        raise GenerationError(message.strip())
    try:
        measured = png_size(output)
    except (OSError, ValueError) as exc:
        message = f"Generator finished without a readable PNG: {exc}"
        _finish(database, artwork_id, status="failed", error=message, duration=duration)
        raise GenerationError(message) from exc
    if measured != (plan.settings.width, plan.settings.height):
        message = (
            f"PNG is {measured[0]}x{measured[1]}, "
            f"expected {plan.settings.width}x{plan.settings.height}."
        )
        _finish(database, artwork_id, status="failed", error=message, duration=duration)
        raise GenerationError(message)
    _finish(database, artwork_id, status="succeeded", error=None, duration=duration)
    _write_sidecar(output, plan, chosen_seed)
    return _record(database, artwork_id)


def check_ready(settings: GeneratorSettings, seed: int) -> None:
    _validate_settings(settings)
    _validate_seed(seed)
    _require_installed_files(settings)


def build_command(settings: GeneratorSettings, prompt: str, seed: int, output: Path) -> list[str]:
    """Production command. It stays on the reviewed Z-Image Turbo profile."""
    profile = production_model(settings)
    return build_model_command(
        settings.executable,
        profile,
        prompt=prompt,
        negative_prompt="",
        seed=seed,
        output=output,
        steps=settings.steps,
        sampler=settings.sampling_method,
        scheduler=None,
        cfg_scale=settings.cfg_scale,
        width=settings.width,
        height=settings.height,
        clip_skip=None,
    )


def build_model_command(
    executable: Path,
    profile: ModelProfile,
    *,
    prompt: str,
    negative_prompt: str,
    seed: int,
    output: Path,
    steps: int,
    sampler: str,
    scheduler: str | None,
    cfg_scale: float,
    width: int,
    height: int,
    clip_skip: int | None,
) -> list[str]:
    if profile.command_type == "checkpoint":
        command = [str(executable), "--model", str(profile.model_path), "--backend", profile.backend]
    elif profile.command_type == "components":
        if profile.text_encoder is None or profile.vae is None:
            raise GenerationError(f"{profile.name} is missing its text encoder or VAE.")
        command = [
            str(executable),
            "--diffusion-model",
            str(profile.model_path),
            "--llm",
            str(profile.text_encoder),
            "--vae",
            str(profile.vae),
            "--backend",
            profile.backend,
        ]
    else:
        raise GenerationError(f"Unknown command type for {profile.name}.")
    if profile.flash_attn:
        command.extend(["--flash-attn", profile.flash_attn])
    command.extend(["--steps", str(steps), "--sampling-method", sampler])
    if scheduler:
        command.extend(["--scheduler", scheduler])
    command.extend(
        [
            "--cfg-scale",
            _format_cfg(cfg_scale),
        ]
    )
    if clip_skip is not None:
        command.extend(["--clip-skip", str(clip_skip)])
    command.extend(
        [
            "--width",
            str(width),
            "--height",
            str(height),
            "--seed",
            str(seed),
            "--prompt",
            prompt,
        ]
    )
    if negative_prompt:
        command.extend(["--negative-prompt", negative_prompt])
    if profile.vae_tiling or max(width, height) > 1024:
        if "--vae-tiling" not in command:
            command.append("--vae-tiling")
    command.extend(["--output", str(output)])
    if profile.offload_to_cpu:
        anchor = "--flash-attn" if "--flash-attn" in command else "--steps"
        command.insert(command.index(anchor), "--offload-to-cpu")
    return command


def _production_model_record(settings: GeneratorSettings) -> dict[str, object]:
    profile = production_model(settings)
    return _profile_fields(profile)


def _profile_record(plan: GenerationPlan) -> dict[str, object]:
    return _profile_fields(plan.profile)


def _profile_fields(profile: ModelProfile) -> dict[str, object]:
    return {
        "model_id": profile.id,
        "model_name": profile.name,
        "model_path": str(profile.model_path),
        "command_type": profile.command_type,
    }


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _default_interpreter():
    from anskar.interpret import OllamaInterpreter

    return OllamaInterpreter()


def _interprets(choice: StyleChoice) -> bool:
    return choice.enabled and choice.composition != "text"


def _image_prompt(
    body: str,
    choice: StyleChoice,
    interpreter,
    abstraction: str,
) -> tuple[str, str, str | None, dict[str, object] | None]:
    if not _interprets(choice):
        return apply_style(body, choice), "direct", None, None
    from anskar.interpret import InterpreterError

    active = interpreter if interpreter is not None else _default_interpreter()
    try:
        result = active.interpret(body, choice, abstraction)
    except InterpreterError as exc:
        raise GenerationError(str(exc)) from exc
    try:
        active.release_for_image_generation()
    except InterpreterError as exc:
        raise GenerationError(str(exc)) from exc
    prompt = result.prompt
    if body in prompt:
        raise GenerationError("The visual prompt repeated the poem. The image was not generated.")
    record = result.to_record()
    ollama = record.get("ollama")
    if not isinstance(ollama, dict):
        ollama = None
    return prompt, "interpreted", abstraction, ollama


def _settings_for_request(
    settings: GeneratorSettings,
    model_id: str,
    width: int | None,
    height: int | None,
) -> tuple[GeneratorSettings, ModelProfile]:
    profile = require_model(model_id, settings)
    if profile.id == PRODUCTION_MODEL_ID:
        return (
            replace(
                settings,
                width=settings.width if width is None else width,
                height=settings.height if height is None else height,
            ),
            profile,
        )
    return (
        replace(
            settings,
            width=profile.width if width is None else width,
            height=profile.height if height is None else height,
            steps=profile.steps,
            sampling_method=profile.sampler,
            cfg_scale=profile.cfg_scale,
        ),
        profile,
    )


def _negative_prompt(profile: ModelProfile) -> str:
    if profile.negative_enabled and profile.negative_prompt.strip():
        return profile.negative_prompt
    return ""


def _image_command(plan: GenerationPlan, seed: int, output: Path) -> list[str]:
    return build_model_command(
        plan.settings.executable,
        plan.profile,
        prompt=plan.prompt,
        negative_prompt=_negative_prompt(plan.profile),
        seed=seed,
        output=output,
        steps=plan.settings.steps,
        sampler=plan.settings.sampling_method,
        scheduler=plan.profile.scheduler,
        cfg_scale=plan.settings.cfg_scale,
        width=plan.settings.width,
        height=plan.settings.height,
        clip_skip=plan.profile.clip_skip,
    )


def _write_sidecar(output: Path, plan: GenerationPlan, seed: int) -> None:
    path = output.with_suffix(".json")
    if path.exists():
        raise GenerationError(f"Refusing to overwrite artwork metadata: {path}")
    payload = {
        "experiment": "production",
        "title": plan.selection.poem.title,
        "author": plan.selection.poem.author,
        "body": plan.source_text,
        "source_text": plan.source_text,
        "source_sha256": prompt_hash(plan.source_text),
        "prompt": plan.prompt,
        "prompt_sha256": plan.prompt_sha256,
        "visual_prompt": plan.prompt if plan.method == "interpreted" else "",
        "interpretation": plan.prompt if plan.method == "interpreted" else "",
        "abstraction": plan.abstraction,
        "method": plan.method,
        "house_style": plan.style.enabled,
        "style_preset": plan.style.preset,
        "style_prompt": plan.style.prompt,
        "style_composition": plan.style.composition,
        "model_id": plan.profile.id,
        "model_name": plan.profile.name,
        "model_path": str(plan.profile.model_path),
        "command_type": plan.profile.command_type,
        "seed": seed,
        "width": plan.settings.width,
        "height": plan.settings.height,
        "steps": plan.settings.steps,
        "cfg_scale": plan.settings.cfg_scale,
        "sampler": plan.settings.sampling_method,
        "scheduler": plan.profile.scheduler,
        "clip_skip": plan.profile.clip_skip,
        "negative_prompt": _negative_prompt(plan.profile),
        "ollama": plan.ollama,
        "print_suitable": plan.print_suitable,
        "intended_print": intended_print(plan.settings.width, plan.settings.height),
        "output_path": str(output),
        "poem_id": plan.selection.poem.id,
        "selection_id": plan.selection.id,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def artwork_path(directory: Path, selection: Selection, seed: int) -> Path:
    name = f"poem-{selection.poem.id}-selection-{selection.id}-seed-{seed}.png"
    return directory / name


def format_plan(plan: GenerationPlan) -> str:
    poem = plan.selection.poem
    if plan.style.enabled:
        style_lines = (
            f"House style: {plan.style.preset}\n"
            f"Style prompt: {plan.style.prompt}\n"
            f"Composition: {plan.style.composition}\n"
        )
    else:
        style_lines = "House style: off\n"
    if plan.method == "interpreted":
        method_lines = f"Method: interpreted\nAbstraction: {plan.abstraction}\nInterpreter: qwen3:8b\n"
    else:
        method_lines = "Method: direct\n"
    export = "print-suitable" if plan.print_suitable else "experimental export"
    return (
        f"Pending poem {poem.id}: {poem.title} by {poem.author}\n"
        f"Model: {plan.profile.name}\n"
        f"{style_lines}"
        f"{method_lines}"
        f"Prompt SHA-256: {plan.prompt_sha256}\n"
        f"Resolution: {plan.settings.width}x{plan.settings.height}\n"
        f"Export: {export}\n"
        f"Backend: {plan.settings.backend}\n"
        f"Steps: {plan.settings.steps}\n"
        f"Sampler: {plan.settings.sampling_method}\n"
        f"CFG: {_format_cfg(plan.settings.cfg_scale)}\n"
        f"Output directory: {plan.settings.artworks_dir}\n"
        "Seed: chosen when generation starts\n"
        "The stored poem is unchanged. The poem stays pending.\n\n"
        "Source poem:\n"
        f"{plan.source_text}\n\n"
        "Prompt:\n"
        f"{plan.prompt}"
    )


def format_artwork(record: ArtworkRecord) -> str:
    duration = "unknown" if record.duration_seconds is None else f"{record.duration_seconds:.1f}s"
    try:
        saved = json.loads(record.settings_json)
    except json.JSONDecodeError:
        saved = {}
    if saved.get("house_style"):
        style_line = f"House style: {saved.get('style_preset')}\n"
    elif "house_style" in saved:
        style_line = "House style: off\n"
    else:
        style_line = ""
    if saved.get("method") == "interpreted":
        method_line = f"Method: interpreted\nAbstraction: {saved.get('abstraction')}\n"
    elif saved.get("method"):
        method_line = "Method: direct\n"
    else:
        method_line = ""
    if "print_suitable" in saved:
        export_line = "Export: print-suitable\n" if saved["print_suitable"] else "Export: experimental export\n"
    else:
        export_line = ""
    return (
        f"Generated: {record.output_path}\n"
        f"Poem: {record.title} by {record.author} (id {record.poem_id})\n"
        f"{style_line}"
        f"{method_line}"
        f"{export_line}"
        f"Seed: {record.seed}\n"
        f"Resolution: {record.width}x{record.height}\n"
        f"Duration: {duration}\n"
        f"Prompt SHA-256: {record.prompt_sha256}\n"
        "The poem stays pending."
    )


def _pending(connection: sqlite3.Connection) -> Selection:
    selection = get_pending(connection)
    if selection is None:
        raise NoPendingSelectionError("There is no pending poem to generate.")
    return selection


def _reject_existing(connection: sqlite3.Connection, poem_id: int) -> None:
    succeeded = connection.execute(
        """
        SELECT output_path FROM artworks
        WHERE poem_id = ? AND status = 'succeeded'
        """,
        (poem_id,),
    ).fetchone()
    if succeeded is not None:
        raise DuplicateArtworkError(
            f"An artwork for this poem already exists: {succeeded['output_path']}. "
            "The pending poem was not changed."
        )
    running = connection.execute(
        """
        SELECT id FROM artworks
        WHERE poem_id = ? AND status = 'running'
        """,
        (poem_id,),
    ).fetchone()
    if running is not None:
        raise DuplicateArtworkError("A generation for this poem is already running.")


def _clear_stale_running(connection: sqlite3.Connection, poem_id: int, timeout_seconds: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
    connection.execute(
        """
        UPDATE artworks
        SET status = 'failed',
            error = 'The previous generation did not finish before the timeout.',
            finished_at = ?
        WHERE poem_id = ? AND status = 'running' AND created_at < ?
        """,
        (utc_now(), poem_id, cutoff),
    )


def _insert_running(
    connection: sqlite3.Connection,
    plan: GenerationPlan,
    *,
    seed: int,
    output: Path,
    settings_json: str,
) -> int:
    try:
        cursor = connection.execute(
            """
            INSERT INTO artworks (
                poem_id, selection_id, title, author, prompt_sha256, model, seed,
                width, height, settings_json, output_path, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                plan.selection.poem.id,
                plan.selection.id,
                plan.selection.poem.title,
                plan.selection.poem.author,
                plan.prompt_sha256,
                str(plan.profile.model_path),
                seed,
                plan.settings.width,
                plan.settings.height,
                settings_json,
                str(output),
                utc_now(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise DuplicateArtworkError("A generation for this poem is already recorded.") from exc
    return int(cursor.lastrowid)


def _finish(
    database: Path,
    artwork_id: int,
    *,
    status: str,
    error: str | None,
    duration: float,
) -> None:
    with connect(database) as connection:
        connection.execute(
            """
            UPDATE artworks
            SET status = ?, error = ?, duration_seconds = ?, finished_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (status, error, duration, utc_now(), artwork_id),
        )


def _record(database: Path, artwork_id: int) -> ArtworkRecord:
    with connect(database) as connection:
        row = connection.execute("SELECT * FROM artworks WHERE id = ?", (artwork_id,)).fetchone()
    if row is None:
        raise GenerationError(f"Artwork record {artwork_id} disappeared.")
    return ArtworkRecord(
        id=row["id"],
        poem_id=row["poem_id"],
        selection_id=row["selection_id"],
        title=row["title"],
        author=row["author"],
        prompt_sha256=row["prompt_sha256"],
        model=row["model"],
        seed=row["seed"],
        width=row["width"],
        height=row["height"],
        duration_seconds=row["duration_seconds"],
        settings_json=row["settings_json"],
        output_path=row["output_path"],
        status=row["status"],
        error=row["error"],
    )


def _require_installed_files(settings: GeneratorSettings) -> None:
    files = {
        "generator executable": settings.executable,
        "diffusion model": settings.diffusion_model,
        "text encoder": settings.text_encoder,
        "VAE": settings.vae,
    }
    missing = [f"{label}: {path}" for label, path in files.items() if not path.is_file()]
    if missing:
        raise GenerationError("Generator files are missing. " + "; ".join(missing))
    if not os.access(settings.executable, os.X_OK):
        raise GenerationError(f"Generator executable is not runnable: {settings.executable}")


def _validate_settings(settings: GeneratorSettings) -> None:
    if settings.steps < 1:
        raise GenerationError("Steps must be at least 1.")
    if (
        settings.width % 8
        or settings.height % 8
        or not 64 <= settings.width <= PRINT_LONG_EDGE
        or not 64 <= settings.height <= PRINT_LONG_EDGE
    ):
        raise GenerationError(
            f"Width and height must be multiples of 8 from 64 through {PRINT_LONG_EDGE}."
        )
    if settings.cfg_scale < 0:
        raise GenerationError("CFG scale cannot be negative.")
    if settings.timeout_seconds < 1:
        raise GenerationError("Timeout must be at least 1 second.")
    if not settings.backend or not settings.sampling_method or not settings.flash_attn:
        raise GenerationError("Backend, sampler, and flash-attention settings are required.")


def _validate_seed(seed: int) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < SEED_LIMIT:
        raise GenerationError(f"Seed must be an integer from 0 through {SEED_LIMIT - 1}.")


def _format_cfg(value: float) -> str:
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    if "." not in text:
        return f"{text}.0"
    return text


def _output_text(payload: bytes | str | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", errors="replace")
    else:
        text = payload
    text = text.strip()
    if len(text) > 4000:
        return text[-4000:]
    return text
