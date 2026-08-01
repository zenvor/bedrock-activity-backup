import json
import tempfile
import unittest
from pathlib import Path

from bedrock_activity_backup.config import Config

from tests.helpers import make_config


class ConfigTests(unittest.TestCase):
    def test_repository_example_loads(self):
        example = Path(__file__).resolve().parents[1] / "config/config.example.json"
        config = Config.load(example)
        self.assertEqual(config.keep_snapshots, 4)

    def test_example_values_are_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            self.assertEqual(config.keep_snapshots, 4)
            self.assertEqual(config.interval_seconds, 1800)

    def test_relative_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            invalid = Config(**{**config.__dict__, "backup_root": Path("relative")})
            with self.assertRaisesRegex(ValueError, "absolute path"):
                invalid.validate()

    def test_unknown_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "bds_root": "/srv/bds",
                        "world_name": "World",
                        "bds_service": "bds.service",
                        "console_fifo": "/run/bds/console",
                        "state_file": "/run/bds/state.json",
                        "backup_root": "/srv/backups",
                        "unexpected": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown fields"):
                Config.load(path)

    def test_snapshot_count_has_safety_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            invalid = Config(**{**config.__dict__, "keep_snapshots": 1})
            with self.assertRaisesRegex(ValueError, "between 2 and 20"):
                invalid.validate()


if __name__ == "__main__":
    unittest.main()
