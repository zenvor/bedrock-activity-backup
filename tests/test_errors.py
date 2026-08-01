import subprocess
import unittest

from bedrock_activity_backup.errors import safe_error_label


class SafeErrorTests(unittest.TestCase):
    def test_called_process_error_does_not_expose_command(self):
        error = subprocess.CalledProcessError(
            23,
            ["rsync", "/private/world-name", "/private/backup-name"],
        )
        label = safe_error_label(error)
        self.assertEqual(label, "subprocess-exit-23")
        self.assertNotIn("private", label)

    def test_timeout_does_not_expose_command(self):
        error = subprocess.TimeoutExpired(["rsync", "/private/world"], 300)
        self.assertEqual(safe_error_label(error), "subprocess-timeout")
