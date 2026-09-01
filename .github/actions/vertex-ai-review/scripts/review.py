#!/usr/bin/env python3
"""Helpers for the vertex-ai-review composite action.

Subcommands:
  fetch-diff      Fetch the PR diff and metadata, falling back to a file list
                  on HTTP 406.
  build-request   Render the prompt template into a Vertex generateContent payload.
  extract-review  Pull the review text out of a Vertex response.

The action keeps every parsing decision in here rather than in shell so it can be
unit tested. Nothing in this module logs prompts, diffs, tokens, or response
bodies -- callers get short status lines only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Callable, Iterable

GITHUB_API = "https://api.github.com"

# Modes describing how complete the review input is.
MODE_FULL = "full"
MODE_TRUNCATED = "truncated"
MODE_FILE_LIST = "file-list"

# GitHub refuses to serialise a diff touching more than 300 files.
HTTP_TOO_LARGE = 406

PROMPT_TOKENS = ("REPOSITORY", "PR_NUMBER", "TITLE", "DESCRIPTION", "DIFF_NOTE", "DIFF")

_NOTE_TRUNCATED = (
    "NOTE: the diff below was truncated because it exceeded the size limit. "
    "You are not seeing the whole change. Say so in your summary and scope your "
    "findings to what is visible."
)
_NOTE_FILE_LIST = (
    "NOTE: this pull request changes more than 300 files, so GitHub refused to "
    "serve a diff for it. Instead of a diff you are given the list of changed "
    "files with per-file line counts. Do not invent code you cannot see -- "
    "comment on the shape and risk of the change and say that a full review "
    "needs a local checkout."
)


class ReviewError(RuntimeError):
    """Raised for conditions the action should fail on, with a safe message."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

Fetcher = Callable[[str, dict], "tuple[int, bytes]"]


def _http_get(url: str, headers: dict) -> "tuple[int, bytes]":
    """GET a URL, returning (status, body). HTTP errors are returned, not raised."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:  # 4xx/5xx carry a body we want
        return error.code, error.read()


def _gh_headers(token: str, accept: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "paritytech-vertex-ai-review",
    }


# --------------------------------------------------------------------------- #
# Diff fetching
# --------------------------------------------------------------------------- #


def truncate_bytes(raw: bytes, max_bytes: int) -> "tuple[str, bool]":
    """Decode raw bytes, cutting to max_bytes. Returns (text, was_truncated)."""
    if max_bytes > 0 and len(raw) > max_bytes:
        return raw[:max_bytes].decode("utf-8", errors="ignore"), True
    return raw.decode("utf-8", errors="ignore"), False


def format_file_list(files: Iterable[dict]) -> str:
    """Render /pulls/{n}/files entries as a compact per-file summary."""
    lines = []
    for entry in files:
        lines.append(
            "{status}\t+{additions}/-{deletions}\t{filename}".format(
                status=entry.get("status", "?"),
                additions=entry.get("additions", 0),
                deletions=entry.get("deletions", 0),
                filename=entry.get("filename", "?"),
            )
        )
    return "\n".join(lines)


def fetch_file_list(
    repo: str,
    pr_number: int,
    token: str,
    fetcher: Fetcher = _http_get,
    per_page: int = 100,
    max_pages: int = 30,
) -> str:
    """Page through the PR's changed files. Used when the diff is too large."""
    collected: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
            f"?per_page={per_page}&page={page}"
        )
        status, body = fetcher(url, _gh_headers(token, "application/vnd.github+json"))
        if status != 200:
            raise ReviewError(f"listing PR files failed with HTTP {status}")
        batch = json.loads(body.decode("utf-8"))
        collected.extend(batch)
        if len(batch) < per_page:
            break
    return format_file_list(collected)


def fetch_pr(
    repo: str,
    pr_number: int,
    token: str,
    fetcher: Fetcher = _http_get,
) -> dict:
    """Fetch the PR object. Only title and body are used downstream."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    status, body = fetcher(url, _gh_headers(token, "application/vnd.github+json"))
    if status != 200:
        raise ReviewError(f"fetching PR metadata failed with HTTP {status}")
    return json.loads(body.decode("utf-8"))


def fetch_diff(
    repo: str,
    pr_number: int,
    token: str,
    max_bytes: int,
    fetcher: Fetcher = _http_get,
) -> "tuple[str, str]":
    """Fetch review input for a PR. Returns (text, mode).

    A >300-file PR gets HTTP 406 from the diff endpoint. Rather than failing the
    run we degrade to the file list, which has no such limit.
    """
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    status, body = fetcher(url, _gh_headers(token, "application/vnd.github.v3.diff"))

    if status == HTTP_TOO_LARGE:
        return fetch_file_list(repo, pr_number, token, fetcher=fetcher), MODE_FILE_LIST
    if status != 200:
        raise ReviewError(f"fetching PR diff failed with HTTP {status}")

    text, was_truncated = truncate_bytes(body, max_bytes)
    if not text.strip():
        raise ReviewError("PR diff was empty")
    return text, MODE_TRUNCATED if was_truncated else MODE_FULL


# --------------------------------------------------------------------------- #
# Prompt + payload
# --------------------------------------------------------------------------- #


def diff_note(mode: str) -> str:
    return {MODE_TRUNCATED: _NOTE_TRUNCATED, MODE_FILE_LIST: _NOTE_FILE_LIST}.get(mode, "")


def render_prompt(template: str, values: dict) -> str:
    """Substitute {{TOKEN}} placeholders in a single pass.

    Single pass matters: substituted values are attacker-controlled (PR title,
    body, diff), and a second pass would let a value containing "{{DIFF}}" be
    expanded. Braces in the diff are safe because this is not str.format.
    """
    missing = [token for token in PROMPT_TOKENS if token not in values]
    if missing:
        raise ReviewError(f"prompt values missing: {', '.join(missing)}")
    pattern = re.compile(r"\{\{(" + "|".join(PROMPT_TOKENS) + r")\}\}")
    return pattern.sub(lambda match: values[match.group(1)], template)


THINKING_LEVELS = ("low", "medium", "high")

# Categories the model may use, in the order they get rendered. The enum in the
# schema is what stops the model inventing a heading such as "Reliability".
CATEGORIES = ("Correctness", "Security", "Tests", "Maintainability")

MAX_SUMMARY_POINTS = 3
MAX_FINDINGS_PER_CATEGORY = 3

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "array",
            "maxItems": MAX_SUMMARY_POINTS,
            "items": {"type": "string"},
        },
        "groups": {
            "type": "array",
            "maxItems": len(CATEGORIES),
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "findings": {
                        "type": "array",
                        "maxItems": MAX_FINDINGS_PER_CATEGORY,
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "impact": {"type": "string"},
                                "fix": {"type": "string"},
                            },
                            "required": ["title", "impact", "fix"],
                        },
                    },
                },
                "required": ["category", "findings"],
            },
        },
    },
    "required": ["summary", "groups"],
}


def build_payload(
    prompt: str,
    temperature: "float | None",
    max_output_tokens: int,
    thinking_level: str = "",
    seed: "int | None" = None,
) -> dict:
    """Build a generateContent payload.

    The response is constrained to RESPONSE_SCHEMA. That fixes the categories,
    the item counts and the field names, so run-to-run drift is limited to which
    findings the model chooses. render_review turns the JSON into markdown, so
    bullet characters and heading levels never come from the model.

    temperature is omitted when None. Google's guidance for Gemini 3 models is
    to leave it at the default 1.0 -- lowering it risks looping and degraded
    reasoning -- so the action sends nothing rather than a value.

    seed asks for best-effort reproducibility. With the same prompt and the same
    parameters the model tries to return the same answer. Google documents this
    as best effort rather than a guarantee.

    thinking_level, when set, caps how much of the output budget the model may
    spend on thinking. Thinking tokens count against maxOutputTokens and are
    then discarded by extract_review, so an unbounded level on a small budget
    can consume the whole allowance and return no review text.
    """
    generation_config: dict = {
        "maxOutputTokens": max_output_tokens,
        "responseMimeType": "application/json",
        "responseSchema": RESPONSE_SCHEMA,
    }
    if temperature is not None:
        generation_config["temperature"] = temperature
    if seed is not None:
        generation_config["seed"] = seed
    if thinking_level:
        if thinking_level not in THINKING_LEVELS:
            raise ReviewError(
                f"thinking level must be one of {', '.join(THINKING_LEVELS)}"
            )
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }


def render_review(data: dict) -> str:
    """Render the schema-constrained JSON as markdown.

    Every character of structure comes from here. The model supplies prose for
    the leaves alone, which is what keeps the shape identical between runs.
    """
    if not isinstance(data, dict):
        raise ReviewError("review JSON was not an object")

    lines: list[str] = ["### Summary", ""]
    summary = data.get("summary") or []
    if not isinstance(summary, list):
        raise ReviewError("review JSON field 'summary' was not a list")
    for point in summary[:MAX_SUMMARY_POINTS]:
        lines.append(f"- {str(point).strip()}")

    groups = data.get("groups") or []
    if not isinstance(groups, list):
        raise ReviewError("review JSON field 'groups' was not a list")

    by_category = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        category = group.get("category")
        if category in CATEGORIES:
            by_category.setdefault(category, []).extend(group.get("findings") or [])

    # Fixed order, so two runs that find the same things read the same way.
    for category in CATEGORIES:
        findings = [f for f in by_category.get(category, []) if isinstance(f, dict)]
        if not findings:
            continue
        lines.extend(["", f"### {category}", ""])
        for finding in findings[:MAX_FINDINGS_PER_CATEGORY]:
            title = str(finding.get("title", "")).strip()
            impact = str(finding.get("impact", "")).strip()
            fix = str(finding.get("fix", "")).strip()
            lines.append(f"- **{title}**")
            if impact:
                lines.append(f"  - Impact: {impact}")
            if fix:
                lines.append(f"  - Fix: {fix}")

    text = "\n".join(lines).strip()
    if len(lines) <= 2:
        raise ReviewError("review JSON held no summary and no findings")
    return text


def vertex_url(project: str, location: str, model: str) -> str:
    """Build the generateContent endpoint.

    The multi-region "global" endpoint is unprefixed; every other location gets a
    "<location>-" host prefix.
    """
    if not project:
        raise ReviewError("vertex project is required")
    host = "aiplatform.googleapis.com"
    if location != "global":
        host = f"{location}-{host}"
    return (
        f"https://{host}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}:generateContent"
    )


# --------------------------------------------------------------------------- #
# Response handling
# --------------------------------------------------------------------------- #


def extract_review(response: dict) -> "tuple[str, str]":
    """Return (review_text, finish_reason) from a generateContent response.

    Thinking parts are dropped: they carry `thought: true` and are not review
    content.
    """
    candidates = response.get("candidates")
    if not candidates:
        feedback = response.get("promptFeedback") or {}
        reason = feedback.get("blockReason") or response.get("error", {}).get("status")
        raise ReviewError(
            "Vertex returned no candidates"
            + (f" (blockReason={reason})" if reason else "")
        )

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason", "")
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part.get("text"), str) and not part.get("thought")
    ).strip()

    if not text:
        raise ReviewError(
            f"Vertex returned no review text (finishReason={finish_reason or 'unknown'})"
        )
    return text, finish_reason


def needs_retry(text: str, finish_reason: str, min_chars: int) -> bool:
    """Whether to spend one more call on this review.

    MAX_TOKENS is the API telling us it truncated, which is a better signal than
    guessing from trailing punctuation.
    """
    if finish_reason == "MAX_TOKENS":
        return True
    return min_chars > 0 and len(text) < min_chars


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ReviewError(f"missing required environment variable {name}")
    return value


def _write_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")


def _cmd_fetch_diff(args: argparse.Namespace) -> int:
    repo = _require_env("GITHUB_REPOSITORY")
    pr_number = int(_require_env("PR_NUMBER"))
    token = _require_env("GH_TOKEN")

    # Title and body are attacker-controlled, so they go to a file rather than
    # through a step output or an env var, where quoting would be a hazard.
    pull_request = fetch_pr(repo, pr_number, token)
    meta = {
        "repository": repo,
        "pr_number": pr_number,
        "title": pull_request.get("title") or "",
        "body": pull_request.get("body") or "",
    }
    with open(args.meta, "w", encoding="utf-8") as handle:
        json.dump(meta, handle)

    text, mode = fetch_diff(
        repo=repo,
        pr_number=pr_number,
        token=token,
        max_bytes=int(os.environ.get("MAX_DIFF_BYTES", "1000000")),
    )
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text)
    _write_output("mode", mode)
    print(f"diff mode={mode} chars={len(text)}")
    return 0


def _cmd_build_request(args: argparse.Namespace) -> int:
    with open(args.prompt, encoding="utf-8") as handle:
        template = handle.read()
    with open(args.diff, encoding="utf-8") as handle:
        diff = handle.read()
    with open(args.meta, encoding="utf-8") as handle:
        meta = json.load(handle)

    prompt = render_prompt(
        template,
        {
            "REPOSITORY": str(meta.get("repository", "")),
            "PR_NUMBER": str(meta.get("pr_number", "")),
            "TITLE": str(meta.get("title", "")),
            "DESCRIPTION": str(meta.get("body", "")),
            "DIFF_NOTE": diff_note(args.mode),
            "DIFF": diff,
        },
    )
    raw_temperature = os.environ.get("TEMPERATURE", "").strip()
    raw_seed = os.environ.get("SEED", "").strip()
    payload = build_payload(
        prompt=prompt,
        temperature=float(raw_temperature) if raw_temperature else None,
        max_output_tokens=args.max_output_tokens,
        thinking_level=os.environ.get("THINKING_LEVEL", "").strip().lower(),
        seed=int(raw_seed) if raw_seed else None,
    )
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    print(f"request built prompt_chars={len(prompt)} max_output_tokens={args.max_output_tokens}")
    return 0


def _cmd_extract_review(args: argparse.Namespace) -> int:
    with open(args.response, encoding="utf-8") as handle:
        try:
            response = json.load(handle)
        except json.JSONDecodeError as error:
            # Only the position is reported -- the body may hold PR content.
            raise ReviewError(
                f"Vertex response was not JSON (at char {error.pos})"
            ) from None
    text, finish_reason = extract_review(response)

    # The model answers in JSON now, so a budget cut leaves the object
    # unterminated. That is retryable, and the next attempt doubles the budget.
    # A parse failure with any other finishReason means the model returned
    # something we cannot use, so fail instead of burning a second call.
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        if finish_reason == "MAX_TOKENS":
            print("retry")
            sys.stderr.write(
                f"review JSON truncated at char {error.pos}, finishReason=MAX_TOKENS\n"
            )
            return 0
        raise ReviewError(
            f"review was not valid JSON (at char {error.pos}, "
            f"finishReason={finish_reason or 'unknown'})"
        ) from None

    markdown = render_review(data)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(markdown + "\n")
    retry = needs_retry(markdown, finish_reason, args.min_chars)
    print("retry" if retry else "ok")
    sys.stderr.write(
        f"review chars={len(markdown)} finishReason={finish_reason or 'unknown'}\n"
    )
    return 0


def _cmd_vertex_url(args: argparse.Namespace) -> int:
    print(
        vertex_url(
            project=_require_env("VERTEX_PROJECT"),
            location=os.environ.get("VERTEX_LOCATION", "global"),
            model=_require_env("VERTEX_MODEL"),
        )
    )
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="review.py")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch-diff")
    fetch.add_argument("--out", default="pr.diff")
    fetch.add_argument("--meta", default="pr.meta.json")
    fetch.set_defaults(func=_cmd_fetch_diff)

    build = sub.add_parser("build-request")
    build.add_argument("--prompt", required=True)
    build.add_argument("--diff", default="pr.diff")
    build.add_argument("--meta", default="pr.meta.json")
    build.add_argument("--out", default="req.json")
    build.add_argument("--mode", default=MODE_FULL)
    build.add_argument("--max-output-tokens", type=int, default=4096)
    build.set_defaults(func=_cmd_build_request)

    extract = sub.add_parser("extract-review")
    extract.add_argument("--response", default="resp.json")
    extract.add_argument("--out", default="review.txt")
    extract.add_argument("--min-chars", type=int, default=220)
    extract.set_defaults(func=_cmd_extract_review)

    url = sub.add_parser("vertex-url")
    url.set_defaults(func=_cmd_vertex_url)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ReviewError as error:
        sys.stderr.write(f"error: {error}\n")
        return 22


if __name__ == "__main__":
    raise SystemExit(main())
