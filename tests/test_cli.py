import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bedrock_activity_backup.cli import main

from tests.helpers import create_owned_snapshot, make_config


class CliTests(unittest.TestCase):
    def test_status_keeps_activity_state_when_listing_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            config.snapshot_root.mkdir(parents=True)
            snapshot = create_owned_snapshot(config, "20260801T010000Z-00000001")
            output = io.StringIO()
            with patch(
                "bedrock_activity_backup.cli.Config.load", return_value=config
            ), redirect_stdout(output):
                status = main(["--config", "/ignored", "status"])
            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["state"]["phase"], "idle")
            self.assertEqual(payload["snapshot_states"]["verified"], [snapshot.name])

    def test_verify_outputs_machine_readable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            config.snapshot_root.mkdir(parents=True)
            snapshot = create_owned_snapshot(config, "20260801T010000Z-00000001")
            output = io.StringIO()
            with patch(
                "bedrock_activity_backup.cli.Config.load", return_value=config
            ), redirect_stdout(output):
                status = main(["--config", "/ignored", "verify", snapshot.name])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue())["snapshot"], snapshot.name)

    def test_restore_plan_outputs_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            config.snapshot_root.mkdir(parents=True)
            snapshot = create_owned_snapshot(config, "20260801T010000Z-00000001")
            output = io.StringIO()
            with patch(
                "bedrock_activity_backup.cli.Config.load", return_value=config
            ), redirect_stdout(output):
                status = main(
                    ["--config", "/ignored", "restore-plan", snapshot.name]
                )
            self.assertEqual(status, 0)
            self.assertRegex(
                json.loads(output.getvalue())["plan_sha256"], r"^[a-f0-9]{64}$"
            )

    def test_invalid_snapshot_name_is_not_echoed_in_error(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            private_value = "../private-world-name"
            with patch(
                "bedrock_activity_backup.cli.Config.load", return_value=config
            ), self.assertLogs("bedrock_activity_backup.cli", level="ERROR") as logs:
                status = main(
                    ["--config", "/ignored", "verify", private_value]
                )
            self.assertEqual(status, 1)
            self.assertNotIn(private_value, "\n".join(logs.output))
