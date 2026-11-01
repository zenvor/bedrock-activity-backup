import contextlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bedrock_activity_backup.console import BdsConsole


READY_OUTPUT = (
    "Data saved. Files are now ready to be copied.\n"
    "Test World/db/CURRENT:16, Test World/db/MANIFEST-000001:8, "
    "Test World/level.dat:5\n"
)


class ConsoleTests(unittest.TestCase):
    def test_journal_split_file_lists_are_reassembled_before_parsing(self):
        expected = {
            "测试 世界/db/CURRENT": 16,
            "测试 世界/db/MANIFEST-000001": 8,
            "测试 世界/level.dat": 5,
            **{f"测试 世界/db/{index:06d}.ldb": 123456 for index in range(2000)},
        }
        file_list = ", ".join(f"{name}:{size}" for name, size in expected.items()).encode("utf-8")
        self.assertGreater(len(file_list), 48 * 1024)
        for split in (48 * 1024, file_list.index(b", ") + 2, 1):
            with self.subTest(split=split):
                records = [
                    {"MESSAGE": "Data saved. Files are now ready to be copied.", "_STREAM_ID": "stdout"},
                    {"MESSAGE": list(file_list[:split]), "_STREAM_ID": "stdout", "_LINE_BREAK": "line-max"},
                    {"MESSAGE": "unprefixed stderr diagnostic", "_STREAM_ID": "stderr"},
                    {"MESSAGE": list(file_list[split:]), "_STREAM_ID": "stdout"},
                ]
                output = "\n".join(json.dumps(record) for record in records)
                result = subprocess.CompletedProcess([], 0, output, "")
                with patch("subprocess.run", return_value=result) as run:
                    console = BdsConsole("minecraft.service", Path("/tmp/unused"), run=run)
                    self.assertEqual(console._save_query_files(console._logs_after("cursor")), expected)
                    self.assertIn("json", run.call_args.args[0])
                    self.assertIn("--all", run.call_args.args[0])

    def test_partial_journal_continuation_is_not_a_complete_file_list(self):
        header, file_list = READY_OUTPUT.rstrip("\n").split("\n")
        records = [
            {"MESSAGE": header, "_STREAM_ID": "stdout"},
            {"MESSAGE": file_list + ", ", "_STREAM_ID": "stdout", "_LINE_BREAK": "line-max"},
        ]
        output = BdsConsole._join_journal_records("\n".join(json.dumps(record) for record in records))
        self.assertIsNone(BdsConsole._save_query_files(output))
        records.append({"MESSAGE": "Test World/db/000002.ldb:12", "_STREAM_ID": "stdout"})
        output = BdsConsole._join_journal_records("\n".join(json.dumps(record) for record in records))
        self.assertEqual(BdsConsole._save_query_files(output)["Test World/db/000002.ldb"], 12)

    def test_abnormal_journal_line_end_does_not_complete_a_file_list(self):
        header, file_list = READY_OUTPUT.rstrip("\n").split("\n")
        for line_break in ("eof", "nul", "pid-change"):
            with self.subTest(line_break=line_break):
                records = [
                    {"MESSAGE": header, "_STREAM_ID": "stdout"},
                    {"MESSAGE": file_list, "_STREAM_ID": "stdout", "_LINE_BREAK": line_break},
                ]
                output = BdsConsole._join_journal_records("\n".join(json.dumps(record) for record in records))
                self.assertIsNone(BdsConsole._save_query_files(output))

    def test_uncertain_hold_send_still_attempts_resume(self):
        console = BdsConsole("minecraft.service", Path("/tmp/unused"))
        sent = []

        def send(command):
            sent.append(command)
            if command == "save hold":
                raise OSError("write outcome uncertain")

        with patch.object(console, "_journal_cursor", return_value="cursor"), patch.object(console, "_send", side_effect=send), patch.object(console, "_wait_for"):
            with self.assertRaises(OSError):
                with console.paused_saves(30, 10):
                    self.fail("copy must not start")
        self.assertEqual(sent, ["save hold", "save resume"])

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
                READY_OUTPUT,
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
            + [
                "Data saved. Files are now ready to be copied.\nTest World/db/CURRENT:",
                "Data saved. Files are now ready to be copied.\nTest World/db/CURRENT:\n"
                + READY_OUTPUT,
            ]
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
            with console.paused_saves(30, 10) as files:
                self.assertEqual(files["Test World/db/MANIFEST-000001"], 8)

        self.assertEqual(clock.sleeps, [1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0])
        self.assertEqual(
            sent,
            ["save hold", *(["save query"] * 7), "save resume"],
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
                return_value=READY_OUTPUT,
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
            return_value=READY_OUTPUT,
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
            return_value=READY_OUTPUT,
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
                return_value=READY_OUTPUT,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
                with console.paused_saves(30, 10):
                    pass
        self.assertEqual(sent[-1], "save resume")


if __name__ == "__main__":
    unittest.main()
