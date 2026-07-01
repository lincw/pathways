"""Pluggable LLM CLI wrapper for all reasoning calls in the pipeline.

Backend is selected at runtime (agy / claude / gemini / codex / ollama) via
--cli or PW_LLM_CLI; see LLM_CLI_SPECS below. Robust patterns:
  - _find_cli: searches PATH + common per-user install roots (npm, homebrew, .local)
  - _run_process: Popen + poll loop (supports cancellation; no zombie processes)
  - _extract_json: balanced-bracket scanner, tolerates markdown fences and LLM preamble
  - call_llm_structured: Pydantic schema instruction + retry with field-level repair hints

The default backend (agy) needs no API key — it uses the local CLI subscription.
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

from scripts.config import LLM_TIMEOUT, LLM_CLI, OLLAMA_MODEL
from scripts.progress import spinner as _spinner

_MAX_TRIES = 3


# ---------------------------------------------------------------------------
# LLM CLI backends — how to invoke each supported CLI non-interactively.
# Each builder maps (executable_path, prompt) -> argv list returning plain text
# (or JSON) on stdout. Default backend is agy; switch with --cli / PW_LLM_CLI.
# ---------------------------------------------------------------------------

LLM_CLI_SPECS = {
    "agy":    lambda exe, p: [exe, "-p", p],
    "claude": lambda exe, p: [exe, "-p", p],                       # Claude Code print mode
    "gemini": lambda exe, p: [exe, "-p", p],
    "codex":  lambda exe, p: [exe, "exec", "--skip-git-repo-check", p],
    "ollama": lambda exe, p: [exe, "run", OLLAMA_MODEL, p],
}

_ACTIVE_CLI_OVERRIDE: str | None = None
_CLI_INFO_CACHE: dict | None = None


def set_llm_cli(name: str | None) -> None:
    """Override the configured LLM CLI at runtime (e.g. from --cli)."""
    global _ACTIVE_CLI_OVERRIDE, _CLI_INFO_CACHE
    if name:
        _ACTIVE_CLI_OVERRIDE = name.strip()
        _CLI_INFO_CACHE = None


def _active_cli_name() -> str:
    return _ACTIVE_CLI_OVERRIDE or LLM_CLI


def _resolve_cli() -> tuple[str, str | None, object]:
    """Return (name, executable_path_or_None, argv_builder) for the active CLI."""
    name = _active_cli_name()
    builder = LLM_CLI_SPECS.get(name, LLM_CLI_SPECS["agy"])
    exe = _find_cli(name, f"{name.upper()}_CLI_PATH")
    return name, exe, builder


def active_cli_info() -> dict:
    """Name/path/version of the active CLI — used for the report model note."""
    global _CLI_INFO_CACHE
    if _CLI_INFO_CACHE is not None:
        return _CLI_INFO_CACHE
    name, exe, _ = _resolve_cli()
    version = ""
    if exe:
        try:
            p = _run_process([exe, "--version"], env=os.environ.copy(), timeout=15)
            out = (p.stdout or "").strip()
            version = out.splitlines()[0] if out else ""
        except Exception:
            version = ""
    _CLI_INFO_CACHE = {"name": name, "path": exe or "", "version": version}
    return _CLI_INFO_CACHE


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
                f"LLM CLI timed out (>{timeout}s): {(stderr or stdout or '').strip()[:300]}"
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

def call_llm(prompt: str, timeout: int = LLM_TIMEOUT, desc: str | None = None) -> str:
    """Call the active LLM CLI with a prompt and return its text response.

    Backend is selected by --cli / PW_LLM_CLI (default: agy). Pass ``desc`` to
    show an animated spinner while the CLI runs.
    """
    name, exe, build_argv = _resolve_cli()
    if not exe:
        raise RuntimeError(
            f"LLM CLI '{name}' not found. Ensure it is on PATH, pick another with "
            f"--cli, or set {name.upper()}_CLI_PATH=/path/to/{name}."
        )
    with _spinner(desc):
        proc = _run_process(
            build_argv(exe, _safe_arg(prompt)),
            env=os.environ.copy(),
            timeout=timeout,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"{name} CLI failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}")
    return (proc.stdout or "").strip()


def call_llm_structured(
    prompt: str,
    schema: Type[BaseModel],
    timeout: int = LLM_TIMEOUT,
    max_tries: int = _MAX_TRIES,
    desc: str | None = None,
) -> BaseModel:
    """Call the LLM with a JSON Schema instruction and validate against a Pydantic model.

    Retries up to max_tries times, feeding field-level Pydantic errors back into
    the prompt so the model can self-correct.
    """
    base_prompt = prompt.rstrip() + "\n\n" + _schema_instruction(schema)
    current_prompt = base_prompt
    last_err: Exception | None = None
    last_raw = ""

    for _ in range(max(1, max_tries)):
        raw = call_llm(current_prompt, timeout=timeout, desc=desc)
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
        f"LLM structured output failed after {max_tries} tries: {last_err}; "
        f"last output: {last_raw.strip()[:200]!r}"
    ) from last_err


def call_llm_json(prompt: str, timeout: int = LLM_TIMEOUT) -> dict | list:
    """Call the LLM and return a plain dict/list (no Pydantic schema validation).

    Use call_llm_structured() instead when you have a Pydantic model — it retries
    with field-level repair hints and is more reliable.
    """
    json_prompt = (
        prompt.rstrip()
        + "\n\nRespond with ONLY valid JSON — no markdown fences, no explanation text."
    )
    raw = call_llm(json_prompt, timeout=timeout)
    json_text = _extract_json(raw)
    return json.loads(json_text)
