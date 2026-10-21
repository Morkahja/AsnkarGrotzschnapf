"""Installed image models and the reviewed production choice.

Production stays on Z-Image Turbo. The prompt lab can select another installed
model. Each model keeps its own command shape and its own editable defaults.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from anskar.artwork.settings import GeneratorSettings, load_settings

PRODUCTION_MODEL_ID = "z-image-turbo"

DEFAULT_NEGATIVE_PROMPT = (
    "text, letters, words, captions, handwriting, typography, logos, signage, "
    "watermarks, writing, alphabet, numbers, signature, font, title, subtitle, "
    "inscription, label, glyphs, calligraphy, printed text, written text"
)

# Used where CFG actually applies a negative branch. Z-Image Turbo stays off.
GUIDED_NEGATIVE_PROMPT = (
    "photorealistic, photograph, stock photography, documentary, realistic rendering, "
    "commercial product photography, text, typography, letters, words, watermark, logo"
)

DREAMSHAPER_NEGATIVE_PROMPT = (
    "text, letters, words, writing, typography, quotes, captions, subtitles, "
    "calligraphy, signage, logos, watermarks, signatures, photorealism, stock photography"
)

SAMPLERS = (
    "euler",
    "euler_a",
    "heun",
    "dpm2",
    "dpm++2s_a",
    "dpm++2m",
    "dpm++2mv2",
    "dpm++2m_sde",
    "dpm++2m_sde_bt",
    "lms",
)

SCHEDULERS = (
    "discrete",
    "karras",
    "exponential",
    "ays",
    "gits",
    "sgm_uniform",
    "simple",
    "smoothstep",
    "kl_optimal",
    "lcm",
    "beta",
)

REALVISXL_PATH = Path("/home/babuni/local-ai/models/realvisxl-v5/RealVisXL_V5.0_fp16.safetensors")
ZIMAGE_PATH = Path("/home/babuni/local-ai/models/z-image/z-image-Q4_K_M.gguf")
DREAMSHAPER_PATH = Path(
    "/home/babuni/local-ai/models/dreamshaper-xl/"
    "DreamShaperXL1.0Alpha2_fixedVae_half_00001_.safetensors"
)
# Integrity-checked at install. Recorded in experiment metadata; not rehashed on every run.
DREAMSHAPER_SHA256 = "0f1b80cfe81b9c3bde7fdcbf6898897b2811b27be1df684583c3d85cbc9b1fa4"

COMMERCIAL_ALLOWED = "allowed"
COMMERCIAL_UNVERIFIED = "unverified"


@dataclass(frozen=True)
class ModelProfile:
    id: str
    name: str
    command_type: str
    model_path: Path
    text_encoder: Path | None
    vae: Path | None
    width: int
    height: int
    steps: int
    cfg_scale: float
    sampler: str
    scheduler: str | None
    clip_skip: int | None
    negative_enabled: bool
    negative_prompt: str
    flash_attn: str | None
    offload_to_cpu: bool
    backend: str
    vae_tiling: bool = False
    commercial_use: str = COMMERCIAL_ALLOWED
    model_checksum: str | None = None

    def to_public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "command_type": self.command_type,
            "model_path": str(self.model_path),
            "width": self.width,
            "height": self.height,
            "steps": self.steps,
            "cfg_scale": self.cfg_scale,
            "sampler": self.sampler,
            "scheduler": self.scheduler or "",
            "clip_skip": self.clip_skip,
            "negative_enabled": self.negative_enabled,
            "negative_prompt": self.negative_prompt,
            "vae_tiling": self.vae_tiling,
            "offload_to_cpu": self.offload_to_cpu,
            "commercial_use": self.commercial_use,
            "model_checksum": self.model_checksum,
        }


def production_model(settings: GeneratorSettings | None = None) -> ModelProfile:
    """The reviewed production model. This remains Z-Image Turbo."""
    return _turbo(settings or load_settings())


def installed_models(settings: GeneratorSettings | None = None) -> list[ModelProfile]:
    active = settings or load_settings()
    models = [_turbo(active)]
    zimage = _zimage(active)
    encoder = zimage.text_encoder
    vae = zimage.vae
    if zimage.model_path.is_file() and encoder is not None and encoder.is_file() and vae is not None and vae.is_file():
        models.append(zimage)
    realvis = _realvis(active)
    if realvis.model_path.is_file():
        models.append(realvis)
    dreamshaper = _dreamshaper(active)
    if dreamshaper.model_path.is_file():
        models.append(dreamshaper)
    return models


def require_model(model_id: str, settings: GeneratorSettings | None = None) -> ModelProfile:
    for profile in installed_models(settings):
        if profile.id == model_id:
            return profile
    raise ValueError(f"Model is not installed: {model_id}")


def model_allows_publication(model_id: str | None, settings: GeneratorSettings | None = None) -> bool:
    """False when missing, unknown, not installed, or commercially unverified."""
    if not model_id:
        return False
    try:
        profile = require_model(model_id, settings)
    except ValueError:
        return False
    return profile.commercial_use != COMMERCIAL_UNVERIFIED


def validate_sampler(sampler: str) -> str:
    if sampler not in SAMPLERS:
        raise ValueError(f"Sampler must be one of: {', '.join(SAMPLERS)}.")
    return sampler


def validate_scheduler(scheduler: str | None) -> str | None:
    if scheduler is None or scheduler == "":
        return None
    if scheduler not in SCHEDULERS:
        raise ValueError(f"Scheduler must be one of: {', '.join(SCHEDULERS)}.")
    return scheduler


def _turbo(settings: GeneratorSettings) -> ModelProfile:
    return ModelProfile(
        id="z-image-turbo",
        name="Z-Image Turbo",
        command_type="components",
        model_path=settings.diffusion_model,
        text_encoder=settings.text_encoder,
        vae=settings.vae,
        width=settings.width,
        height=settings.height,
        steps=settings.steps,
        cfg_scale=settings.cfg_scale,
        sampler=settings.sampling_method,
        scheduler=None,
        clip_skip=None,
        negative_enabled=False,
        negative_prompt="",
        flash_attn=settings.flash_attn,
        offload_to_cpu=settings.offload_to_cpu,
        backend=settings.backend,
    )


def _realvis(settings: GeneratorSettings) -> ModelProfile:
    return ModelProfile(
        id="realvisxl-v5",
        name="RealVisXL V5",
        command_type="checkpoint",
        model_path=_path("ANSKAR_REALVISXL", REALVISXL_PATH),
        text_encoder=None,
        vae=None,
        width=1024,
        height=1024,
        steps=30,
        cfg_scale=5.0,
        sampler="dpm++2m",
        scheduler="karras",
        clip_skip=2,
        negative_enabled=True,
        negative_prompt=GUIDED_NEGATIVE_PROMPT,
        flash_attn=None,
        offload_to_cpu=settings.offload_to_cpu,
        backend=settings.backend,
    )


def _zimage(settings: GeneratorSettings) -> ModelProfile:
    return ModelProfile(
        id="z-image",
        name="Z-Image",
        command_type="components",
        model_path=_path("ANSKAR_ZIMAGE_MODEL", ZIMAGE_PATH),
        text_encoder=settings.text_encoder,
        vae=settings.vae,
        width=512,
        height=512,
        steps=35,
        cfg_scale=4.0,
        sampler="euler",
        scheduler=None,
        clip_skip=None,
        negative_enabled=True,
        negative_prompt=GUIDED_NEGATIVE_PROMPT,
        flash_attn=settings.flash_attn,
        offload_to_cpu=settings.offload_to_cpu,
        backend=settings.backend,
    )


def _dreamshaper(settings: GeneratorSettings) -> ModelProfile:
    return ModelProfile(
        id="dreamshaper-xl",
        name="DreamShaper XL",
        command_type="checkpoint",
        model_path=_path("ANSKAR_DREAMSHAPER_XL", DREAMSHAPER_PATH),
        text_encoder=None,
        vae=None,
        width=1024,
        height=1024,
        steps=30,
        cfg_scale=5.5,
        sampler="euler_a",
        scheduler=None,
        clip_skip=None,
        negative_enabled=True,
        negative_prompt=DREAMSHAPER_NEGATIVE_PROMPT,
        flash_attn=None,
        offload_to_cpu=True,
        backend=settings.backend,
        vae_tiling=True,
        commercial_use=COMMERCIAL_UNVERIFIED,
        model_checksum=DREAMSHAPER_SHA256,
    )


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return Path(value).expanduser()
