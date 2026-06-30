"""Agy CLI wrapper for all LLM reasoning calls in the LPS pipeline.

Ported from eu_jobsmith/app/llm_cli.py — same robust patterns:
  - _find_cli: searches PATH + common per-user install roots (npm, homebrew, .local)
  - _run_process: Popen + poll loop (supports cancellation; no zombie processes)
  - _extract_json: balanced-bracket scanner, tolerates markdown fences and LLM preamble
  - call_agy_structured: Pydantic schema instruction + retry with field-level repair hints

No API key required — uses the local agy CLI subscription.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Type

from pydantic import BaseModel, ValidationError

from scripts.config import AGY_TIMEOUT

_MAX_TRIES = 3


# ---------------------------------------------------------------------------
# CLI discovery
# ---------------------------------------------------------------------------

def _cli_search_paths(name: str) -> list[Path]:
    """Common per-user CLI install locations that shutil.which() misses in GUI/packaged apps."""
    home = Path.home()
    if os.name == "nt":
        names = [f"{name}.cmd", f"{name}.exe", name]
        roots = [
            Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "npm",
            Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))) / "npm",
            home / ".local" / "bin",
            home / ".bun" / "bin",
        ]
    else:
        names = [name]
        roots = [
            home / ".local" / "bin",
            home / ".npm-global" / "bin",
            home / ".bun" / "bin",
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            Path("/usr/bin"),
        ]
    paths: list[Path] = []
    for root in roots:
        for n in names:
            paths.append(root / n)
    return paths


def _find_cli(name: str, env_var: str) -> str | None:
    """env var override → PATH (shutil.which) → common install roots."""
    override = (os.environ.get(env_var) or "").strip()
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    for path in _cli_search_paths(name):
        try:
            if path.exists():
                return str(path)
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

def _safe_arg(text: str) -> str:
    """Strip NUL bytes — subprocess raises ValueError on embedded \\x00."""
    return (text or "").replace("\x00", "").lstrip()


def _run_process(args: list[str], *, env: dict[str, str], timeout: int) -> SimpleNamespace:
    """Popen + poll loop.  Raises RuntimeError on timeout or non-zero exit."""
    import subprocess

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        creationflags=no_window,
    )
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise RuntimeError(
                f"agy CLI timed out (>{timeout}s): {(stderr or stdout or '').strip()[:300]}"
            )
        try:
            stdout, stderr = proc.communicate(timeout=min(0.2, remaining))
            return SimpleNamespace(returncode=proc.returncode, stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired:
            continue


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> str:
    """Extract the first complete JSON object from LLM output.

    Strips markdown fences, then uses balanced-bracket scanning that ignores
    bracket characters inside strings — handles nested objects correctly.
    """
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    if start == -1:
        return text
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            esc = (not esc) and ch == "\\"
            if not esc and ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


# ---------------------------------------------------------------------------
# Structured output: schema instruction + retry with repair hints
# ---------------------------------------------------------------------------

def _schema_instruction(schema: Type[BaseModel]) -> str:
    return (
        "Respond with ONLY a single JSON object matching the schema below. "
        "No markdown fences, no explanation text:\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )


def _repair_hint(exc: ValidationError) -> str:
    problems = []
    for e in exc.errors()[:8]:
        loc = ".".join(str(p) for p in e.get("loc", ())) or "(root)"
        problems.append(f"- field `{loc}`: {e.get('msg')}")
    return (
        "Previous output did not match the schema. Fix these fields and output ONLY valid JSON:\n"
        + "\n".join(problems)
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_agy(prompt: str, timeout: int = AGY_TIMEOUT) -> str:
    """Call `agy -p prompt` and return the text response."""
    exe = _find_cli("agy", "AGY_CLI_PATH")
    if not exe:
        raise RuntimeError(
            "agy CLI not found. Install antigravity and ensure it is on PATH, "
            "or set AGY_CLI_PATH=/path/to/agy."
        )
    proc = _run_process(
        [exe, "-p", _safe_arg(prompt)],
        env=os.environ.copy(),
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"agy CLI failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}")
    return (proc.stdout or "").strip()


def call_agy_structured(
    prompt: str,
    schema: Type[BaseModel],
    timeout: int = AGY_TIMEOUT,
    max_tries: int = _MAX_TRIES,
) -> BaseModel:
    """Call agy with a JSON Schema instruction and validate against a Pydantic model.

    Retries up to max_tries times, feeding field-level Pydantic errors back into
    the prompt so the model can self-correct — same pattern as eu_jobsmith/llm_cli.py.
    """
    base_prompt = prompt.rstrip() + "\n\n" + _schema_instruction(schema)
    current_prompt = base_prompt
    last_err: Exception | None = None
    last_raw = ""

    for _ in range(max(1, max_tries)):
        raw = call_agy(current_prompt, timeout=timeout)
        last_raw = raw
        json_text = _extract_json(raw)
        try:
            payload = json.loads(json_text)
            return schema.model_validate(payload)
        except json.JSONDecodeError as exc:
            last_err = exc
            current_prompt = (
                base_prompt
                + "\n\nPrevious output was not valid JSON. Output ONLY a valid JSON object."
            )
        except ValidationError as exc:
            last_err = exc
            current_prompt = base_prompt + "\n\n" + _repair_hint(exc)

    raise RuntimeError(
        f"agy structured output failed after {max_tries} tries: {last_err}; "
        f"last output: {last_raw.strip()[:200]!r}"
    ) from last_err


def call_agy_json(prompt: str, timeout: int = AGY_TIMEOUT) -> dict | list:
    """Call agy and return a plain dict/list (no Pydantic schema validation).

    Use call_agy_structured() instead when you have a Pydantic model — it retries
    with field-level repair hints and is more reliable.
    """
    json_prompt = (
        prompt.rstrip()
        + "\n\nRespond with ONLY valid JSON — no markdown fences, no explanation text."
    )
    raw = call_agy(json_prompt, timeout=timeout)
    json_text = _extract_json(raw)
    return json.loads(json_text)
