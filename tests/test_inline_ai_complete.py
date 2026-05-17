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

    def tearDown(self):
        if self._old_histfile is None:
            os.environ.pop("HISTFILE", None)
        else:
            os.environ["HISTFILE"] = self._old_histfile
        if self._old_session_log is None:
            os.environ.pop("TERMTAB_SESSION_LOG", None)
        else:
            os.environ["TERMTAB_SESSION_LOG"] = self._old_session_log

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


if __name__ == "__main__":
    unittest.main()
