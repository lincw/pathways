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

from scripts.config import LLM_TIMEOUT, LLM_CLI, LLM_MODEL, OLLAMA_MODEL
from scripts.progress import spinner as _spinner

_MAX_TRIES = 3


# ---------------------------------------------------------------------------
# LLM CLI backends — how to invoke each supported CLI non-interactively.
# Each builder maps (executable_path, prompt) -> argv list returning plain text
# (or JSON) on stdout. Default backend is agy; switch with --cli / PW_LLM_CLI.
# ---------------------------------------------------------------------------

# Each backend declares how to build its argv, and — only where verified — how to
# inject an explicit model so the recorded model note is *enforced*, not merely
# claimed. A backend without a `model_argv` cannot have its model selected/verified
# by the pipeline, so its report note reads "CLI default (not recorded)" and any
# --model value is ignored (with a warning) rather than stamped as a false label.
LLM_CLI_SPECS = {
    "agy": {
        "argv": lambda exe, p: [exe, "-p", p],
        "model_argv": lambda exe, p, m: [exe, "--model", m, "-p", p],  # verified: `agy --model`
    },
    "claude": {  # Claude Code print mode — model flag not verified here
        "argv": lambda exe, p: [exe, "-p", p],
    },
    "gemini": {
        "argv": lambda exe, p: [exe, "-p", p],
    },
    "codex": {
        "argv": lambda exe, p: [exe, "exec", "--skip-git-repo-check", p],
    },
    # NOTE: ollama does NOT generate via this argv — call_llm routes it to the
    # HTTP API (_call_ollama_api). The `ollama run` CLI renders for a terminal
    # (spinner, ANSI word-wrap) which corrupts machine-readable output. The argv
    # here is kept only for `ollama --version` discovery in active_cli_info.
    "ollama": {
        "argv": lambda exe, p: [exe, "run", OLLAMA_MODEL, p],
        "model_argv": lambda exe, p, m: [exe, "run", m, p],
    },
}

_ACTIVE_CLI_OVERRIDE: str | None = None
_ACTIVE_MODEL_OVERRIDE: str | None = None
_CLI_INFO_CACHE: dict | None = None


def set_llm_cli(name: str | None) -> None:
    """Override the configured LLM CLI at runtime (e.g. from --cli)."""
    global _ACTIVE_CLI_OVERRIDE, _CLI_INFO_CACHE
    if name:
        _ACTIVE_CLI_OVERRIDE = name.strip()
        _CLI_INFO_CACHE = None


def set_llm_model(model: str | None) -> None:
    """Override the recorded model name at runtime (e.g. from --model)."""
    global _ACTIVE_MODEL_OVERRIDE, _CLI_INFO_CACHE
    if model:
        _ACTIVE_MODEL_OVERRIDE = model.strip()
        _CLI_INFO_CACHE = None


def _active_cli_name() -> str:
    return _ACTIVE_CLI_OVERRIDE or LLM_CLI


def _declared_model(name: str) -> str:
    """The user-declared model label: --model / PW_LLM_MODEL, or ollama's config.

    CLIs do not reliably report their own model, so this is the user's assertion
    (required via --model). It is recorded verbatim in the report note.
    """
    if _ACTIVE_MODEL_OVERRIDE:
        return _ACTIVE_MODEL_OVERRIDE
    if LLM_MODEL:
        return LLM_MODEL
    if name == "ollama":
        return OLLAMA_MODEL
    return ""


def _resolve_cli() -> tuple[str, str | None, dict]:
    """Return (name, executable_path_or_None, spec_dict) for the active CLI."""
    name = _active_cli_name()
    spec = LLM_CLI_SPECS.get(name, LLM_CLI_SPECS["agy"])
    exe = _find_cli(name, f"{name.upper()}_CLI_PATH")
    return name, exe, spec


def active_cli_info() -> dict:
    """Name/path/CLI-version/model of the active backend — for the report note.

    ``version`` is the CLI tool's own version (``<cli> --version``); ``model`` is
    the underlying LLM identifier (from --model / PW_LLM_MODEL / ollama config),
    or empty when the CLI does not expose it.
    """
    global _CLI_INFO_CACHE
    if _CLI_INFO_CACHE is not None:
        return _CLI_INFO_CACHE
    name, exe, spec = _resolve_cli()
    version = ""
    if exe:
        try:
            p = _run_process([exe, "--version"], env=os.environ.copy(), timeout=15)
            out = (p.stdout or "").strip()
            version = out.splitlines()[0] if out else ""
        except Exception:
            version = ""
    _CLI_INFO_CACHE = {
        "name": name,
        "path": exe or "",
        "version": version,
        "model": _declared_model(name),         # user-declared label, recorded verbatim
    }
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

# Terminal control sequences some CLIs stream into stdout (e.g. Ollama's live
# "thinking" spinner redrawing the line with cursor moves / erase-line codes).
# These embed raw \x1b bytes that make json.loads fail with "Invalid control
# character", so we scrub them before parsing. Covers CSI (\x1b[...), OSC
# (\x1b]...BEL/ST), and lone two-char escapes.
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
# Leftover C0 control chars (backspace, carriage return, etc.) other than the
# whitespace JSON tolerates natively.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_terminal_noise(text: str) -> str:
    """Remove ANSI escape sequences and stray control chars from CLI output."""
    if not text:
        return text
    text = _ANSI_RE.sub("", text)
    return _CTRL_RE.sub("", text)


# Reasoning/"thinking" blocks that reasoning models emit before the real answer.
# They routinely contain braces and JSON examples, so a naive first-`{` scan
# grabs reasoning instead of the answer. We drop these blocks first. Covers
# <think>...</think> (many models) and Ollama's plain-text console block
# ("Thinking...\n...\n...done thinking."). The Ollama form only strips when the
# closing marker is present, so a truncated/odd stream is left intact rather
# than nuked. Model-agnostic: non-reasoning output has no markers -> no-op.
_THINK_TAG_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_OLLAMA_THINK_RE = re.compile(
    r"(?:^|\n)\s*Thinking\.\.\..*?\.\.\.\s*done thinking\.?", re.IGNORECASE | re.DOTALL
)


def _strip_reasoning(text: str) -> str:
    """Remove reasoning-model thinking blocks that precede the real answer."""
    if not text:
        return text
    text = _THINK_TAG_RE.sub("", text)
    text = _OLLAMA_THINK_RE.sub("", text)
    return text


def _clean_output(text: str) -> str:
    """Scrub terminal noise + reasoning blocks + markdown fences from CLI output."""
    text = _strip_reasoning(_strip_terminal_noise((text or "").strip()))
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text).strip()


def _iter_json_objects(text: str):
    """Yield every balanced top-level ``{...}`` substring, in document order.

    Balanced-bracket scanning that ignores braces inside strings, so nested
    objects are handled. Reasoning models emit the real answer *after* their
    thinking, so callers that want the answer should prefer the LAST candidate.
    """
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc = 0, False, False
        for j in range(i, n):
            ch = text[j]
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
                    yield text[i : j + 1]
                    i = j + 1
                    break
        else:
            # Unbalanced tail — yield best-effort remainder and stop.
            yield text[i:]
            return


# Optional last-ditch repair for malformed JSON (unescaped quotes, missing
# commas, trailing commas) from small local models. Soft dependency: if
# json-repair isn't installed, we simply skip repair and fall through to the
# normal retry loop. Primary defense is grammar-constrained decoding (Ollama
# --format json); this only helps backends that lack that.
try:  # pragma: no cover - import guard
    from json_repair import repair_json as _repair_json
except Exception:  # pragma: no cover
    _repair_json = None


def _loads_lenient(candidate: str) -> object:
    """json.loads(strict=False), falling back to json-repair when available.

    Raises json.JSONDecodeError if the text can't be parsed even after repair.
    """
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        if _repair_json is None:
            raise
        repaired = _repair_json(candidate)  # returns "" if unrepairable
        if not repaired:
            raise
        return json.loads(repaired, strict=False)


def _candidate_payloads(payload: object):
    """Yield the payload plus likely-unwrapped variants for schema validation.

    Small models sometimes echo the JSON-Schema envelope we send them, nesting
    the real instance under a ``"properties"`` key (or a single wrapper key like
    the schema title). Yielding those unwrapped variants lets validation recover
    ``{"properties": {"x": 1}}`` -> ``{"x": 1}`` instead of failing.
    """
    yield payload
    if isinstance(payload, dict):
        inner = payload.get("properties")
        if isinstance(inner, dict) and inner is not payload:
            yield inner
        if len(payload) == 1:
            (only,) = payload.values()
            if isinstance(only, dict):
                yield only


def _extract_json(text: str) -> str:
    """Extract the first complete JSON object from LLM output (best-effort).

    Strips terminal noise, reasoning blocks and markdown fences, then returns
    the first balanced ``{...}``. Used by callers without a schema to validate
    against; schema callers use :func:`_iter_json_objects` to try all candidates.
    """
    text = _clean_output(text)
    for candidate in _iter_json_objects(text):
        return candidate
    return text


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
# Ollama HTTP API — used instead of `ollama run`, which renders for a terminal
# (spinner + ANSI word-wrap) and corrupts machine-readable output. The API
# returns the answer in a clean `response` field, keeps any reasoning in a
# separate `thinking` field, and accepts a JSON Schema as `format` for
# schema-constrained decoding (guaranteed-valid, correctly-shaped output).
# ---------------------------------------------------------------------------

def _ollama_host() -> str:
    """Base URL of the Ollama server (honours OLLAMA_HOST, defaults to local)."""
    host = (os.environ.get("OLLAMA_HOST") or "").strip() or "http://127.0.0.1:11434"
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host.rstrip("/")


def _call_ollama_api(prompt: str, model: str, *, fmt, timeout: int) -> str:
    """POST to Ollama's /api/generate and return the clean `response` text.

    ``fmt`` may be a JSON Schema dict (schema-constrained), the string "json"
    (any valid JSON), or None (free text). Degrades gracefully: if the server
    rejects `think` or a schema `format`, it retries without that feature.
    """
    import requests

    url = _ollama_host() + "/api/generate"
    body: dict = {"model": model, "prompt": prompt, "stream": False, "think": False}
    if fmt is not None:
        body["format"] = fmt
    last_text = ""
    for _ in range(3):
        try:
            resp = requests.post(url, json=body, timeout=timeout)
        except requests.RequestException as e:
            raise RuntimeError(f"Ollama API request to {url} failed: {e}") from e
        if resp.status_code == 200:
            return (resp.json().get("response") or "").strip()
        last_text = resp.text or ""
        low = last_text.lower()
        # Degrade unsupported features and retry rather than hard-failing.
        if "think" in body and "think" in low:
            body.pop("think", None)
            continue
        if isinstance(body.get("format"), dict):  # schema-constrained unsupported
            body["format"] = "json"
            continue
        break
    raise RuntimeError(f"Ollama API error at {url}: {last_text[:300]}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_llm(
    prompt: str,
    timeout: int = LLM_TIMEOUT,
    desc: str | None = None,
    json_mode: bool = False,
    json_schema: dict | None = None,
) -> str:
    """Call the active LLM backend with a prompt and return its text response.

    Backend is selected by --cli / PW_LLM_CLI (default: agy). Pass ``desc`` to
    show an animated spinner while the call runs. ``json_mode=True`` requests
    JSON output; ``json_schema`` additionally constrains it to that schema where
    the backend supports it (Ollama), guaranteeing valid, correctly-shaped JSON.
    Ollama is driven via its HTTP API; all other backends shell out to their CLI.
    """
    name, exe, spec = _resolve_cli()
    safe_prompt = _safe_arg(prompt)
    model = _declared_model(name)

    if name == "ollama":
        # Prefer schema-constrained decoding, else plain JSON mode, else free text.
        fmt = json_schema if json_schema is not None else ("json" if json_mode else None)
        with _spinner(desc):
            return _call_ollama_api(safe_prompt, model or OLLAMA_MODEL, fmt=fmt, timeout=timeout)

    if not exe:
        raise RuntimeError(
            f"LLM CLI '{name}' not found. Ensure it is on PATH, pick another with "
            f"--cli, or set {name.upper()}_CLI_PATH=/path/to/{name}."
        )
    # Where the CLI supports model selection (agy), pass the declared model
    # through; otherwise the CLI runs its own default.
    if model and "model_argv" in spec:
        argv = spec["model_argv"](exe, safe_prompt, model)
    else:
        argv = spec["argv"](exe, safe_prompt)
    with _spinner(desc):
        proc = _run_process(argv, env=os.environ.copy(), timeout=timeout)
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
    json_schema = schema.model_json_schema()

    for _ in range(max(1, max_tries)):
        raw = call_llm(
            current_prompt, timeout=timeout, desc=desc,
            json_mode=True, json_schema=json_schema,
        )
        last_raw = raw
        # Reasoning models emit the answer AFTER their thinking (which may itself
        # contain JSON-shaped examples), so try every balanced {...} candidate,
        # last first, and accept the first that both parses and validates.
        candidates = list(_iter_json_objects(_clean_output(raw)))
        json_err: Exception | None = None
        val_err: ValidationError | None = None
        for candidate in reversed(candidates):
            try:
                payload = _loads_lenient(candidate)
            except json.JSONDecodeError as exc:
                json_err = exc
                continue
            for obj in _candidate_payloads(payload):
                try:
                    return schema.model_validate(obj)
                except ValidationError as exc:
                    val_err = exc
        # No candidate validated — re-prompt with the most actionable hint.
        if val_err is not None:
            last_err = val_err
            current_prompt = base_prompt + "\n\n" + _repair_hint(val_err)
        else:
            last_err = json_err or last_err
            current_prompt = (
                base_prompt
                + "\n\nPrevious output was not valid JSON. Output ONLY a valid JSON object."
            )

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
    raw = call_llm(json_prompt, timeout=timeout, json_mode=True)
    json_text = _extract_json(raw)
    return _loads_lenient(json_text)
