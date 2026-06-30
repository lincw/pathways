"""Shared progress display utilities for the pipeline.

Provides a spinner context manager for long-running LLM calls and other
unknown-duration operations. Falls back gracefully when rich is absent.
"""

from contextlib import nullcontext
from typing import Optional

try:
    from rich.console import Console
    _console = Console(highlight=False)
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False


def spinner(desc: Optional[str]):
    """Animated spinner for the duration of a ``with`` block.

    Shows ``desc`` as status text while the block runs, then clears it.
    Returns ``nullcontext()`` when rich is unavailable or desc is None.
    """
    if desc and _HAS_RICH:
        return _console.status(f"[bold cyan]{desc}[/bold cyan]", spinner="dots")
    return nullcontext()
