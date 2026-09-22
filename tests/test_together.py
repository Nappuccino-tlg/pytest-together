"""What the plugin promises, checked against code that really does race.

Nothing here is mocked. A race either happens on a real thread or it does not, and a
fake that reported one would be the exact failure this package exists to catch.
"""

import asyncio
import threading
import time

import pytest

from pytest_together import NotConcurrent, Result, Together

# -- the thing it is for ---------------------------------------------------------------


def test_a_check_then_act_bug_is_caught(together):
    """The headline. Read a counter, do some work, write it back: sequentially it is
    correct, and under a real overlap every worker reads the same value."""
    state = {"count": 0}
    limit = 1

    def take_the_last_seat():
        seen = state["count"]
        if seen >= limit:
            return False
        time.sleep(0.02)  # the window -- a query, a round trip, a lock not held
        state["count"] = seen + 1
        return True

    result = together(take_the_last_seat, times=20)

    assert result.count(bool) > limit, "the race did not happen"
    assert result.overlapped


def test_a_lock_closes_the_same_window(together):
    """The other half, and the half that decides whether anyone trusts the tool: with
    the read and the write held together, exactly one worker wins."""
    state = {"count": 0}
    limit = 1
    lock = threading.Lock()

    def take_the_last_seat():
        with lock:
            seen = state["count"]
            if seen >= limit:
                return False
            time.sleep(0.02)
            state["count"] = seen + 1
            return True

    result = together(take_the_last_seat, times=20)

    assert result.count(bool) == limit
    assert result.overlapped


# -- the part that makes it more than a barrier ----------------------------------------


def test_setup_inside_the_worker_is_caught(together):
    """The reason this is more than a barrier, and the exact bug it was written for.

    Every worker leaves the barrier at the same instant and then spends its own time
    opening a connection. Timing the barrier would call this concurrent and be wrong --
    by the time the contended line runs, the workers are strung out and nothing races.
    Marking the real moment is what makes the failure visible.
    """
    queue = {"n": 0}
    lock = threading.Lock()

    def connect_then_race(mark):
        with lock:  # a pool with one slot: each worker waits longer than the last
            mine = queue["n"]
            queue["n"] += 1
        time.sleep(mine * 0.01)
        mark()  # only now is the window open, and it opened late
        return True

    with pytest.raises(NotConcurrent) as caught:
        together(connect_then_race, times=6)

    message = str(caught.value)
    assert "did not overlap" in message
    assert "proved nothing" in message
    assert "in the work itself" in message, "should say the workers marked their own start"
    assert "tolerance_ms=" in message, "the failure should say how to accept it on purpose"


def test_warming_before_the_barrier_is_what_fixes_it(together):
    """The same work, with the expensive part done once before anyone races. This is the
    advice the failure message gives, and it has to actually work."""
    pool = [object() for _ in range(6)]  # opened up front, outside the race
    handed = iter(pool)
    lock = threading.Lock()
    seen = []

    def race_only(mark):
        mark()
        with lock:
            seen.append(next(handed))
        return True

    result = together(race_only, times=6)

    assert result.overlapped
    assert len(seen) == 6


def test_a_worker_without_mark_is_timed_from_the_barrier(together):
    """No argument, no mark: the whole body is assumed to be the race. Right for a short
    body, and the docstring says so rather than pretending otherwise."""
    result = together(lambda: True, times=4)

    assert result.overlapped
    assert not any(o.marked for o in result)


def test_only_the_first_mark_counts(together):
    """A worker that marks in a loop means the first time round. A later call moving the
    evidence would let a slow test quietly re-qualify itself."""

    def marks_twice(mark):
        mark()
        time.sleep(0.02)
        mark()
        return True

    result = together(marks_twice, times=3)
    assert result.overlapped, "a second mark should not have moved the start"


def test_a_wide_spread_can_be_accepted_deliberately(together):
    """Sometimes the work really is slow and the tester knows it. Raising the tolerance
    is allowed; it just has to be said out loud rather than happening by default."""

    def slow():
        time.sleep(0.01)
        return True

    result = together(slow, times=4, tolerance_ms=5000)
    assert len(result) == 4


def test_lenient_mode_reports_instead_of_failing():
    loose = Together(tolerance_ms=0.0001, strict=False)
    result = loose(lambda: True, times=3)

    assert not result.overlapped
    assert result.spread_ms > 0
    assert result.count(bool) == 3


# -- asyncio ---------------------------------------------------------------------------


async def test_coroutines_race_too(together):
    """pytest-race is threads only, and most of the code that races in Python now is
    async. Same barrier, same measurement, on the running loop."""
    state = {"count": 0}

    async def take():
        seen = state["count"]
        if seen >= 1:
            return False
        await asyncio.sleep(0.02)
        state["count"] = seen + 1
        return True

    result = await together(take, times=15)

    assert result.count(bool) > 1
    assert result.overlapped


async def test_an_async_lock_closes_it(together):
    state = {"count": 0}
    lock = asyncio.Lock()

    async def take():
        async with lock:
            seen = state["count"]
            if seen >= 1:
                return False
            await asyncio.sleep(0.02)
            state["count"] = seen + 1
            return True

    result = await together(take, times=15)
    assert result.count(bool) == 1


async def test_mixing_sync_and_async_is_refused(together):
    """Half on threads and half on the loop is not simultaneous, and silently doing it
    would produce a number nobody could interpret."""

    async def coro():
        return 1

    with pytest.raises(TypeError, match="two calls"):
        together(coro, lambda: 2)


# -- several different callables -------------------------------------------------------


def test_different_callables_run_against_the_same_barrier(together):
    """The classic pair: a deposit and a withdrawal that must not interleave."""
    seen = []

    def deposit():
        seen.append("deposit")
        return "deposit"

    def withdraw():
        seen.append("withdraw")
        return "withdraw"

    result = together(deposit, withdraw)

    assert sorted(result.values) == ["deposit", "withdraw"]
    assert len(seen) == 2


# -- results ---------------------------------------------------------------------------


def test_an_exception_in_one_worker_does_not_strand_the_others(together):
    """A worker that raises must not leave the rest waiting at a barrier that will never
    release -- the test would hang instead of failing, which is the worst outcome."""

    def sometimes():
        if threading.current_thread().name.endswith("-1"):
            raise ValueError("boom")
        return True

    result = together(sometimes, times=4)

    assert len(result) == 4
    for outcome in result:
        assert outcome.ok or isinstance(outcome.error, ValueError)


def test_errors_are_kept_and_can_be_reraised(together):
    def always_fails():
        raise RuntimeError("nope")

    result = together(always_fails, times=3)

    assert len(result.errors) == 3
    with pytest.raises(RuntimeError, match="nope"):
        result.raise_for_errors()


def test_raise_for_errors_is_quiet_when_nothing_failed(together):
    result = together(lambda: 1, times=2)
    result.raise_for_errors()
    assert result.values == [1, 1]


def test_count_takes_a_predicate(together):
    numbers = iter(range(10))
    lock = threading.Lock()

    def take():
        with lock:
            return next(numbers)

    result = together(take, times=10)
    assert result.count(lambda n: n % 2 == 0) == 5


def test_outcomes_are_in_the_order_given(together):
    result = together(lambda: "a", lambda: "b", lambda: "c")
    assert result.values == ["a", "b", "c"]
    assert [o.index for o in result] == [0, 1, 2]


# -- misuse ------------------------------------------------------------------------------


def test_one_worker_cannot_race(together):
    with pytest.raises(ValueError, match="at least 2"):
        together(lambda: 1, times=1)


def test_times_needs_exactly_one_callable(together):
    with pytest.raises(TypeError, match="exactly one"):
        together(lambda: 1, lambda: 2, times=5)


def test_nothing_to_run(together):
    with pytest.raises(TypeError, match="nothing to run"):
        together()


def test_spread_of_a_single_outcome_is_zero():
    assert Result().spread_ms == 0.0
    assert Result().overlapped


# -- choosing the instant, in detail -------------------------------------------------


async def test_an_async_worker_can_mark_too(together):
    """Same seam on the loop: the expensive await happens before the window opens."""
    order = {"n": 0}

    async def connect_then_race(mark):
        mine = order["n"]
        order["n"] += 1
        await asyncio.sleep(mine * 0.01)  # a pool handing out one connection at a time
        mark()
        return True

    with pytest.raises(NotConcurrent, match="in the work itself"):
        await together(connect_then_race, times=5)


async def test_an_async_worker_that_raises_is_recorded(together):
    async def boom():
        raise RuntimeError("async nope")

    result = await together(boom, times=3)

    assert len(result.errors) == 3
    with pytest.raises(RuntimeError, match="async nope"):
        result.raise_for_errors()


def test_unmarked_workers_get_the_warm_it_first_advice():
    """The other half of the failure message. A worker that never marked cannot be told
    the gap is in its own work, because nobody said where its work began."""
    impossible = Together(tolerance_ms=-1.0)

    with pytest.raises(NotConcurrent) as caught:
        impossible(lambda: True, times=3)

    assert "Warm it before the barrier" in str(caught.value)
    assert "in the work itself" not in str(caught.value)


def test_wants_mark_reads_the_signature():
    """Which workers are handed `mark`, spelled out. A wrong answer here either drops
    the argument a worker asked for or passes one a worker cannot take."""
    wants = Together._wants_mark

    assert wants(lambda mark: None)
    assert wants(lambda *args: None)
    assert not wants(lambda: None)
    assert not wants(lambda mark=None: None), "a default means it can run without one"
    assert not wants(lambda *, mark=None: None), "keyword-only is not positional"
    # Some C callables have no readable signature at all -- `range` is one. Those fall
    # back to not being passed a mark, because guessing wrong raises a TypeError from
    # inside a worker that the person writing the test did not write and cannot explain.
    assert not wants(range)
    assert not wants(type)
