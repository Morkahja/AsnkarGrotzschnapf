"""Desktop notification helpers for the daily pipeline."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from anskar.daily.notify import desktop_notification, notifications_enabled, notify_stage


class NotifyTests(unittest.TestCase):
    def test_anskar_notify_off_skips_send(self) -> None:
        with patch.dict(os.environ, {"ANSKAR_NOTIFY": "0"}):
            self.assertFalse(notifications_enabled())
            self.assertFalse(desktop_notification(title="t", body="b"))

    def test_stage_titles(self) -> None:
        seen: list[tuple[str, str]] = []

        def capture(*, title: str, body: str, runner=None) -> bool:
            seen.append((title, body))
            return True

        with patch("anskar.daily.notify.desktop_notification", side_effect=capture):
            self.assertTrue(notify_stage("generate", "Poem 1"))
            self.assertTrue(notify_stage("instagram"))
            self.assertTrue(notify_stage("etsy"))
        self.assertEqual(seen[0][0], "Generating artwork")
        self.assertEqual(seen[1][0], "Publishing to Instagram")
        self.assertEqual(seen[2][0], "Publishing to Etsy")
