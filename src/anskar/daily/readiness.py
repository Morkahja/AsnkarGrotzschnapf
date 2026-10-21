"""Post-wake production readiness.

Unrelated systemd failures (is-system-running=degraded) must not block painting
when Anskar's own dependencies work.
"""

from __future__ import annotations

import os
import pwd
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from anskar.artwork.settings import load_settings
from anskar.config import PROJECT_ROOT
from anskar.daily.errors import DailyError
from anskar.daily.power import inhibit_command
from anskar.daily.schedule import default_python
from anskar.interpret import MODEL as OLLAMA_MODEL

# This machine's CPU safety workaround: cores 2 and 10 stay offline.
DEFAULT_EXPECTED_OFFLINE_CPUS = frozenset({2, 10})
CPU_ONLINE_ROOT = Path("/sys/devices/system/cpu")
# Post-suspend: give the machine and Ollama time to come back (~2.5 minutes).
SETTLE_SECONDS = 30
READY_ATTEMPTS = 24
READY_PAUSE_SECONDS = 5
OLLAMA_START_ATTEMPTS = 3
NETWORK_PROBE_HOST = "graph.instagram.com"
NETWORK_PROBE_PORT = 443
OLLAMA_HOST = os.environ.get("ANSKAR_OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_BIN = "/usr/bin/ollama"

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class ReadinessReport:
    ok: bool
    checks: dict[str, str]
    failed: str | None = None
    system_state: str | None = None

    def raise_if_failed(self) -> None:
        if self.ok:
            return
        detail = self.failed or "unknown dependency"
        raise DailyError(
            f"Production is not ready after wake: {detail}.",
            kind="error",
        )


def expected_offline_cpus() -> frozenset[int]:
    override = os.environ.get("ANSKAR_EXPECTED_CPU_OFFLINE")
    if override is None or not override.strip():
        return DEFAULT_EXPECTED_OFFLINE_CPUS
    values: set[int] = set()
    for part in override.replace(" ", "").split(","):
        if not part:
            continue
        values.add(int(part))
    return frozenset(values)


def wait_for_production_ready(
    *,
    sleep: Sleeper,
    runner: Runner | None = None,
    settle_seconds: float = SETTLE_SECONDS,
    attempts: int = READY_ATTEMPTS,
    pause_seconds: float = READY_PAUSE_SECONDS,
    system_state: Callable[[], str] | None = None,
    start_ollama: Callable[[], None] | None = None,
    network_probe: Callable[[], bool] | None = None,
    ollama_probe: Callable[[], bool] | None = None,
    cpu_root: Path = CPU_ONLINE_ROOT,
    project_root: Path | None = None,
    python_path: str | None = None,
) -> ReadinessReport:
    """
    Wait until Anskar's real production dependencies are available.

    systemctl is-system-running may report degraded because of an unrelated
    failed unit; that alone must not stop production.
    """
    sleep(settle_seconds)
    run = runner or _subprocess
    state_fn = system_state or _system_state
    last_state = ""
    last_report: ReadinessReport | None = None
    ollama_start = start_ollama
    ollama_starts = 0

    for attempt in range(attempts):
        last_state = state_fn().strip()
        report = assess_production_ready(
            runner=run,
            network_probe=network_probe,
            ollama_probe=ollama_probe,
            cpu_root=cpu_root,
            project_root=project_root,
            python_path=python_path,
            system_state=last_state,
        )
        last_report = report
        if report.ok:
            return report
        # Start (or restart) Ollama when that is what is missing. This machine
        # has a tarball install with no systemd unit — fall back to `ollama serve`.
        if (
            ollama_starts < OLLAMA_START_ATTEMPTS
            and report.failed
            and report.failed.startswith("ollama:")
        ):
            try:
                if ollama_start is not None:
                    ollama_start()
                else:
                    _try_start_ollama(run)
            except (OSError, subprocess.TimeoutExpired, DailyError):
                pass
            ollama_starts += 1
        if attempt + 1 < attempts:
            sleep(pause_seconds)

    assert last_report is not None
    last_report.raise_if_failed()
    return last_report


def assess_production_ready(
    *,
    runner: Runner | None = None,
    network_probe: Callable[[], bool] | None = None,
    ollama_probe: Callable[[], bool] | None = None,
    cpu_root: Path = CPU_ONLINE_ROOT,
    project_root: Path | None = None,
    python_path: str | None = None,
    system_state: str | None = None,
) -> ReadinessReport:
    """One-shot dependency assessment. degraded system state is informational only."""
    checks: dict[str, str] = {}
    root = project_root or PROJECT_ROOT
    python = python_path or default_python()
    run = runner or _subprocess

    # 1. Suspend/resume settle is handled by the caller; note observed state.
    if system_state:
        checks["system_state"] = (
            f"observed {system_state} (informational; degraded alone is allowed)"
        )
    else:
        checks["system_state"] = "not queried"

    # 2. CPU safety configuration.
    try:
        offline = read_offline_cpus(cpu_root)
        expected = expected_offline_cpus()
        if offline != expected:
            detail = (
                f"offline CPUs are {sorted(offline)}, expected {sorted(expected)}"
            )
            checks["cpu_safety"] = f"failed: {detail}"
            return ReadinessReport(
                False, checks, failed=f"cpu_safety: {detail}", system_state=system_state
            )
        checks["cpu_safety"] = f"ok (offline {sorted(expected)})"
    except (OSError, ValueError) as exc:
        checks["cpu_safety"] = f"failed: {exc}"
        return ReadinessReport(
            False, checks, failed=f"cpu_safety: {exc}", system_state=system_state
        )

    # 3. Project and Python environment.
    if not root.is_dir():
        detail = f"project root missing at {root}"
        checks["project"] = f"failed: {detail}"
        return ReadinessReport(False, checks, failed=f"project: {detail}", system_state=system_state)
    py = Path(python)
    if not py.is_file() or not os.access(py, os.X_OK):
        detail = f"Python executable not usable at {python}"
        checks["python"] = f"failed: {detail}"
        return ReadinessReport(False, checks, failed=f"python: {detail}", system_state=system_state)
    checks["project"] = f"ok ({root})"
    checks["python"] = f"ok ({python})"

    # 4. Ollama backend.
    ollama_ok = ollama_probe() if ollama_probe is not None else _ollama_reachable()
    if not ollama_ok:
        detail = f"Ollama is not available at {OLLAMA_HOST} (model {OLLAMA_MODEL})"
        checks["ollama"] = f"failed: {detail}"
        return ReadinessReport(False, checks, failed=f"ollama: {detail}", system_state=system_state)
    checks["ollama"] = f"ok ({OLLAMA_HOST}, model {OLLAMA_MODEL})"

    # 5. Image-generation backend and model files.
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001
        checks["image_backend"] = f"failed: {exc}"
        return ReadinessReport(
            False, checks, failed=f"image_backend: {exc}", system_state=system_state
        )
    missing = [
        path
        for path in (settings.executable, settings.diffusion_model, settings.text_encoder, settings.vae)
        if not path.is_file()
    ]
    if missing:
        detail = "missing " + ", ".join(str(path) for path in missing)
        checks["image_backend"] = f"failed: {detail}"
        return ReadinessReport(
            False, checks, failed=f"image_backend: {detail}", system_state=system_state
        )
    if not os.access(settings.executable, os.X_OK):
        detail = f"sd-cli is not executable at {settings.executable}"
        checks["image_backend"] = f"failed: {detail}"
        return ReadinessReport(
            False, checks, failed=f"image_backend: {detail}", system_state=system_state
        )
    checks["image_backend"] = f"ok ({settings.executable.name})"

    # 6. Network for Instagram publishing.
    net_ok = network_probe() if network_probe is not None else _network_reachable()
    if not net_ok:
        detail = f"cannot reach {NETWORK_PROBE_HOST}:{NETWORK_PROBE_PORT}"
        checks["network"] = f"failed: {detail}"
        return ReadinessReport(False, checks, failed=f"network: {detail}", system_state=system_state)
    checks["network"] = f"ok ({NETWORK_PROBE_HOST})"

    # 7. Sleep inhibitor can be acquired.
    try:
        _assert_inhibit_works(run, python)
        checks["sleep_inhibit"] = "ok"
    except DailyError as exc:
        checks["sleep_inhibit"] = f"failed: {exc}"
        return ReadinessReport(
            False, checks, failed=f"sleep_inhibit: {exc}", system_state=system_state
        )

    return ReadinessReport(True, checks, failed=None, system_state=system_state)


def read_offline_cpus(cpu_root: Path = CPU_ONLINE_ROOT) -> set[int]:
    """Return CPU indexes whose online file is 0. Does not change any CPU."""
    offline: set[int] = set()
    for path in sorted(cpu_root.glob("cpu[0-9]*")):
        name = path.name
        if not name[3:].isdigit():
            continue
        index = int(name[3:])
        online = path / "online"
        if not online.is_file():
            # Boot CPU often has no online file and stays online.
            continue
        value = online.read_text(encoding="utf-8").strip()
        if value == "0":
            offline.add(index)
        elif value != "1":
            raise ValueError(f"Unexpected online value {value!r} for {name}")
    return offline


def _assert_inhibit_works(runner: Runner, python: str) -> None:
    if not Path("/usr/bin/systemd-inhibit").is_file():
        raise DailyError("systemd-inhibit is not installed.", kind="error")
    command = inhibit_command(python, "-c", "pass")
    # When python is not the real interpreter in tests, callers inject runner.
    completed = runner(command)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise DailyError(
            f"systemd-inhibit could not be acquired ({detail or completed.returncode}).",
            kind="error",
        )


def _ollama_reachable() -> bool:
    host = OLLAMA_HOST.rstrip("/")
    url = f"{host}/api/tags"
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=3) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _try_start_ollama(
    runner: Runner,
    *,
    spawn_serve: Callable[[], None] | None = None,
) -> None:
    """
    Bring Ollama up without asking a person.

    Prefer a systemd unit when one exists. On this computer Ollama was installed
    from the official tarball and has no unit, so fall back to `ollama serve`
    as the artist user (ANSKAR_TEST_USER when the wake service runs as root).
    """
    for command in (
        ["systemctl", "--user", "start", "ollama"],
        ["systemctl", "start", "ollama"],
    ):
        try:
            completed = runner(command)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            print("Started Ollama via systemctl.", flush=True)
            return
    if spawn_serve is not None:
        spawn_serve()
        return
    _spawn_ollama_serve()


def ollama_serve_command() -> list[str]:
    """Argv that starts the local Ollama HTTP server in the background."""
    exe = shutil.which("ollama") or OLLAMA_BIN
    user = os.environ.get("ANSKAR_TEST_USER") or ""
    if os.geteuid() == 0 and user and user != "root":
        info = pwd.getpwnam(user)
        return [
            "runuser",
            "-u",
            user,
            "--",
            "env",
            f"HOME={info.pw_dir}",
            f"USER={user}",
            f"LOGNAME={user}",
            exe,
            "serve",
        ]
    return [exe, "serve"]


def _spawn_ollama_serve() -> None:
    """Detach `ollama serve` so readiness can keep probing the port."""
    if _ollama_reachable():
        return
    command = ollama_serve_command()
    print(f"Starting Ollama with: {' '.join(command)}", flush=True)
    subprocess.Popen(
        command,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _network_reachable() -> bool:
    try:
        with socket.create_connection((NETWORK_PROBE_HOST, NETWORK_PROBE_PORT), timeout=5):
            return True
    except OSError:
        return False


def _system_state() -> str:
    try:
        completed = _subprocess(["systemctl", "is-system-running"])
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or "").strip()


def _subprocess(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
