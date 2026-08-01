import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bedrock_activity_backup.console import BdsConsole


class ConsoleTests(unittest.TestCase):
    def test_query_player_count_parses_runtime_response(self):
        console = BdsConsole("minecraft.service", Path("/tmp/unused"))
        with (
            patch.object(console, "_journal_cursor", return_value="cursor"),
            patch.object(console, "_send") as send,
            patch.object(
                console,
                "_wait_for",
                return_value="There are 2/4 players online:\n",
            ),
        ):
            self.assertEqual(console.query_player_count(8), 2)
            send.assert_called_once_with("list")

    def test_resume_is_sent_when_copy_body_raises(self):
        console = BdsConsole("minecraft.service", Path("/tmp/unused"))
        outputs = iter(
            [
                "Data saved. Files are now ready to be copied.",
                "Changes to the world are resumed.",
            ]
        )
        sent = []
        with (
            patch.object(console, "_journal_cursor", return_value="cursor"),
            patch.object(console, "_send", side_effect=sent.append),
            patch.object(console, "_logs_after", side_effect=lambda _cursor: next(outputs)),
            patch.object(console, "_wait_for", return_value="Changes to the world are resumed."),
        ):
            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                with console.paused_saves(30, 10):
                    raise RuntimeError("copy failed")
        self.assertEqual(sent[0:2], ["save hold", "save query"])
        self.assertEqual(sent[-1], "save resume")

    def test_multiline_command_is_rejected_before_fifo_access(self):
        console = BdsConsole("minecraft.service", Path("/tmp/unused"))
        with self.assertRaisesRegex(ValueError, "one line"):
            console._send("list\nstop")

    def test_resume_is_sent_even_when_confirmation_cursor_fails(self):
        console = BdsConsole("minecraft.service", Path("/tmp/unused"))
        cursors = iter(["hold-cursor", RuntimeError("journal failed")])
        sent = []

        def cursor():
            value = next(cursors)
            if isinstance(value, Exception):
                raise value
            return value

        with (
            patch.object(console, "_journal_cursor", side_effect=cursor),
            patch.object(console, "_send", side_effect=sent.append),
            patch.object(
                console,
                "_logs_after",
                return_value="Data saved. Files are now ready to be copied.",
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
                with console.paused_saves(30, 10):
                    pass
        self.assertEqual(sent[-1], "save resume")


if __name__ == "__main__":
    unittest.main()
