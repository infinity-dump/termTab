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
    def test_redacts_secret_like_values(self):
        api_key = "sk-" + "x" * 16
        aws_key = "AKIA" + "1" * 16
        text = f"export OPENROUTER_API_KEY={api_key} aws={aws_key}"
        redacted = inline_ai.redact(text)
        self.assertIn("<redacted>", redacted)
        self.assertNotIn(api_key, redacted)
        self.assertNotIn(aws_key, redacted)

    def test_history_weighting_prefers_recent_prefix_match(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as history:
            history.write(": 1:0;git status --short\n")
            history.write(": 2:0;git status --short\n")
            history.write(": 3:0;git stash list\n")
            history.write(": 4:0;git status --short --branch\n")
            histfile = history.name

        old_histfile = os.environ.get("HISTFILE")
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
            if old_histfile is None:
                os.environ.pop("HISTFILE", None)
            else:
                os.environ["HISTFILE"] = old_histfile
            os.unlink(histfile)

        self.assertEqual(suggestion, "git status --short --branch")

    def test_merge_completion_handles_suffix_or_full_line(self):
        self.assertEqual(inline_ai.merge_completion("git sta", "tus"), "git status")
        self.assertEqual(inline_ai.merge_completion("git sta", "git status"), "git status")


if __name__ == "__main__":
    unittest.main()
