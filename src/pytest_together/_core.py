"""Run callables at the same instant, and say whether they actually managed it.

A barrier is the easy half. Every concurrency test has one, and every concurrency test
believes it. The hard half is that a barrier releases *threads*, and a thread that then
opens a socket, takes a lock, or imports something on first use does not arrive when the
barrier said it did -- it arrives whenever that first-time cost finishes. The window a race
needs is a few milliseconds wide, and a first-call cost is comfortably wider.

So the test passes, the race never happens, and the green tick means nothing. The way that
gets discovered is in production.

Everything here exists to make that failure visible. Each worker records the instant the
race actually opens for it, and the spread between the first and the last is reported with
the result; too wide, and `Together` refuses to let the test claim a pass it did not earn.

Which instant that is matters, and is the one thing a caller has to help with. Released
from a barrier, every worker *starts* together by definition -- so timing the barrier
proves nothing about a worker that then spends forty milliseconds opening a connection
before it touches the contended thing. A worker that takes an argument is handed `mark`
and decides for itself::

    def take_a_seat(mark):
        conn = pool.get()   # first-call cost, nobody is racing yet
        mark()              # from here the window is open
        seen = conn.read(KEY)
        ...

Without `mark` the barrier is used, which is right when the whole body is the race and
wrong -- silently -- when it is not.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

#: How far apart the workers may start and still be called simultaneous.
#:
#: A check-then-act race is open for as long as the gap between the read and the write --
#: a lock acquisition, a database round trip, single-digit milliseconds at the generous
#: end. Workers spread wider than this are not testing that, whatever the barrier did.
DEFAULT_TOLERANCE_MS = 5.0


class NotConcurrent(AssertionError):
    """The workers did not start close enough together to prove anything.

    An AssertionError rather than a plain exception, because that is what it is: the test
    asserted a race and no race was possible. Failing here is the entire point -- a
    concurrency test that quietly did not overlap is worse than no test, because it is
    counted as evidence.
    """


@dataclass
class Outcome:
    """What one worker did, and when it really started."""

    index: int
    value: Any = None
    error: BaseException | None = None
    #: perf_counter at the instant the race opened for this worker: when it called
    #: `mark()`, or when it left the barrier if it never did. Not when it was scheduled,
    #: and not when it finished.
    started: float = 0.0
    #: Whether this worker chose the instant itself. A result where nobody did is only
    #: as good as the assumption that each body is a race from its first line.
    marked: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Result:
    """Every worker's outcome, plus the evidence that they overlapped."""

    outcomes: list[Outcome] = field(default_factory=list)
    tolerance_ms: float = DEFAULT_TOLERANCE_MS

    @property
    def spread_ms(self) -> float:
        """Milliseconds between the first worker starting and the last.

        The number to put in a failure message. If it is large, nothing else in the result
        means what it appears to mean.
        """
        if len(self.outcomes) < 2:
            return 0.0
        starts = [o.started for o in self.outcomes]
        return (max(starts) - min(starts)) * 1000

    @property
    def overlapped(self) -> bool:
        """Whether the workers started close enough together to have raced."""
        return self.spread_ms <= self.tolerance_ms

    @property
    def values(self) -> list[Any]:
        """What each worker returned, in the order they were given."""
        return [o.value for o in self.outcomes]

    @property
    def errors(self) -> list[BaseException]:
        return [o.error for o in self.outcomes if o.error is not None]

    def raise_for_errors(self) -> None:
        """Re-raise the first exception a worker raised.

        Workers swallow their exceptions so that one failure does not strand the others at
        the barrier. Nothing is hidden: this puts the first one back.
        """
        for outcome in self.outcomes:
            if outcome.error is not None:
                raise outcome.error

    def count(self, predicate: Callable[[Any], bool] = bool) -> int:
        """How many successful workers satisfy `predicate`. The usual assertion is about
        this number -- how many got through the limiter, won the lock, took the last seat."""
        return sum(1 for o in self.outcomes if o.ok and predicate(o.value))

    def __len__(self) -> int:
        return len(self.outcomes)

    def __iter__(self):
        return iter(self.outcomes)


class Together:
    """Release N workers at one instant and measure whether they made it.

    Call it with a function to run n times, or with several functions to run once each::

        result = together(charge, times=50)
        result = together(deposit, withdraw)

    Sync callables run on threads; coroutine functions run as tasks on the running loop.
    Mixing the two in one call is refused rather than quietly serialised.
    """

    def __init__(self, tolerance_ms: float = DEFAULT_TOLERANCE_MS, strict: bool = True):
        self.tolerance_ms = tolerance_ms
        #: Whether a spread wider than the tolerance fails the test. On by default: the
        #: whole reason this exists is that the silent version of this is a false pass.
        self.strict = strict

    # -- the api ---------------------------------------------------------------------

    def __call__(
        self,
        *callables: Callable[..., Any],
        times: int | None = None,
        tolerance_ms: float | None = None,
        strict: bool | None = None,
    ) -> Result | Any:
        workers = self._plan(callables, times)
        tolerance = self.tolerance_ms if tolerance_ms is None else tolerance_ms
        enforce = self.strict if strict is None else strict

        if any(inspect.iscoroutinefunction(w) for w in workers):
            if not all(inspect.iscoroutinefunction(w) for w in workers):
                raise TypeError(
                    "mixing coroutine functions and plain functions in one call would "
                    "run half of them on threads and half on the loop, which is not "
                    "simultaneous -- use two calls"
                )
            return self._run_async(workers, tolerance, enforce)
        return self._run_threads(workers, tolerance, enforce)

    @staticmethod
    def _wants_mark(fn: Callable[..., Any]) -> bool:
        """Whether this worker asked to choose its own starting instant.

        Signature inspection rather than trying the call and catching TypeError: a worker
        that raises TypeError of its own would otherwise be retried with a different
        argument count, and the second failure would be blamed on us.
        """
        try:
            parameters = inspect.signature(fn).parameters.values()
        except (TypeError, ValueError):  # builtins and C callables have no signature
            return False
        for parameter in parameters:
            if parameter.kind is parameter.VAR_POSITIONAL:
                return True
            positional = (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            if parameter.kind in positional and parameter.default is parameter.empty:
                return True
        return False

    @staticmethod
    def _plan(callables: Sequence[Callable[..., Any]], times: int | None) -> list:
        if not callables:
            raise TypeError("nothing to run")
        if times is None:
            return list(callables)
        if len(callables) != 1:
            raise TypeError("times= runs one callable repeatedly; pass exactly one")
        if times < 2:
            raise ValueError("times must be at least 2 -- one worker cannot race")
        return [callables[0]] * times

    # -- threads ---------------------------------------------------------------------

    def _run_threads(self, workers: list, tolerance: float, enforce: bool) -> Result:
        count = len(workers)
        result = Result([Outcome(i) for i in range(count)], tolerance)

        # A barrier rather than a flag: every thread is already inside the barrier before
        # any of them is released, so the release costs one wake-up each rather than the
        # thread-start cost that a naive "start them all in a loop" pays per worker.
        gate = threading.Barrier(count)

        def run(index: int, fn: Callable[..., Any]) -> None:
            outcome = result.outcomes[index]
            try:
                gate.wait()
            except threading.BrokenBarrierError as exc:  # pragma: no cover - defensive
                outcome.error = exc
                return
            outcome.started = time.perf_counter()

            def mark() -> None:
                # Only the first call counts: a worker that marks in a loop means the
                # first time round, and a later one would quietly move the evidence.
                if not outcome.marked:
                    outcome.marked = True
                    outcome.started = time.perf_counter()

            try:
                outcome.value = fn(mark) if self._wants_mark(fn) else fn()
            # Broad on purpose: a worker that raises must not strand the others at a
            # barrier that will never release. raise_for_errors puts it back.
            except BaseException as exc:
                outcome.error = exc

        threads = [
            threading.Thread(target=run, args=(i, fn), daemon=True)
            for i, fn in enumerate(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self._check(result, enforce)
        return result

    # -- asyncio ---------------------------------------------------------------------

    async def _run_async(self, workers: list, tolerance: float, enforce: bool) -> Result:
        count = len(workers)
        result = Result([Outcome(i) for i in range(count)], tolerance)

        ready = asyncio.Event()
        parked = [asyncio.Event() for _ in range(count)]

        async def run(index: int, fn: Callable[..., Any]) -> None:
            outcome = result.outcomes[index]
            # Report parked *before* waiting, so the release happens only once every task
            # has reached this line -- a task still being created has not arrived.
            parked[index].set()
            await ready.wait()
            outcome.started = time.perf_counter()

            def mark() -> None:
                if not outcome.marked:
                    outcome.marked = True
                    outcome.started = time.perf_counter()

            try:
                outcome.value = await (fn(mark) if self._wants_mark(fn) else fn())
            # Broad on purpose: a worker that raises must not strand the others at a
            # barrier that will never release. raise_for_errors puts it back.
            except BaseException as exc:
                outcome.error = exc

        tasks = [asyncio.create_task(run(i, fn)) for i, fn in enumerate(workers)]
        await asyncio.gather(*(event.wait() for event in parked))
        ready.set()
        await asyncio.gather(*tasks)

        self._check(result, enforce)
        return result

    # -- the verdict -------------------------------------------------------------------

    def _check(self, result: Result, enforce: bool) -> None:
        if not enforce or result.overlapped:
            return
        if any(outcome.marked for outcome in result):
            # They chose their own instant and still arrived apart, so the gap is not in
            # setup this tool can advise about -- it is in the work being timed.
            hint = (
                "Each worker marked its own start, so the gap is in the work itself "
                "rather than in setup before it."
            )
        else:
            hint = (
                "Usually it is first-call cost inside the worker: a connection being "
                "opened, a lazy import, a pool filling. Warm it before the barrier, or "
                "take a `mark` argument and call mark() once the racing part is about "
                "to begin."
            )
        raise NotConcurrent(
            f"the {len(result)} workers started {result.spread_ms:.1f}ms apart, which is "
            f"wider than the {result.tolerance_ms:.1f}ms tolerance -- they did not "
            f"overlap, so this test proved nothing.\n"
            f"{hint}\n"
            f"If the work here really is that slow, raise the tolerance deliberately: "
            f"together(..., tolerance_ms={result.spread_ms * 2:.0f})."
        )
