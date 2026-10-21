"""Post-wake production readiness — unrelated systemd failures must not block."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anskar.artwork.settings import GeneratorSettings
from anskar.daily.errors import DailyError
from anskar.daily.readiness import (
    assess_production_ready,
    expected_offline_cpus,
    ollama_serve_command,
    read_offline_cpus,
    wait_for_production_ready,
    _try_start_ollama,
)
from anskar.daily.rehearsal import launch_after_wake, wait_for_resume_ready


def _cpu_tree(root: Path, *, offline: set[int], online: set[int]) -> Path:
    for index in offline | online:
        cpu = root / f"cpu{index}"
        cpu.mkdir(parents=True)
        (cpu / "online").write_text("0\n" if index in offline else "1\n", encoding="utf-8")
    # Boot CPU without an online file stays online.
    (root / "cpu0").mkdir(exist_ok=True)
    return root


def _settings(tmpdir: Path) -> GeneratorSettings:
    tmpdir.mkdir(parents=True, exist_ok=True)
    exe = tmpdir / "sd-cli"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    diffusion = tmpdir / "model.gguf"
    text = tmpdir / "text.gguf"
    vae = tmpdir / "vae.safetensors"
    for path in (diffusion, text, vae):
        path.write_bytes(b"x")
    return GeneratorSettings(
        executable=exe,
        diffusion_model=diffusion,
        text_encoder=text,
        vae=vae,
        backend="vulkan0",
        steps=8,
        sampling_method="euler",
        cfg_scale=1.0,
        width=1600,
        height=1600,
        offload_to_cpu=True,
        flash_attn="off",
        timeout_seconds=60,
        artworks_dir=tmpdir / "artworks",
    )


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="anskar-ready-"))
        self.cpu = _cpu_tree(self.root / "cpu", offline={2, 10}, online={1, 3, 4})
        self.settings = _settings(self.root / "sd")
        self.python = self.root / "python"
        self.python.write_text("#!/bin/sh\n", encoding="utf-8")
        self.python.chmod(0o755)
        self.env = patch.dict(os.environ, {"ANSKAR_EXPECTED_CPU_OFFLINE": "2,10"})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def _ok_kwargs(self, **overrides):
        base = {
            "cpu_root": self.cpu,
            "project_root": self.root,
            "python_path": str(self.python),
            "network_probe": lambda: True,
            "ollama_probe": lambda: True,
            "system_state": "degraded",
            "runner": lambda _cmd: type(
                "R",
                (),
                {"returncode": 0, "stdout": "", "stderr": ""},
            )(),
        }
        base.update(overrides)
        with patch("anskar.daily.readiness.load_settings", return_value=self.settings):
            return assess_production_ready(**base)

    def test_degraded_unrelated_service_does_not_block(self) -> None:
        report = self._ok_kwargs(system_state="degraded")
        self.assertTrue(report.ok)
        self.assertIn("degraded", report.checks["system_state"])
        self.assertIsNone(report.failed)

    def test_correct_offline_cpu_configuration(self) -> None:
        offline = read_offline_cpus(self.cpu)
        self.assertEqual(offline, {2, 10})
        self.assertEqual(expected_offline_cpus(), frozenset({2, 10}))
        report = self._ok_kwargs()
        self.assertTrue(report.ok)
        self.assertIn("offline [2, 10]", report.checks["cpu_safety"])

    def test_incorrect_cpu_configuration_fails(self) -> None:
        bad = _cpu_tree(self.root / "cpu-bad", offline={2}, online={1, 3, 10})
        report = self._ok_kwargs(cpu_root=bad)
        self.assertFalse(report.ok)
        self.assertIsNotNone(report.failed)
        assert report.failed is not None
        self.assertTrue(report.failed.startswith("cpu_safety:"))

    def test_delayed_network_availability(self) -> None:
        network = iter([False, False, True])
        pauses: list[float] = []
        with patch("anskar.daily.readiness.load_settings", return_value=self.settings):
            report = wait_for_production_ready(
                sleep=pauses.append,
                settle_seconds=1,
                attempts=5,
                pause_seconds=2,
                system_state=lambda: "degraded",
                network_probe=lambda: next(network),
                ollama_probe=lambda: True,
                cpu_root=self.cpu,
                project_root=self.root,
                python_path=str(self.python),
                runner=lambda _cmd: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            )
        self.assertTrue(report.ok)
        self.assertEqual(pauses[0], 1)
        self.assertGreaterEqual(len(pauses), 3)

    def test_delayed_ollama_availability_and_start(self) -> None:
        ollama = iter([False, True])
        started: list[str] = []
        pauses: list[float] = []
        with patch("anskar.daily.readiness.load_settings", return_value=self.settings):
            report = wait_for_production_ready(
                sleep=pauses.append,
                settle_seconds=0,
                attempts=4,
                pause_seconds=1,
                system_state=lambda: "running",
                network_probe=lambda: True,
                ollama_probe=lambda: next(ollama),
                start_ollama=lambda: started.append("start"),
                cpu_root=self.cpu,
                project_root=self.root,
                python_path=str(self.python),
                runner=lambda _cmd: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            )
        self.assertTrue(report.ok)
        self.assertEqual(started, ["start"])

    def test_ollama_falls_back_to_serve_when_no_systemd_unit(self) -> None:
        """This machine has a tarball Ollama install — systemctl start ollama is a no-op."""
        spawned: list[str] = []

        def runner(command):
            return type(
                "R",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "Unit ollama.service not found.",
                },
            )()

        _try_start_ollama(runner, spawn_serve=lambda: spawned.append("serve"))
        self.assertEqual(spawned, ["serve"])

    def test_ollama_serve_command_drops_to_artist_when_root(self) -> None:
        with patch("anskar.daily.readiness.os.geteuid", return_value=0):
            with patch.dict(os.environ, {"ANSKAR_TEST_USER": "babuni"}):
                with patch("anskar.daily.readiness.pwd.getpwnam") as getpw:
                    getpw.return_value = type(
                        "Pw",
                        (),
                        {"pw_dir": "/home/babuni", "pw_uid": 1000},
                    )()
                    command = ollama_serve_command()
        self.assertEqual(command[0], "runuser")
        self.assertIn("babuni", command)
        self.assertIn("serve", command)
        self.assertTrue(any(part == "HOME=/home/babuni" for part in command))

    def test_successful_startup_after_wake(self) -> None:
        started: list[str] = []
        ok_runner = lambda _cmd: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("anskar.daily.readiness.load_settings", return_value=self.settings):
            wait_for_resume_ready(
                sleep=lambda _s: None,
                system_state=lambda: "degraded",
                network_probe=lambda: True,
                ollama_probe=lambda: True,
                cpu_root=self.cpu,
                project_root=self.root,
                python_path=str(self.python),
                runner=ok_runner,
                settle_seconds=0,
                attempts=1,
            )
            with tempfile.TemporaryDirectory() as temporary:
                with patch.dict(os.environ, {"ANSKAR_TEST_CYCLE": "1"}):
                    launch_after_wake(
                        sleep=lambda _s: None,
                        system_state=lambda: "degraded",
                        network_probe=lambda: True,
                        ollama_probe=lambda: True,
                        cpu_root=self.cpu,
                        project_root=self.root,
                        python_path=str(self.python),
                        runner=ok_runner,
                        start_work=lambda: started.append("work"),
                        directory=Path(temporary),
                    )
        self.assertEqual(started, ["work"])

    def test_genuine_dependency_failures_name_the_check(self) -> None:
        report = self._ok_kwargs(network_probe=lambda: False)
        self.assertFalse(report.ok)
        assert report.failed is not None
        self.assertTrue(report.failed.startswith("network:"))

        report = self._ok_kwargs(ollama_probe=lambda: False)
        self.assertFalse(report.ok)
        assert report.failed is not None
        self.assertTrue(report.failed.startswith("ollama:"))

        with self.assertRaises(DailyError) as ctx:
            with patch("anskar.daily.readiness.load_settings", return_value=self.settings):
                wait_for_production_ready(
                    sleep=lambda _s: None,
                    settle_seconds=0,
                    attempts=1,
                    system_state=lambda: "degraded",
                    network_probe=lambda: False,
                    ollama_probe=lambda: True,
                    cpu_root=self.cpu,
                    project_root=self.root,
                    python_path=str(self.python),
                    runner=lambda _cmd: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
                )
        self.assertIn("network:", str(ctx.exception))

    def test_missing_image_backend_is_named(self) -> None:
        broken = _settings(self.root / "broken")
        broken.executable.unlink()
        with patch("anskar.daily.readiness.load_settings", return_value=broken):
            report = assess_production_ready(
                cpu_root=self.cpu,
                project_root=self.root,
                python_path=str(self.python),
                network_probe=lambda: True,
                ollama_probe=lambda: True,
                runner=lambda _cmd: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
            )
        self.assertFalse(report.ok)
        assert report.failed is not None
        self.assertTrue(report.failed.startswith("image_backend:"))


if __name__ == "__main__":
    unittest.main()
