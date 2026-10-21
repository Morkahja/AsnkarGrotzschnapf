"""Local generator settings.

Defaults match the installed Z-Image Turbo setup. Environment variables override
them without changing the installed models or runtime.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from anskar.artwork.png import DEFAULT_PRINT_INCHES, pixels_for_print
from anskar.config import PROJECT_ROOT

INSTALLED_EXECUTABLE = Path("/home/babuni/local-ai/stable-diffusion.cpp/build-vulkan/bin/sd-cli")
INSTALLED_MODEL_DIR = Path("/home/babuni/local-ai/models/z-image-turbo")
DEFAULT_WIDTH, DEFAULT_HEIGHT = pixels_for_print(DEFAULT_PRINT_INCHES, DEFAULT_PRINT_INCHES)


@dataclass(frozen=True)
class GeneratorSettings:
    executable: Path
    diffusion_model: Path
    text_encoder: Path
    vae: Path
    backend: str
    steps: int
    sampling_method: str
    cfg_scale: float
    width: int
    height: int
    offload_to_cpu: bool
    flash_attn: str
    timeout_seconds: int
    artworks_dir: Path

    def to_record(self, *, seed: int, prompt_sha256: str) -> dict[str, object]:
        return {
            "executable": str(self.executable),
            "diffusion_model": str(self.diffusion_model),
            "text_encoder": str(self.text_encoder),
            "vae": str(self.vae),
            "backend": self.backend,
            "steps": self.steps,
            "sampling_method": self.sampling_method,
            "cfg_scale": self.cfg_scale,
            "width": self.width,
            "height": self.height,
            "offload_to_cpu": self.offload_to_cpu,
            "flash_attn": self.flash_attn,
            "seed": seed,
            "timeout_seconds": self.timeout_seconds,
            "prompt_sha256": prompt_sha256,
        }


def load_settings() -> GeneratorSettings:
    model_dir = _path("ANSKAR_SD_MODEL_DIR", INSTALLED_MODEL_DIR)
    return GeneratorSettings(
        executable=_path("ANSKAR_SD_CLI", INSTALLED_EXECUTABLE),
        diffusion_model=_path("ANSKAR_SD_DIFFUSION_MODEL", model_dir / "z_image_turbo-Q4_K.gguf"),
        text_encoder=_path(
            "ANSKAR_SD_TEXT_ENCODER",
            model_dir / "Qwen3-4B-Instruct-2507-UD-Q4_K_XL.gguf",
        ),
        vae=_path("ANSKAR_SD_VAE", model_dir / "ae.safetensors"),
        backend=_text("ANSKAR_SD_BACKEND", "vulkan0"),
        steps=_int("ANSKAR_SD_STEPS", 8),
        sampling_method=_text("ANSKAR_SD_SAMPLER", "euler"),
        cfg_scale=_float("ANSKAR_SD_CFG", 1.0),
        width=_int("ANSKAR_SD_WIDTH", DEFAULT_WIDTH),
        height=_int("ANSKAR_SD_HEIGHT", DEFAULT_HEIGHT),
        offload_to_cpu=_bool("ANSKAR_SD_OFFLOAD_TO_CPU", True),
        flash_attn=_text("ANSKAR_SD_FLASH_ATTN", "off,diffusion=on"),
        timeout_seconds=_int("ANSKAR_SD_TIMEOUT", 900),
        artworks_dir=_path("ANSKAR_ARTWORKS_DIR", PROJECT_ROOT / "data" / "artworks"),
    )


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return Path(value).expanduser()


def _text(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return float(value)


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")
