import importlib.util
import os
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "inline_ai_complete.py"


spec = importlib.util.spec_from_file_location("inline_ai_complete", MODULE_PATH)
inline_ai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inline_ai)


class InlineAICompleteTests(unittest.TestCase):
    def setUp(self):
        self._old_histfile = os.environ.get("HISTFILE")
        self._old_session_log = os.environ.get("TERMTAB_SESSION_LOG")
        self._old_output_last = os.environ.get("TERMTAB_OUTPUT_LAST")
        os.environ.pop("TERMTAB_OUTPUT_LAST", None)
        # Isolate from the developer's live termtab session: ambient session
        # entries otherwise leak into history/recency scoring.
        os.environ["TERMTAB_SESSION_LOG"] = "/tmp/termtab-test-missing-session.log"

    def tearDown(self):
        if self._old_histfile is None:
            os.environ.pop("HISTFILE", None)
        else:
            os.environ["HISTFILE"] = self._old_histfile
        if self._old_session_log is None:
            os.environ.pop("TERMTAB_SESSION_LOG", None)
        else:
            os.environ["TERMTAB_SESSION_LOG"] = self._old_session_log
        if self._old_output_last is None:
            os.environ.pop("TERMTAB_OUTPUT_LAST", None)
        else:
            os.environ["TERMTAB_OUTPUT_LAST"] = self._old_output_last

    def test_redacts_secret_like_values(self):
        api_key = "sk-" + "x" * 16
        aws_key = "AKIA" + "1" * 16
        text = f"export OPENROUTER_API_KEY={api_key} aws={aws_key}"
        redacted = inline_ai.redact(text)
        self.assertIn("<redacted>", redacted)
        self.assertNotIn(api_key, redacted)
        self.assertNotIn(aws_key, redacted)

    def test_session_mkdir_suggests_cd_followup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = pathlib.Path(tmpdir)
            (cwd / "app").mkdir()
            with tempfile.NamedTemporaryFile("w", delete=False) as session:
                session.write(f"1\t0\t{cwd}\tmkdir app\n")
                session_log = session.name
            with tempfile.NamedTemporaryFile("w", delete=False) as history:
                histfile = history.name

            os.environ["TERMTAB_SESSION_LOG"] = session_log
            os.environ["HISTFILE"] = histfile
            try:
                suggestion = inline_ai.inferred_next_command(
                    "c",
                    str(cwd),
                    {"enabled": True, "session_enabled": True, "session_max_entries": 200},
                )
            finally:
                os.unlink(session_log)
                os.unlink(histfile)

        self.assertEqual(suggestion, "cd app")

    def test_history_mkdir_fallback_suggests_cd_for_open_shells(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = pathlib.Path(tmpdir)
            (cwd / "demo dir").mkdir()
            with tempfile.NamedTemporaryFile("w", delete=False) as history:
                history.write(": 1:0;mkdir 'demo dir'\n")
                histfile = history.name

            os.environ["TERMTAB_SESSION_LOG"] = "/tmp/termtab-test-missing-session.log"
            os.environ["HISTFILE"] = histfile
            try:
                suggestion = inline_ai.inferred_next_command(
                    "cd ",
                    str(cwd),
                    {"enabled": True, "session_enabled": True, "session_max_entries": 200},
                )
            finally:
                os.unlink(histfile)

        self.assertEqual(suggestion, "cd 'demo dir'")

    def test_session_mv_suggests_cd_to_renamed_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = pathlib.Path(tmpdir)
            (cwd / "theGrid").mkdir()
            with tempfile.NamedTemporaryFile("w", delete=False) as session:
                session.write(f"1\t0\t{cwd}\tmv HardHarness theGrid\n")
                session_log = session.name
            with tempfile.NamedTemporaryFile("w", delete=False) as history:
                histfile = history.name

            os.environ["TERMTAB_SESSION_LOG"] = session_log
            os.environ["HISTFILE"] = histfile
            try:
                suggestion = inline_ai.inferred_next_command(
                    "cd th",
                    str(cwd),
                    {"enabled": True, "session_enabled": True, "session_max_entries": 200},
                )
            finally:
                os.unlink(session_log)
                os.unlink(histfile)

        self.assertEqual(suggestion, "cd theGrid")

    def test_session_mv_to_existing_parent_suggests_moved_child(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = pathlib.Path(tmpdir)
            (cwd / "archive" / "HardHarness").mkdir(parents=True)
            with tempfile.NamedTemporaryFile("w", delete=False) as session:
                session.write(f"1\t0\t{cwd}\tmv HardHarness archive\n")
                session_log = session.name
            with tempfile.NamedTemporaryFile("w", delete=False) as history:
                histfile = history.name

            os.environ["TERMTAB_SESSION_LOG"] = session_log
            os.environ["HISTFILE"] = histfile
            try:
                suggestion = inline_ai.inferred_next_command(
                    "cd ar",
                    str(cwd),
                    {"enabled": True, "session_enabled": True, "session_max_entries": 200},
                )
            finally:
                os.unlink(session_log)
                os.unlink(histfile)

        self.assertEqual(suggestion, "cd archive/HardHarness")

    def test_mkdir_followup_only_uses_immediate_previous_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = pathlib.Path(tmpdir)
            (cwd / "old-app").mkdir()
            with tempfile.NamedTemporaryFile("w", delete=False) as session:
                session.write(f"1\t0\t{cwd}\tmkdir old-app\n")
                session.write(f"2\t0\t{cwd}\tls\n")
                session_log = session.name
            with tempfile.NamedTemporaryFile("w", delete=False) as history:
                histfile = history.name

            os.environ["TERMTAB_SESSION_LOG"] = session_log
            os.environ["HISTFILE"] = histfile
            try:
                suggestion = inline_ai.inferred_next_command(
                    "c",
                    str(cwd),
                    {"enabled": True, "session_enabled": True, "session_max_entries": 200},
                )
            finally:
                os.unlink(session_log)
                os.unlink(histfile)

        self.assertEqual(suggestion, "")

    def test_history_weighting_prefers_recent_prefix_match(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as history:
            history.write(": 1:0;git status --short\n")
            history.write(": 2:0;git status --short\n")
            history.write(": 3:0;git stash list\n")
            history.write(": 4:0;git status --short --branch\n")
            histfile = history.name

        os.environ["HISTFILE"] = histfile
        try:
            suggestion = inline_ai.best_history_suggestion(
                "git sta",
                {
                    "enabled": True,
                    "direct_match_min_chars": 3,
                    "max_entries": 100,
                    "max_bytes": 65536,
                    "top_matches": 8,
                    "max_suggestion_chars": 320,
                    "recency_weight": 100,
                    "frequency_weight": 8,
                },
            )
        finally:
            os.unlink(histfile)

        self.assertEqual(suggestion, "git status --short --branch")

    def test_merge_completion_handles_suffix_or_full_line(self):
        self.assertEqual(inline_ai.merge_completion("git sta", "tus"), "git status")
        self.assertEqual(inline_ai.merge_completion("git sta", "git status"), "git status")

    def test_merge_completion_rejects_path_replacement_for_partial_token(self):
        self.assertEqual(inline_ai.merge_completion("cd th", "~/Projects/theGrid"), "")

    def test_cd_path_suggestion_uses_current_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = pathlib.Path(tmpdir)
            (cwd / "swarm").mkdir()
            (cwd / "webforge").mkdir()
            self.assertEqual(inline_ai.cd_path_suggestion("cd sw", tmpdir), "cd swarm")

    def test_cd_path_suggestion_skips_ambiguous_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = pathlib.Path(tmpdir)
            (cwd / "webforge").mkdir()
            (cwd / "webforge-ai-search").mkdir()
            self.assertEqual(inline_ai.cd_path_suggestion("cd we", tmpdir), "")

    def test_cd_path_suggestion_escapes_spaces_keeping_typed_prefix(self):
        # The suggestion must extend the typed line verbatim or zsh's
        # prefix gate drops the ghost text ('cd demo' vs "cd 'demo app'").
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = pathlib.Path(tmpdir)
            (cwd / "demo app").mkdir()
            self.assertEqual(inline_ai.cd_path_suggestion("cd demo", tmpdir), "cd demo\\ app")

    def test_cd_path_suggestion_preserves_tilde_prefix(self):
        old_home = os.environ.get("HOME")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["HOME"] = tmpdir
                pathlib.Path(tmpdir, "Projects").mkdir()
                self.assertEqual(
                    inline_ai.cd_path_suggestion("cd ~/Pro", "/"), "cd ~/Projects"
                )
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home

    def test_cd_path_suggestion_survives_unknown_user_tilde(self):
        self.assertEqual(
            inline_ai.cd_path_suggestion("cd ~termtabnosuchuser99/pro", "/"), ""
        )

    def test_detect_path_correction_survives_unknown_user_tilde(self):
        output = "cat: ~termtabnosuchuser99/notes.txt: No such file or directory"
        self.assertEqual(
            inline_ai.detect_path_correction(
                "cat ~termtabnosuchuser99/notes.txt", output, "/"
            ),
            "",
        )

    def test_redact_covers_private_key_block(self):
        pem = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ==\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        redacted = inline_ai.redact(f"leaked:\n{pem}\ndone")
        self.assertNotIn("b3BlbnNzaC1rZXktdjEA", redacted)
        self.assertNotIn("PRIVATE KEY-----", redacted.replace("<redacted>", ""))
        self.assertIn("done", redacted)

    def test_redact_covers_truncated_private_key_block(self):
        truncated = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA7pQ=="
        )
        redacted = inline_ai.redact(truncated)
        self.assertNotIn("MIIEpAIBAAKCAQEA", redacted)

    def test_sanitize_typescript_strips_ansi_and_collapses_cr(self):
        raw = b"\x1b[31mhello\x1b[0m\r\nworld\rdone\n"
        cleaned = inline_ai.sanitize_typescript(raw)
        self.assertNotIn("\x1b", cleaned)
        self.assertIn("hello", cleaned)
        lines = cleaned.split("\n")
        self.assertIn("done", lines)

    def test_sanitize_typescript_handles_backspace(self):
        raw = b"abxc\b\bcd"
        cleaned = inline_ai.sanitize_typescript(raw)
        self.assertEqual(cleaned, "abcd")

    def test_detect_correction_for_command_not_found(self):
        original = inline_ai.cached_path_executables
        inline_ai.cached_path_executables = lambda: ["git", "ls", "grep", "go"]
        try:
            suggestion = inline_ai.detect_correction(
                "gti status",
                "zsh: command not found: gti\n",
            )
        finally:
            inline_ai.cached_path_executables = original
        self.assertEqual(suggestion, "git status")

    def test_detect_correction_for_git_did_you_mean(self):
        output = (
            "git: 'stauts' is not a git command. See 'git --help'.\n"
            "\n"
            "The most similar commands are\n"
            "\tstatus\n"
            "\tstash\n"
        )
        suggestion = inline_ai.detect_correction("git stauts --short", output)
        self.assertEqual(suggestion, "git status --short")

    def test_correction_suggestion_returns_fix_when_prefix_matches(self):
        event = {
            "exit": 127,
            "cwd": "/tmp",
            "cmd": "gti status",
            "output": "zsh: command not found: gti\n",
        }
        original = inline_ai.cached_path_executables
        inline_ai.cached_path_executables = lambda: ["git", "ls"]
        try:
            self.assertEqual(inline_ai.correction_suggestion("g", "/tmp", event), "git status")
            self.assertEqual(inline_ai.correction_suggestion("gi", "/tmp", event), "git status")
            self.assertEqual(inline_ai.correction_suggestion("npm i", "/tmp", event), "")
            self.assertEqual(inline_ai.correction_suggestion("", "/tmp", event), "")
        finally:
            inline_ai.cached_path_executables = original

    def test_correction_suggestion_can_return_nonprefix_replacement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / "HardHarness").mkdir()
            event = {
                "exit": 1,
                "cwd": tmpdir,
                "cmd": "mv HardHardness theGrid",
                "output": "mv: rename HardHardness to theGrid: No such file or directory\n",
            }
            self.assertEqual(
                inline_ai.correction_suggestion("mv HardHardness theGrid", tmpdir, event, require_prefix=True),
                "",
            )
            self.assertEqual(
                inline_ai.correction_suggestion("mv HardHardness theGrid", tmpdir, event, require_prefix=False),
                "mv HardHarness theGrid",
            )

    def test_find_retry_context_skips_intervening_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tty_path = pathlib.Path(tmpdir) / "session.tty"
            tty_path.write_bytes(b"mv: rename HardHardness to theGrid: No such file or directory\n")
            end = tty_path.stat().st_size
            out_path = pathlib.Path(tmpdir) / "session.out.tsv"
            rows = [
                ["1700000001", "1", "/tmp/x", "mv HardHardness theGrid", str(tty_path), "0", str(end)],
                ["1700000002", "0", "/tmp/x", "ls", str(tty_path), str(end), str(end)],
            ]
            out_path.write_text("\n".join("\t".join(r) for r in rows) + "\n")
            os.environ["TERMTAB_OUTPUT_LAST"] = str(out_path)
            ev = inline_ai.find_retry_context("mv HardHardness theGrid", "/tmp/x")
        self.assertIsNotNone(ev)
        self.assertEqual(ev["cmd"], "mv HardHardness theGrid")
        self.assertIn("No such file or directory", ev["output"])

    def test_find_retry_context_uses_failure_output_when_status_was_logged_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tty_path = pathlib.Path(tmpdir) / "session.tty"
            tty_path.write_bytes(b"mv: rename HardHardness to theGrid: No such file or directory\n")
            end = tty_path.stat().st_size
            out_path = pathlib.Path(tmpdir) / "session.out.tsv"
            out_path.write_text(
                "\t".join(["1700000001", "0", tmpdir, "mv HardHardness theGrid", str(tty_path), "0", str(end)])
                + "\n"
            )
            os.environ["TERMTAB_OUTPUT_LAST"] = str(out_path)
            ev = inline_ai.find_retry_context("mv ", tmpdir)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["cmd"], "mv HardHardness theGrid")
        self.assertIn("No such file or directory", ev["output"])

    def test_find_retry_context_respects_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tty_path = pathlib.Path(tmpdir) / "s.tty"
            tty_path.write_bytes(b"err\n")
            end = tty_path.stat().st_size
            out_path = pathlib.Path(tmpdir) / "s.out.tsv"
            out_path.write_text(
                "\t".join(["1700000001", "1", "/tmp/a", "gti status", str(tty_path), "0", str(end)]) + "\n"
            )
            os.environ["TERMTAB_OUTPUT_LAST"] = str(out_path)
            self.assertIsNone(inline_ai.find_retry_context("g", "/tmp/b"))

    def test_last_terminal_event_reads_typescript_slice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tty_path = pathlib.Path(tmpdir) / "session.tty"
            tty_path.write_bytes(b"prelude\n\x1b[31mzsh: command not found: gti\x1b[0m\r\n")
            last_path = pathlib.Path(tmpdir) / "session.last.tsv"
            end = tty_path.stat().st_size
            last_path.write_text(
                "\t".join(["1700000000", "127", "/tmp", "gti status", str(tty_path), "8", str(end)]) + "\n"
            )
            os.environ["TERMTAB_OUTPUT_LAST"] = str(last_path)
            event = inline_ai.last_terminal_event()
        self.assertIsNotNone(event)
        self.assertEqual(event["exit"], 127)
        self.assertEqual(event["cmd"], "gti status")
        self.assertIn("command not found: gti", event["output"])
        self.assertNotIn("\x1b", event["output"])
        self.assertNotIn("prelude", event["output"])

    def test_is_retry_intent(self):
        self.assertTrue(inline_ai.is_retry_intent("g", "gti status"))
        self.assertTrue(inline_ai.is_retry_intent("gti", "gti status"))
        self.assertTrue(inline_ai.is_retry_intent("git st", "gti status"))
        self.assertTrue(inline_ai.is_retry_intent("git status --short", "git stauts --short"))
        self.assertFalse(inline_ai.is_retry_intent("", "gti status"))
        self.assertFalse(inline_ai.is_retry_intent("ls", "gti status"))
        self.assertFalse(inline_ai.is_retry_intent("mv OtherThing target", "mv HardHardness theGrid"))

    def test_detect_path_correction_for_missing_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / "HardHardiness").mkdir()
            suggestion = inline_ai.detect_correction(
                "mv ./HardHardness theGrid",
                "mv: rename ./HardHardness to theGrid: No such file or directory\n",
                cwd=tmpdir,
            )
        self.assertEqual(suggestion, "mv ./HardHardiness theGrid")

    def test_detect_path_correction_quotes_spaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / "Hard Hardness").mkdir()
            suggestion = inline_ai.detect_correction(
                "mv HardHardness theGrid",
                "mv: rename HardHardness to theGrid: No such file or directory\n",
                cwd=tmpdir,
            )
        self.assertEqual(suggestion, "mv 'Hard Hardness' theGrid")

    def test_detect_path_correction_skips_when_no_close_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / "totally-different").mkdir()
            suggestion = inline_ai.detect_correction(
                "mv HardHardness theGrid",
                "mv: rename HardHardness to theGrid: No such file or directory\n",
                cwd=tmpdir,
            )
        self.assertEqual(suggestion, "")

    def test_detect_illegal_option_correction_drops_bad_flag(self):
        output = (
            "mv: illegal option -- r\n"
            "usage: mv [-f | -i | -n] [-hv] source target\n"
            "       mv [-f | -i | -n] [-v] source ... directory\n"
        )
        fix = inline_ai.detect_correction(
            "mv -r qwen3.5-local ~/Projects",
            output,
        )
        self.assertEqual(fix, "mv qwen3.5-local ~/Projects")

    def test_detect_illegal_option_correction_handles_clustered_short_flags(self):
        output = "tar: invalid option -- z\n"
        fix = inline_ai.detect_correction("tar -xzvf archive.tar", output)
        self.assertEqual(fix, "tar -xvf archive.tar")

    def test_detect_illegal_option_correction_handles_long_option(self):
        output = "grep: unrecognized option '--foo'\n"
        fix = inline_ai.detect_correction("grep --foo --color pattern file", output)
        self.assertEqual(fix, "grep --color pattern file")

    def test_best_history_suggestion_skips_excluded(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as history:
            history.write(": 1:0;mv ./HardHardness theGrid\n")
            history.write(": 2:0;mv ./HardHardness theGrid\n")
            history.write(": 3:0;mv ./HardHardiness theGrid\n")
            histfile = history.name
        os.environ["HISTFILE"] = histfile
        try:
            suggestion = inline_ai.best_history_suggestion(
                "mv ",
                {
                    "enabled": True,
                    "direct_match_min_chars": 3,
                    "max_entries": 100,
                    "max_bytes": 65536,
                    "top_matches": 8,
                    "max_suggestion_chars": 320,
                    "recency_weight": 100,
                    "frequency_weight": 8,
                },
                exclude="mv ./HardHardness theGrid",
            )
        finally:
            os.unlink(histfile)
        self.assertEqual(suggestion, "mv ./HardHardiness theGrid")

    def test_best_history_suggestion_skips_mv_when_source_no_longer_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = pathlib.Path(tmpdir)
            (cwd / "HouseHaul").mkdir()
            with tempfile.NamedTemporaryFile("w", delete=False) as history:
                history.write(": 1:0;mv HardHarness theGrid\n")
                history.write(": 2:0;mv HouseHaul newHome\n")
                histfile = history.name
            os.environ["HISTFILE"] = histfile
            try:
                suggestion = inline_ai.best_history_suggestion(
                    "mv H",
                    {
                        "enabled": True,
                        "direct_match_min_chars": 3,
                        "max_entries": 100,
                        "max_bytes": 65536,
                        "top_matches": 8,
                        "max_suggestion_chars": 320,
                        "recency_weight": 100,
                        "frequency_weight": 8,
                    },
                    cwd=str(cwd),
                )
            finally:
                os.unlink(histfile)
        self.assertEqual(suggestion, "mv HouseHaul newHome")


if __name__ == "__main__":
    unittest.main()
