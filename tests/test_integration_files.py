import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IntegrationFileTests(unittest.TestCase):
    def test_watcher_stop_timeout_has_backup_margin(self):
        unit = (ROOT / "systemd/bedrock-activity-backup.service").read_text(
            encoding="utf-8"
        )
        match = re.search(r"^TimeoutStopSec=(\d+)$", unit, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 600)

    def test_watcher_write_scope_excludes_manual_backups(self):
        unit = (ROOT / "systemd/bedrock-activity-backup.service").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "ReadWritePaths=/opt/minecraft-bedrock/backups/automatic /run/minecraft-bedrock",
            unit,
        )
        self.assertNotIn(
            "ReadWritePaths=/opt/minecraft-bedrock/backups /run", unit
        )

    def test_installer_requires_rsync_fsync_and_verifies_rollback_state(self):
        installer = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
        self.assertIn("rsync --help | grep -q -- '--fsync'", installer)
        self.assertIn("actual_bds_active", installer)
        self.assertIn("actual_watcher_active", installer)
        self.assertIn("actual_watcher_enabled", installer)
        self.assertIn(
            "install -d -m 0700 -o root -g root /var/lib/bedrock-activity-backup/rehearsals",
            installer,
        )
