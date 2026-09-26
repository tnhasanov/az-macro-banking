"""Where the engine is, for whoever is watching.

The engine's functions call `stage("rendering")` at the points where the work actually changes
character. With nobody watching the calls do nothing; a job worker installs a reporter, and those
same calls become the stages a person sees on the job page. Nothing here estimates, extrapolates or
invents a percentage: a stage is reported when it starts, and a count ("dataset 12 of 31") only
where the engine knows both numbers.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Callable, Iterator

Reporter = Callable[[str, "str | None"], None]
_reporter: contextvars.ContextVar[Reporter | None] = contextvars.ContextVar("azmonitor_progress", default=None)
_detail: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "azmonitor_progress_detail", default=None)


def stage(name: str, detail: str | None = None) -> None:
    fn = _reporter.get()
    if fn is not None:
        fn(name, detail)


def detail(text: str) -> None:
    """Progress within the current stage. Never starts a new one."""
    fn = _detail.get()
    if fn is not None:
        fn(text)


@contextmanager
def reporting(on_stage: Reporter, on_detail: Callable[[str], None] | None = None) -> Iterator[None]:
    t1 = _reporter.set(on_stage)
    t2 = _detail.set(on_detail)
    try:
        yield
    finally:
        _reporter.reset(t1)
        _detail.reset(t2)
