#!/usr/bin/env python3
"""Comprehensive unit tests for gitops-updater."""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from updater import (
    ApplyResult,
    GitOpsEngine,
    ImageRef,
    RegistryImage,
    ScanResult,
    Update,
    check_plex_active_streams,
    is_major_change,
    is_plex_workload,
    parse_semver,
)
from bot import TelegramBot


class TestSemverAndClassification(unittest.TestCase):
    def test_parse_semver(self):
        self.assertEqual(parse_semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_semver("4.0.9"), (4, 0, 9))
        self.assertEqual(parse_semver("1.43.5.11079-b3b13e506"), (1, 43, 5))
        self.assertEqual(parse_semver("1.24.1"), (1, 24, 1))
        self.assertEqual(parse_semver("15.4-alpine"), (15, 4, 0))
        self.assertEqual(parse_semver("v2"), (2, 0, 0))
        self.assertIsNone(parse_semver("latest"))
        self.assertIsNone(parse_semver(""))
        self.assertIsNone(parse_semver(None))

    def test_is_major_change(self):
        # Patch updates: same major, same minor
        self.assertFalse(is_major_change("4.0.9", "4.0.10"))
        self.assertFalse(is_major_change("v2.18.2", "v2.18.3"))
        # Minor updates: same major, minor bumped
        self.assertFalse(is_major_change("1.24.1", "1.25.0"))
        self.assertFalse(is_major_change("1.43.4.1000", "1.43.5.11079-b3b13e506"))
        # Major updates: major version changed
        self.assertTrue(is_major_change("15.4", "16.0"))
        self.assertTrue(is_major_change("v1.9.5", "v2.0.0"))
        # Non-semver or unrecognized versions fail safe as major/manual approval
        self.assertTrue(is_major_change("latest", "1.2.3"))
        self.assertTrue(is_major_change("v1.0.0", "custom-tag"))

    def test_is_plex_workload(self):
        self.assertTrue(is_plex_workload("mediaslave", "plexinc/pms-docker"))
        self.assertTrue(is_plex_workload("plex", "linuxserver/plex"))
        self.assertTrue(is_plex_workload("my-plex-server", "some/repo"))
        self.assertFalse(is_plex_workload("sonarr", "linuxserver/sonarr"))
        self.assertFalse(is_plex_workload("tautulli", "ghcr.io/linuxserver/tautulli"))
        self.assertFalse(is_plex_workload("postgres", "postgres:15"))


class TestPlexStreamingValidation(unittest.TestCase):
    @patch("updater.urlopen")
    def test_plex_idle(self, mock_urlopen):
        # Mock idle response
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = json.dumps({"MediaContainer": {"size": 0, "Metadata": []}}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        is_streaming, reason = check_plex_active_streams("http://192.168.0.10:32400")
        self.assertFalse(is_streaming)
        self.assertEqual(reason, "No active streams")

    @patch("updater.urlopen")
    def test_plex_active_stream(self, mock_urlopen):
        # Mock active streaming response
        active_payload = {
            "MediaContainer": {
                "size": 1,
                "Metadata": [
                    {
                        "title": "Episode 1",
                        "grandparentTitle": "Test Show",
                        "User": {"title": "Alice"},
                        "Player": {"title": "Living Room Apple TV", "state": "playing"},
                    }
                ],
            }
        }
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = json.dumps(active_payload).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        is_streaming, reason = check_plex_active_streams("http://192.168.0.10:32400")
        self.assertTrue(is_streaming)
        self.assertIn("Alice", reason)
        self.assertIn("Test Show - Episode 1", reason)
        self.assertIn("Living Room Apple TV", reason)

    @patch("updater.urlopen")
    def test_plex_unreachable_fail_safe(self, mock_urlopen):
        # When Plex is unreachable, fail-closed to prevent interrupting playback
        mock_urlopen.side_effect = TimeoutError("Connection timed out")
        is_streaming, reason = check_plex_active_streams("http://192.168.0.10:32400")
        self.assertTrue(is_streaming)
        self.assertIn("Failed to reach Plex server", reason)

    @patch("updater.urlopen")
    def test_plex_xml_fallback(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "text/xml"}
        mock_resp.read.return_value = b'<MediaContainer size="2"><Video title="Movie"/></MediaContainer>'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        is_streaming, reason = check_plex_active_streams("http://192.168.0.10:32400")
        self.assertTrue(is_streaming)
        self.assertIn("2 stream(s) active in Plex", reason)


class TestEngineApplyUpdates(unittest.TestCase):
    def setUp(self):
        self.repo_dir = Path("/tmp/mock_repo")
        self.engine = GitOpsEngine(
            repo_dir=self.repo_dir,
            apps_dir_name="apps",
            flux_enabled=False,
            plex_url="http://mock-plex:32400",
        )

    @patch.object(GitOpsEngine, "ensure_latest_git")
    @patch.object(GitOpsEngine, "is_plex_streaming")
    def test_apply_updates_skips_plex_when_streaming(self, mock_streaming, mock_git):
        mock_streaming.return_value = (True, "Alice watching Movie")

        plex_img = ImageRef(self.repo_dir / "apps/plex.yaml", 10, "mediaslave", "plexinc/pms-docker:1.43.4", "plexinc/pms-docker", "latest")
        sonarr_img = ImageRef(self.repo_dir / "apps/sonarr.yaml", 5, "sonarr", "linuxserver/sonarr:4.0.9", "linuxserver/sonarr", "latest")

        plex_update = Update(plex_img, RegistryImage("plexinc/pms-docker", "latest", "sha:1", "1.43.5"), "1.43.4", "1.43.5", is_major=False, is_plex=True)
        sonarr_update = Update(sonarr_img, RegistryImage("linuxserver/sonarr", "latest", "sha:2", "4.0.10"), "4.0.9", "4.0.10", is_major=False, is_plex=False)

        with patch("updater.replace_image"), patch("updater.add_rollout_marker"), patch("subprocess.run"), patch("subprocess.check_output", return_value="abc1234"):
            result = self.engine.apply_updates([plex_update, sonarr_update])

        self.assertTrue(result.success)
        self.assertEqual(len(result.updated_workloads), 1)
        self.assertIn("sonarr", result.updated_workloads[0])
        self.assertEqual(len(result.deferred_workloads), 1)
        self.assertIn("mediaslave", result.deferred_workloads[0])
        self.assertIn("mediaslave", result.deferred_reasons)

    @patch.object(GitOpsEngine, "ensure_latest_git")
    @patch.object(GitOpsEngine, "is_plex_streaming")
    def test_apply_updates_applies_plex_when_idle(self, mock_streaming, mock_git):
        mock_streaming.return_value = (False, "No active streams")

        plex_img = ImageRef(self.repo_dir / "apps/plex.yaml", 10, "mediaslave", "plexinc/pms-docker:1.43.4", "plexinc/pms-docker", "latest")
        plex_update = Update(plex_img, RegistryImage("plexinc/pms-docker", "latest", "sha:1", "1.43.5"), "1.43.4", "1.43.5", is_major=False, is_plex=True)

        with patch("updater.replace_image"), patch("updater.add_rollout_marker"), patch("subprocess.run"), patch("subprocess.check_output", return_value="def5678"):
            result = self.engine.apply_updates([plex_update])

        self.assertTrue(result.success)
        self.assertEqual(len(result.updated_workloads), 1)
        self.assertIn("mediaslave", result.updated_workloads[0])
        self.assertEqual(len(result.deferred_workloads), 0)


class TestBotProcessScan(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock(spec=GitOpsEngine)
        self.bot = TelegramBot(
            token="dummy_token",
            allowed_chat_ids={12345},
            engine=self.engine,
            cluster_name="Test Cluster",
        )
        self.bot.send_message = MagicMock()
        self.bot.edit_message = MagicMock()
        self.bot.broadcast = MagicMock()

    def test_auto_applies_minor_patch_and_prompts_for_major(self):
        sonarr_img = ImageRef(Path("/tmp/sonarr.yaml"), 1, "sonarr", "sonarr:4.0.9", "sonarr", "latest")
        pg_img = ImageRef(Path("/tmp/pg.yaml"), 1, "postgres", "postgres:15.4", "postgres", "15")

        sonarr_update = Update(sonarr_img, RegistryImage("sonarr", "latest", "sha:s", "4.0.10"), "4.0.9", "4.0.10", is_major=False, is_plex=False)
        pg_update = Update(pg_img, RegistryImage("postgres", "16", "sha:p", "16.0"), "15.4", "16.0", is_major=True, is_plex=False)

        scan = ScanResult(
            scanned_at=datetime.now(timezone.utc),
            updates=[sonarr_update, pg_update],
            up_to_date_count=10,
        )

        self.engine.apply_updates.return_value = ApplyResult(
            applied_at=datetime.now(timezone.utc),
            success=True,
            commit_sha="a1b2c3d",
            updated_workloads=["sonarr (4.0.9 -> 4.0.10)"],
        )

        self.bot.process_scan(scan, chat_id=12345, is_scheduled=False)

        # Verify engine.apply_updates was called with sonarr only
        self.engine.apply_updates.assert_called_once()
        applied_batch = self.engine.apply_updates.call_args[0][0]
        self.assertEqual(len(applied_batch), 1)
        self.assertEqual(applied_batch[0].image.workload, "sonarr")

        # Verify summary was sent for sonarr
        sent_texts = [call[0][1] for call in self.bot.send_message.call_args_list]
        self.assertTrue(any("Auto-Applied Image Updates" in t and "sonarr" in t for t in sent_texts))

        # Verify prompt with buttons was sent for postgres (major)
        prompt_call = next(call for call in self.bot.send_message.call_args_list if "Major Version Updates Available" in call[0][1])
        self.assertIn("postgres", prompt_call[0][1])
        self.assertIn("reply_markup", prompt_call[1])

    def test_plex_only_auto_update_deferred_when_streaming(self):
        plex_img = ImageRef(Path("/tmp/plex.yaml"), 1, "mediaslave", "plexinc/pms-docker:1.43.4", "plexinc/pms-docker", "latest")
        plex_update = Update(plex_img, RegistryImage("plexinc/pms-docker", "latest", "sha:1", "1.43.5"), "1.43.4", "1.43.5", is_major=False, is_plex=True)

        scan = ScanResult(
            scanned_at=datetime.now(timezone.utc),
            updates=[plex_update],
            up_to_date_count=5,
        )

        self.engine.is_plex_streaming.return_value = (True, "Alice watching Movie")

        self.bot.process_scan(scan, chat_id=12345, is_scheduled=False)

        # Ensure apply_updates was NOT called on engine
        self.engine.apply_updates.assert_not_called()

        # Ensure deferred notification was sent
        sent_texts = [call[0][1] for call in self.bot.send_message.call_args_list]
        self.assertTrue(any("Plex update deferred" in t and "Alice watching Movie" in t for t in sent_texts))

    def test_only_major_updates_requires_approval_and_no_auto_apply(self):
        pg_img = ImageRef(Path("/tmp/pg.yaml"), 1, "postgres", "postgres:15.4", "postgres", "15")
        pg_update = Update(pg_img, RegistryImage("postgres", "16", "sha:p", "16.0"), "15.4", "16.0", is_major=True, is_plex=False)

        scan = ScanResult(
            scanned_at=datetime.now(timezone.utc),
            updates=[pg_update],
            up_to_date_count=8,
        )

        self.bot.process_scan(scan, chat_id=12345, is_scheduled=False)

        # No auto-apply
        self.engine.apply_updates.assert_not_called()

        # Approval prompt sent
        prompt_call = next(call for call in self.bot.send_message.call_args_list if "Major Version Updates Available" in call[0][1])
        self.assertIn("postgres", prompt_call[0][1])
        self.assertIn("reply_markup", prompt_call[1])


if __name__ == "__main__":
    unittest.main()
