import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bedrock_activity_backup.console import BdsConsole


class ConsoleTests(unittest.TestCase):
    class FakeClock:
        def __init__(self):
            self.value = 0.0
            self.sleeps = []

        def monotonic(self):
            return self.value

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.value += seconds

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

    def test_save_query_waits_then_uses_capped_exponential_backoff(self):
        clock = self.FakeClock()
        console = BdsConsole(
            "minecraft.service",
            Path("/tmp/unused"),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        outputs = iter(
            ["A previous save has not been completed."] * 5
            + ["Data saved. Files are now ready to be copied."]
        )
        sent = []
        with patch.object(console, "_journal_cursor", return_value="cursor"), patch.object(
            console, "_send", side_effect=sent.append
        ), patch.object(
            console, "_logs_after", side_effect=lambda _cursor: next(outputs)
        ), patch.object(
            console,
            "_wait_for",
            return_value="Changes to the world are resumed.",
        ), self.assertLogs("bedrock_activity_backup.console", level="INFO") as logs:
            with console.paused_saves(30, 10):
                pass

        self.assertEqual(clock.sleeps, [1.0, 2.0, 4.0, 5.0, 5.0, 5.0])
        self.assertEqual(
            sent,
            ["save hold", *(["save query"] * 6), "save resume"],
        )
        self.assertTrue(any("observed a previous-save warning" in entry for entry in logs.output))
        self.assertTrue(any("ready for copying" in entry for entry in logs.output))

    def test_short_ready_timeout_queries_immediately(self):
        clock = self.FakeClock()
        console = BdsConsole(
            "minecraft.service",
            Path("/tmp/unused"),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        sent = []
        with (
            patch.object(console, "_journal_cursor", return_value="cursor"),
            patch.object(console, "_send", side_effect=sent.append),
            patch.object(
                console,
                "_logs_after",
                return_value="Data saved. Files are now ready to be copied.",
            ),
            patch.object(console, "_wait_for", return_value="Changes to the world are resumed."),
        ):
            with console.paused_saves(1, 10):
                pass

        self.assertEqual(clock.sleeps, [])
        self.assertEqual(sent, ["save hold", "save query", "save resume"])

    def test_ready_timeout_does_not_query_at_deadline_and_resumes(self):
        clock = self.FakeClock()
        console = BdsConsole(
            "minecraft.service",
            Path("/tmp/unused"),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        sent = []
        with (
            patch.object(console, "_journal_cursor", return_value="cursor"),
            patch.object(console, "_send", side_effect=sent.append),
            patch.object(console, "_logs_after", return_value="Saving..."),
            patch.object(console, "_wait_for", return_value="Changes to the world are resumed."),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not reach backup-ready"):
                with console.paused_saves(3, 10):
                    pass

        self.assertEqual(clock.sleeps, [1.0])
        self.assertEqual(sent, ["save hold", "save query", "save resume"])

    def test_resume_send_retries_before_confirmation(self):
        console = BdsConsole("minecraft.service", Path("/tmp/unused"), sleep=lambda _seconds: None)
        sent = []

        def send(command):
            sent.append(command)
            if command == "save resume" and sent.count(command) < 3:
                raise RuntimeError("fifo unavailable")

        with patch.object(console, "_journal_cursor", return_value="cursor"), patch.object(
            console, "_send", side_effect=send
        ), patch.object(
            console,
            "_logs_after",
            return_value="Data saved. Files are now ready to be copied.",
        ), patch.object(
            console,
            "_wait_for",
            return_value="Changes to the world are resumed.",
        ), self.assertLogs("bedrock_activity_backup.console", level="CRITICAL") as logs:
            with console.paused_saves(30, 10):
                pass

        self.assertEqual(sent.count("save resume"), 3)
        self.assertEqual(len(logs.output), 2)

    def test_resume_send_failure_after_all_retries_is_reported(self):
        console = BdsConsole("minecraft.service", Path("/tmp/unused"), sleep=lambda _seconds: None)
        sent = []

        def send(command):
            sent.append(command)
            if command == "save resume":
                raise RuntimeError("fifo unavailable")

        with patch.object(console, "_journal_cursor", return_value="cursor"), patch.object(
            console, "_send", side_effect=send
        ), patch.object(
            console,
            "_logs_after",
            return_value="Data saved. Files are now ready to be copied.",
        ), self.assertLogs("bedrock_activity_backup.console", level="CRITICAL") as logs:
            with self.assertRaisesRegex(RuntimeError, "could not send BDS save resume"):
                with console.paused_saves(30, 10):
                    pass

        self.assertEqual(sent.count("save resume"), 3)
        self.assertEqual(len(logs.output), 3)

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
