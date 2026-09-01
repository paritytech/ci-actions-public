#!/usr/bin/env python3
"""Unit tests for review.py. Stdlib only -- runners have no pytest."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import review


def fetcher_from(*responses):
    """Build a fetcher returning the given (status, body) pairs in order."""
    queue = list(responses)
    calls = []

    def fetcher(url, headers):
        calls.append((url, headers))
        return queue.pop(0)

    fetcher.calls = calls
    return fetcher


class TruncateBytesTest(unittest.TestCase):
    def test_under_limit_is_untouched(self):
        self.assertEqual(review.truncate_bytes(b"abc", 10), ("abc", False))

    def test_over_limit_is_cut(self):
        self.assertEqual(review.truncate_bytes(b"abcdef", 3), ("abc", True))

    def test_zero_limit_disables_truncation(self):
        self.assertEqual(review.truncate_bytes(b"abcdef", 0), ("abcdef", False))

    def test_split_multibyte_char_does_not_raise(self):
        # "ä" is two bytes; cutting between them must not blow up.
        text, truncated = review.truncate_bytes("aä".encode("utf-8"), 2)
        self.assertTrue(truncated)
        self.assertEqual(text, "a")


class FetchDiffTest(unittest.TestCase):
    def test_full_diff(self):
        fetcher = fetcher_from((200, b"diff --git a/x b/x\n"))
        text, mode = review.fetch_diff("o/r", 1, "t", 1000, fetcher=fetcher)
        self.assertEqual(mode, review.MODE_FULL)
        self.assertIn("diff --git", text)
        self.assertEqual(
            fetcher.calls[0][1]["Accept"], "application/vnd.github.v3.diff"
        )

    def test_oversized_diff_is_truncated(self):
        fetcher = fetcher_from((200, b"x" * 50))
        _, mode = review.fetch_diff("o/r", 1, "t", 10, fetcher=fetcher)
        self.assertEqual(mode, review.MODE_TRUNCATED)

    def test_406_falls_back_to_file_list(self):
        files = [
            {"status": "modified", "additions": 3, "deletions": 1, "filename": "a.py"}
        ]
        fetcher = fetcher_from(
            (review.HTTP_TOO_LARGE, b""),
            (200, json.dumps(files).encode()),
        )
        text, mode = review.fetch_diff("o/r", 1, "t", 1000, fetcher=fetcher)
        self.assertEqual(mode, review.MODE_FILE_LIST)
        self.assertEqual(text, "modified\t+3/-1\ta.py")

    def test_other_status_raises(self):
        fetcher = fetcher_from((404, b""))
        with self.assertRaises(review.ReviewError):
            review.fetch_diff("o/r", 1, "t", 1000, fetcher=fetcher)

    def test_empty_diff_raises(self):
        fetcher = fetcher_from((200, b"   \n"))
        with self.assertRaises(review.ReviewError):
            review.fetch_diff("o/r", 1, "t", 1000, fetcher=fetcher)


class FetchFileListTest(unittest.TestCase):
    def test_pages_until_short_batch(self):
        page1 = [{"filename": f"f{i}.py"} for i in range(2)]
        page2 = [{"filename": "last.py"}]
        fetcher = fetcher_from(
            (200, json.dumps(page1).encode()),
            (200, json.dumps(page2).encode()),
        )
        text = review.fetch_file_list("o/r", 1, "t", fetcher=fetcher, per_page=2)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertEqual(len(fetcher.calls), 2)

    def test_non_200_raises(self):
        fetcher = fetcher_from((500, b""))
        with self.assertRaises(review.ReviewError):
            review.fetch_file_list("o/r", 1, "t", fetcher=fetcher)


class RenderPromptTest(unittest.TestCase):
    def values(self, **overrides):
        base = {token: "" for token in review.PROMPT_TOKENS}
        base.update(overrides)
        return base

    def test_substitutes_every_token(self):
        template = "".join(f"{{{{{t}}}}}" for t in review.PROMPT_TOKENS)
        values = self.values(**{t: t.lower() for t in review.PROMPT_TOKENS})
        rendered = review.render_prompt(template, values)
        self.assertEqual(rendered, "".join(t.lower() for t in review.PROMPT_TOKENS))

    def test_single_pass_does_not_expand_injected_token(self):
        # A PR title of "{{DIFF}}" must stay literal, not pull in the diff.
        rendered = review.render_prompt(
            "T:{{TITLE}} D:{{DIFF}}",
            self.values(TITLE="{{DIFF}}", DIFF="SECRET"),
        )
        self.assertEqual(rendered, "T:{{DIFF}} D:SECRET")

    def test_braces_in_values_are_literal(self):
        rendered = review.render_prompt("{{DIFF}}", self.values(DIFF="fn() { x }"))
        self.assertEqual(rendered, "fn() { x }")

    def test_backslash_in_value_is_literal(self):
        # re.sub with a plain string would treat \g as a group reference.
        rendered = review.render_prompt("{{DIFF}}", self.values(DIFF=r"a\g<0>b\1"))
        self.assertEqual(rendered, r"a\g<0>b\1")

    def test_missing_value_raises(self):
        with self.assertRaises(review.ReviewError):
            review.render_prompt("{{DIFF}}", {"DIFF": "x"})

    def test_unknown_placeholder_is_left_alone(self):
        rendered = review.render_prompt("{{NOPE}}", self.values())
        self.assertEqual(rendered, "{{NOPE}}")


class VertexUrlTest(unittest.TestCase):
    def test_global_is_unprefixed(self):
        self.assertEqual(
            review.vertex_url("p", "global", "m"),
            "https://aiplatform.googleapis.com/v1/projects/p/locations/global"
            "/publishers/google/models/m:generateContent",
        )

    def test_region_is_prefixed(self):
        self.assertTrue(
            review.vertex_url("p", "europe-west3", "m").startswith(
                "https://europe-west3-aiplatform.googleapis.com/"
            )
        )

    def test_missing_project_raises(self):
        with self.assertRaises(review.ReviewError):
            review.vertex_url("", "global", "m")


class ExtractReviewTest(unittest.TestCase):
    def response(self, parts, finish_reason="STOP"):
        return {
            "candidates": [
                {"finishReason": finish_reason, "content": {"parts": parts}}
            ]
        }

    def test_joins_text_parts(self):
        text, reason = review.extract_review(
            self.response([{"text": "a"}, {"text": "b"}])
        )
        self.assertEqual((text, reason), ("ab", "STOP"))

    def test_drops_thinking_parts(self):
        text, _ = review.extract_review(
            self.response([{"text": "hmm", "thought": True}, {"text": "real"}])
        )
        self.assertEqual(text, "real")

    def test_no_candidates_reports_block_reason(self):
        with self.assertRaises(review.ReviewError) as ctx:
            review.extract_review({"promptFeedback": {"blockReason": "SAFETY"}})
        self.assertIn("SAFETY", str(ctx.exception))

    def test_thinking_only_response_raises_with_finish_reason(self):
        with self.assertRaises(review.ReviewError) as ctx:
            review.extract_review(
                self.response([{"text": "hmm", "thought": True}], "MAX_TOKENS")
            )
        self.assertIn("MAX_TOKENS", str(ctx.exception))

    def test_non_string_text_is_ignored(self):
        with self.assertRaises(review.ReviewError):
            review.extract_review(self.response([{"text": None}]))


class NeedsRetryTest(unittest.TestCase):
    def test_max_tokens_always_retries(self):
        self.assertTrue(review.needs_retry("x" * 5000, "MAX_TOKENS", 220))

    def test_short_review_retries(self):
        self.assertTrue(review.needs_retry("short", "STOP", 220))

    def test_long_review_does_not_retry(self):
        self.assertFalse(review.needs_retry("x" * 300, "STOP", 220))

    def test_zero_min_chars_disables_length_check(self):
        self.assertFalse(review.needs_retry("", "STOP", 0))


class DiffNoteTest(unittest.TestCase):
    def test_full_mode_has_no_note(self):
        self.assertEqual(review.diff_note(review.MODE_FULL), "")

    def test_degraded_modes_have_notes(self):
        self.assertIn("truncated", review.diff_note(review.MODE_TRUNCATED))
        self.assertIn("300 files", review.diff_note(review.MODE_FILE_LIST))


class BuildPayloadTest(unittest.TestCase):
    def test_shape(self):
        payload = review.build_payload("p", 0.2, 4096)
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "p")
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 4096)
        self.assertEqual(payload["generationConfig"]["temperature"], 0.2)

    def test_response_is_schema_constrained(self):
        config = review.build_payload("p", None, 4096)["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(
            config["responseSchema"]["properties"]["groups"]["items"]["properties"][
                "category"
            ]["enum"],
            list(review.CATEGORIES),
        )

    def test_seed_is_sent_when_given(self):
        config = review.build_payload("p", None, 4096, seed=7)["generationConfig"]
        self.assertEqual(config["seed"], 7)

    def test_seed_is_omitted_when_none(self):
        config = review.build_payload("p", None, 4096)["generationConfig"]
        self.assertNotIn("seed", config)

    def test_seed_zero_is_sent(self):
        # 0 is a valid seed and must not be dropped by a falsy check.
        config = review.build_payload("p", None, 4096, seed=0)["generationConfig"]
        self.assertEqual(config["seed"], 0)


class RenderReviewTest(unittest.TestCase):
    def finding(self, title="t", impact="i", fix="f"):
        return {"title": title, "impact": impact, "fix": fix}

    def test_renders_summary_and_categories_in_fixed_order(self):
        data = {
            "summary": ["one", "two"],
            "groups": [
                {"category": "Maintainability", "findings": [self.finding("m")]},
                {"category": "Correctness", "findings": [self.finding("c")]},
            ],
        }
        text = review.render_review(data)
        self.assertLess(text.index("### Correctness"), text.index("### Maintainability"))
        self.assertIn("- one", text)
        self.assertIn("- **c**", text)
        self.assertIn("  - Impact: i", text)
        self.assertIn("  - Fix: f", text)

    def test_unknown_category_is_dropped(self):
        data = {
            "summary": ["s"],
            "groups": [{"category": "Reliability", "findings": [self.finding()]}],
        }
        text = review.render_review(data)
        self.assertNotIn("Reliability", text)

    def test_empty_category_gets_no_heading(self):
        data = {"summary": ["s"], "groups": [{"category": "Tests", "findings": []}]}
        self.assertNotIn("### Tests", review.render_review(data))

    def test_caps_are_enforced(self):
        many = [self.finding(f"t{i}") for i in range(9)]
        data = {
            "summary": [f"s{i}" for i in range(9)],
            "groups": [{"category": "Security", "findings": many}],
        }
        text = review.render_review(data)
        self.assertEqual(text.count("\n- s"), review.MAX_SUMMARY_POINTS)
        self.assertEqual(text.count("- **t"), review.MAX_FINDINGS_PER_CATEGORY)

    def test_two_groups_of_one_category_merge(self):
        data = {
            "summary": ["s"],
            "groups": [
                {"category": "Tests", "findings": [self.finding("a")]},
                {"category": "Tests", "findings": [self.finding("b")]},
            ],
        }
        text = review.render_review(data)
        self.assertEqual(text.count("### Tests"), 1)
        self.assertIn("- **a**", text)
        self.assertIn("- **b**", text)

    def test_missing_impact_and_fix_are_skipped(self):
        data = {
            "summary": ["s"],
            "groups": [{"category": "Correctness", "findings": [{"title": "bare"}]}],
        }
        text = review.render_review(data)
        self.assertIn("- **bare**", text)
        self.assertNotIn("Impact:", text)

    def test_empty_review_raises(self):
        with self.assertRaises(review.ReviewError):
            review.render_review({"summary": [], "groups": []})

    def test_non_object_raises(self):
        with self.assertRaises(review.ReviewError):
            review.render_review(["nope"])

    def test_bad_field_types_raise(self):
        with self.assertRaises(review.ReviewError):
            review.render_review({"summary": "s", "groups": []})

    def test_none_temperature_is_omitted(self):
        # Gemini 3 wants its default 1.0; sending nothing is how we get it.
        config = review.build_payload("p", None, 4096)["generationConfig"]
        self.assertNotIn("temperature", config)

    def test_thinking_level_is_nested_under_thinking_config(self):
        config = review.build_payload("p", None, 4096, "low")["generationConfig"]
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "low"})

    def test_empty_thinking_level_is_omitted(self):
        config = review.build_payload("p", None, 4096, "")["generationConfig"]
        self.assertNotIn("thinkingConfig", config)

    def test_bogus_thinking_level_raises(self):
        with self.assertRaises(review.ReviewError):
            review.build_payload("p", None, 4096, "turbo")


class CliTest(unittest.TestCase):
    """The CLI layer must turn bad input into exit 22, never a traceback."""

    def run_extract(self, body, min_chars=0):
        """Return (exit_code, review_text_or_None)."""
        with tempfile.TemporaryDirectory() as tmp:
            response = os.path.join(tmp, "resp.json")
            out = os.path.join(tmp, "review.txt")
            with open(response, "w", encoding="utf-8") as handle:
                handle.write(body)
            code = review.main(
                ["extract-review", "--response", response, "--out", out,
                 "--min-chars", str(min_chars)]
            )
            text = None
            if os.path.exists(out):
                with open(out, encoding="utf-8") as handle:
                    text = handle.read()
            return code, text

    def wrap(self, candidate_text, finish_reason="STOP"):
        return json.dumps(
            {"candidates": [{"finishReason": finish_reason,
                             "content": {"parts": [{"text": candidate_text}]}}]}
        )

    def review_json(self):
        return json.dumps(
            {
                "summary": ["does a thing"],
                "groups": [
                    {
                        "category": "Correctness",
                        "findings": [
                            {"title": "t", "impact": "i", "fix": "f"}
                        ],
                    }
                ],
            }
        )

    def test_non_json_response_exits_22(self):
        code, _ = self.run_extract("<html>502 Bad Gateway</html>")
        self.assertEqual(code, 22)

    def test_json_without_candidates_exits_22(self):
        code, _ = self.run_extract("{}")
        self.assertEqual(code, 22)

    def test_valid_response_renders_markdown(self):
        code, text = self.run_extract(self.wrap(self.review_json()))
        self.assertEqual(code, 0)
        self.assertIn("### Summary", text)
        self.assertIn("### Correctness", text)

    def test_truncated_json_with_max_tokens_asks_for_retry(self):
        # A cut-off object is what a budget exhaustion looks like now.
        body = self.wrap('{"summary": ["a"], "grou', "MAX_TOKENS")
        code, text = self.run_extract(body)
        self.assertEqual(code, 0)
        self.assertIsNone(text)

    def test_unparseable_json_without_max_tokens_exits_22(self):
        code, _ = self.run_extract(self.wrap("this is prose, not json", "STOP"))
        self.assertEqual(code, 22)


if __name__ == "__main__":
    unittest.main()
