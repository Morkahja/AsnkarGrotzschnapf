"""Save prompt-lab images and the metadata beside them."""

import json
import os
import secrets
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from anskar.artwork.generator import (
    SEED_LIMIT,
    GenerationError,
    SubprocessRunner,
    _validate_seed,
    build_model_command,
    prompt_hash,
)
from anskar.artwork.png import PRINT_LONG_EDGE, intended_print, png_size
from anskar.artwork.settings import GeneratorSettings, load_settings
from anskar.config import PROJECT_ROOT
from anskar.lab.prompts import MODES, compose_prompt, experiment_slug
from anskar.models import require_model, validate_sampler, validate_scheduler
from anskar.style import apply_style, lab_style

WIDTH_RANGE = (128, PRINT_LONG_EDGE)
HEIGHT_RANGE = (128, PRINT_LONG_EDGE)
STEPS_RANGE = (1, 50)
CFG_RANGE = (0.0, 20.0)
CLIP_SKIP_RANGE = (1, 4)


@dataclass(frozen=True)
class LabRequest:
    title: str
    body: str
    mode: str
    experiment: str
    seed: int | None
    width: int
    height: int
    steps: int
    cfg_scale: float
    house_style: bool = False
    style_preset: str = "none"
    style_prompt: str = ""
    style_composition: str = "text"
    model_id: str = "z-image-turbo"
    sampler: str = ""
    scheduler: str | None = None
    clip_skip: int | None = None
    clip_skip_set: bool = False
    negative_enabled: bool = False
    negative_prompt: str = ""
    method: str = "direct"
    visual_prompt: str = ""
    interpretation: str = ""
    source_text: str = ""
    ollama: dict | None = None
    abstraction: str = "surreal"


@dataclass(frozen=True)
class LabResult:
    title: str
    body: str
    prompt: str
    mode: str
    seed: int
    steps: int
    cfg_scale: float
    width: int
    height: int
    prompt_sha256: str
    duration_seconds: float
    output_path: str
    metadata_path: str
    image_url: str
    experiment: str
    model_paths: dict[str, str]
    house_style: bool
    style_preset: str
    style_prompt: str
    style_composition: str
    model_id: str
    model_name: str
    model_path: str
    command_type: str
    negative_prompt: str
    sampler: str
    scheduler: str | None
    clip_skip: int | None
    method: str
    source_text: str
    source_sha256: str
    visual_prompt: str
    interpretation: str
    interpretation_sha256: str
    interpretation_edited: bool
    ollama: dict | None
    abstraction: str


def experiments_dir() -> Path:
    return PROJECT_ROOT / "data" / "experiments"


def run_experiment(
    request: LabRequest,
    *,
    settings: GeneratorSettings | None = None,
    runner: SubprocessRunner | None = None,
    directory: Path | None = None,
    now: datetime | None = None,
) -> LabResult:
    _validate_request(request)
    base = load_settings() if settings is None else settings
    profile = require_model(request.model_id, base)
    sampler = validate_sampler(request.sampler or profile.sampler)
    if request.scheduler is None:
        scheduler = validate_scheduler(profile.scheduler)
    else:
        scheduler = validate_scheduler(request.scheduler)
    clip_skip = profile.clip_skip if not request.clip_skip_set else request.clip_skip
    if clip_skip is not None and not CLIP_SKIP_RANGE[0] <= clip_skip <= CLIP_SKIP_RANGE[1]:
        raise ValueError(
            f"Clip skip must be from {CLIP_SKIP_RANGE[0]} through {CLIP_SKIP_RANGE[1]}."
        )
    sent_negative = request.negative_prompt if request.negative_enabled else ""
    choice = lab_style(
        enabled=request.house_style,
        preset=request.style_preset,
        prompt=request.style_prompt,
        composition=request.style_composition,
    )
    current = compose_prompt(request.mode, request.title, request.body)
    source = request.source_text if request.method == "interpreted" and request.source_text.strip() else current
    prompt = _image_prompt(request, current, choice)
    chosen_seed = secrets.randbelow(SEED_LIMIT) if request.seed is None else request.seed
    _check_model_ready(base, profile, chosen_seed)
    root = directory if directory is not None else experiments_dir()
    folder = root / experiment_slug(request.experiment)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    image_path, metadata_path = _allocate(folder, request.mode, chosen_seed, stamp, request.method)
    started = _monotonic()
    try:
        completed = (runner or SubprocessRunner()).run(
            build_model_command(
                base.executable,
                profile,
                prompt=prompt,
                negative_prompt=sent_negative,
                seed=chosen_seed,
                output=image_path,
                steps=request.steps,
                sampler=sampler,
                scheduler=scheduler,
                cfg_scale=request.cfg_scale,
                width=request.width,
                height=request.height,
                clip_skip=clip_skip,
            ),
            timeout=base.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise GenerationError(
            f"Generation timed out after {base.timeout_seconds} seconds."
        ) from exc
    duration = _monotonic() - started
    if completed.returncode != 0:
        detail = _stderr(completed.stderr)
        raise GenerationError(f"Generator exited with status {completed.returncode}. {detail}".strip())
    try:
        measured = png_size(image_path)
    except (OSError, ValueError) as exc:
        raise GenerationError(f"Generator finished without a readable PNG: {exc}") from exc
    if measured != (request.width, request.height):
        raise GenerationError(
            f"PNG is {measured[0]}x{measured[1]}, expected {request.width}x{request.height}."
        )
    digest = prompt_hash(prompt)
    models = {
        "executable": str(base.executable),
        "model_path": str(profile.model_path),
        "command_type": profile.command_type,
    }
    if profile.text_encoder is not None:
        models["text_encoder"] = str(profile.text_encoder)
    if profile.vae is not None:
        models["vae"] = str(profile.vae)
    record = {
        "title": request.title,
        "body": request.body,
        "prompt": prompt,
        "method": request.method,
        "source_text": source,
        "source_sha256": prompt_hash(source),
        "visual_prompt": request.visual_prompt if request.method == "interpreted" else "",
        "visual_prompt_sha256": prompt_hash(request.visual_prompt) if request.method == "interpreted" else "",
        "interpretation": request.interpretation if request.method == "interpreted" else "",
        "interpretation_sha256": prompt_hash(request.interpretation) if request.method == "interpreted" else "",
        "interpretation_edited": request.method == "interpreted" and request.visual_prompt != request.interpretation,
        "ollama": request.ollama if request.method == "interpreted" else None,
        "abstraction": request.abstraction,
        "negative_prompt": sent_negative,
        "negative_enabled": request.negative_enabled,
        "mode": request.mode,
        "seed": chosen_seed,
        "steps": request.steps,
        "cfg_scale": request.cfg_scale,
        "width": request.width,
        "height": request.height,
        "resolution": f"{request.width}x{request.height}",
        "intended_print": intended_print(request.width, request.height),
        "prompt_sha256": digest,
        "duration_seconds": duration,
        "output_path": str(image_path),
        "model_paths": models,
        "model_id": profile.id,
        "model_name": profile.name,
        "model_path": str(profile.model_path),
        "model_checksum": profile.model_checksum,
        "command_type": profile.command_type,
        "commercial_use": profile.commercial_use,
        "experiment": experiment_slug(request.experiment),
        "sampler": sampler,
        "sampling_method": sampler,
        "scheduler": scheduler,
        "clip_skip": clip_skip,
        "backend": profile.backend,
        "offload_to_cpu": profile.offload_to_cpu,
        "vae_tiling": profile.vae_tiling or max(request.width, request.height) > 1024,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "house_style": choice.enabled,
        "style_preset": choice.preset,
        "style_prompt": choice.prompt,
        "style_composition": choice.composition,
    }
    with metadata_path.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    relative = image_path.relative_to(root).as_posix()
    return LabResult(
        title=request.title,
        body=request.body,
        prompt=prompt,
        mode=request.mode,
        seed=chosen_seed,
        steps=request.steps,
        cfg_scale=request.cfg_scale,
        width=request.width,
        height=request.height,
        prompt_sha256=digest,
        duration_seconds=duration,
        output_path=str(image_path),
        metadata_path=str(metadata_path),
        image_url=f"/media/{relative}",
        experiment=record["experiment"],
        model_paths=models,
        house_style=choice.enabled,
        style_preset=choice.preset,
        style_prompt=choice.prompt,
        style_composition=choice.composition,
        model_id=profile.id,
        model_name=profile.name,
        model_path=str(profile.model_path),
        command_type=profile.command_type,
        negative_prompt=sent_negative,
        sampler=sampler,
        scheduler=scheduler,
        clip_skip=clip_skip,
        method=request.method,
        source_text=source,
        source_sha256=prompt_hash(source),
        visual_prompt=record["visual_prompt"],
        interpretation=record["interpretation"],
        interpretation_sha256=record["interpretation_sha256"],
        interpretation_edited=record["interpretation_edited"],
        ollama=record["ollama"],
        abstraction=request.abstraction,
    )


def list_experiments(directory: Path | None = None, *, limit: int = 60) -> list[dict]:
    root = directory if directory is not None else experiments_dir()
    if not root.is_dir():
        return []
    records = []
    for metadata_path in root.glob("*/*.json"):
        try:
            record = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        image = metadata_path.with_suffix(".png")
        if not image.is_file():
            continue
        record["image_url"] = f"/media/{image.relative_to(root).as_posix()}"
        record["metadata_file"] = metadata_path.relative_to(root).as_posix()
        records.append(record)
    records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return records[:limit]


def resolve_media(directory: Path, relative: str) -> Path:
    if relative == "" or relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        raise ValueError("Image path is not available.")
    root = directory.resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ValueError("Image path is not available.")
    if path.suffix.lower() != ".png" or not path.is_file():
        raise ValueError("Image path is not available.")
    return path


def _image_prompt(request: LabRequest, source: str, choice) -> str:
    if request.method == "direct":
        return apply_style(source, choice)
    if request.method == "interpreted":
        return request.visual_prompt
    raise ValueError("Method must be direct or interpreted.")


def _allocate(folder: Path, mode: str, seed: int, stamp: str, method: str) -> tuple[Path, Path]:
    label = "title-body" if mode == "title_body" else mode
    if method == "interpreted":
        label = f"interpreted-{label}"
    for suffix in ["", *[f"-{number}" for number in range(2, 100)]]:
        stem = f"{stamp}-{label}-seed-{seed}{suffix}"
        image_path = folder / f"{stem}.png"
        metadata_path = folder / f"{stem}.json"
        if not image_path.exists() and not metadata_path.exists():
            return image_path, metadata_path
    raise GenerationError("Could not find a free filename. Nothing was overwritten.")


def _validate_request(request: LabRequest) -> None:
    if request.mode not in MODES:
        raise ValueError("Mode must be title, body, or title_body.")
    if request.method not in ("direct", "interpreted"):
        raise ValueError("Method must be direct or interpreted.")
    if request.method == "interpreted" and request.visual_prompt.strip() == "":
        raise ValueError("The visual prompt is empty.")
    if len(request.visual_prompt) > 20_000 or len(request.interpretation) > 20_000:
        raise ValueError("The visual prompt is too long.")
    if len(request.source_text) > 100_000:
        raise ValueError("The source text is too long.")
    if request.ollama is not None and not isinstance(request.ollama, dict):
        raise ValueError("Ollama settings must be an object.")
    if request.abstraction not in ("gentle", "surreal", "experimental"):
        raise ValueError("Abstraction must be gentle, surreal, or experimental.")
    if not isinstance(request.width, int) or isinstance(request.width, bool):
        raise ValueError("Width must be an integer.")
    if not isinstance(request.height, int) or isinstance(request.height, bool):
        raise ValueError("Height must be an integer.")
    if not isinstance(request.steps, int) or isinstance(request.steps, bool):
        raise ValueError("Steps must be an integer.")
    if isinstance(request.cfg_scale, bool) or not isinstance(request.cfg_scale, (int, float)):
        raise ValueError("CFG scale must be a number.")
    if request.seed is not None and (
        isinstance(request.seed, bool) or not isinstance(request.seed, int)
    ):
        raise ValueError("Seed must be an integer.")
    if not WIDTH_RANGE[0] <= request.width <= WIDTH_RANGE[1]:
        raise ValueError(f"Width must be from {WIDTH_RANGE[0]} through {WIDTH_RANGE[1]}.")
    if not HEIGHT_RANGE[0] <= request.height <= HEIGHT_RANGE[1]:
        raise ValueError(f"Height must be from {HEIGHT_RANGE[0]} through {HEIGHT_RANGE[1]}.")
    if not STEPS_RANGE[0] <= request.steps <= STEPS_RANGE[1]:
        raise ValueError(f"Steps must be from {STEPS_RANGE[0]} through {STEPS_RANGE[1]}.")
    if not CFG_RANGE[0] <= float(request.cfg_scale) <= CFG_RANGE[1]:
        raise ValueError(f"CFG scale must be from {CFG_RANGE[0]} through {CFG_RANGE[1]}.")
    if len(request.title) > 10_000 or len(request.body) > 100_000:
        raise ValueError("The text is too long for the prompt lab.")
    if len(request.negative_prompt) > 20_000:
        raise ValueError("The negative prompt is too long.")
    if request.clip_skip_set and request.clip_skip is not None and (
        isinstance(request.clip_skip, bool) or not isinstance(request.clip_skip, int)
    ):
        raise ValueError("Clip skip must be an integer.")


def _check_model_ready(settings: GeneratorSettings, profile, seed: int) -> None:
    _validate_seed(seed)
    files = {"generator executable": settings.executable, profile.name: profile.model_path}
    if profile.command_type == "components":
        if profile.text_encoder is None or profile.vae is None:
            raise GenerationError(f"{profile.name} is missing its text encoder or VAE.")
        files["text encoder"] = profile.text_encoder
        files["VAE"] = profile.vae
    missing = [f"{label}: {path}" for label, path in files.items() if not path.is_file()]
    if missing:
        raise GenerationError("Generator files are missing. " + "; ".join(missing))
    if not os.access(settings.executable, os.X_OK):
        raise GenerationError(f"Generator executable is not runnable: {settings.executable}")


def _monotonic() -> float:
    import time

    return time.monotonic()


def _stderr(payload: bytes | None) -> str:
    if not payload:
        return ""
    text = payload.decode("utf-8", errors="replace").strip()
    return text[-2000:]
