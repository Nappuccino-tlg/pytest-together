# pytest-together

Runs things at the same instant in a test — and fails if they did not actually overlap.

[![CI](https://github.com/Nappuccino-tlg/pytest-together/actions/workflows/ci.yml/badge.svg)](https://github.com/Nappuccino-tlg/pytest-together/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%20--%203.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/dependencies-pytest%20only-brightgreen)
[![PyPI](https://img.shields.io/pypi/v/pytest-together)](https://pypi.org/project/pytest-together/)

```bash
pip install pytest-together
```

## The concurrency test that never ran concurrently

```python
def test_only_one_booking_wins(together):
    result = together(book_the_last_seat, times=20)
    assert result.count(bool) == 1
```

Twenty callables, one barrier, one assertion. If the booking code has a check-then-act
race, more than one gets through and the test goes red.

That is the easy half, and most test suites already have it. The hard half is knowing the
twenty *actually overlapped* — because if they did not, the assertion passes, the bug is
still there, and a green tick now says it isn't.

They very often do not. A barrier releases the workers together, and then the first thing
each one does is open a connection, take a lock, or import something for the first time.
The window a race needs is a few milliseconds wide. A first-call cost is wider.

So this fails the test instead:

```
NotConcurrent: the 20 workers started 84.3ms apart, which is wider than the 5.0ms
tolerance -- they did not overlap, so this test proved nothing.
Usually it is first-call cost inside the worker: a connection being opened, a lazy
import, a pool filling. Warm it before the barrier, or take a `mark` argument and
call mark() once the racing part is about to begin.
If the work here really is that slow, raise the tolerance deliberately:
together(..., tolerance_ms=169).
```

A concurrency test that quietly did not overlap is worse than no test, because it is
counted as evidence.

## Saying where the race actually starts

Timing the barrier is not enough on its own, and pretending otherwise would be the same
lie one level up. Every worker leaves the barrier at the same instant *by construction* —
what matters is when each one reaches the line that contends.

A worker that takes an argument is handed `mark` and decides for itself:

```python
def take_a_seat(mark):
    conn = pool.get()  # first-call cost -- nobody is racing yet
    mark()  # from here the window is open
    seen = conn.get(KEY)
    ...
```

Without `mark`, the barrier is used — right when the whole body is the race, and wrong
when it is not. The failure message tells you which case you are in.

## asyncio, the same way

```python
async def test_one_charge_wins(together):
    result = await together(charge, times=50)
    assert result.count(bool) == 1
```

Coroutine functions run as tasks on the running loop, parked on an event until every one
of them has been created. Mixing coroutines and plain functions in one call is refused
rather than quietly running half on threads: that is not simultaneous, and the number it
produced could not be interpreted.

## The result

| | |
|---|---|
| `result.count(bool)` | successful workers matching a predicate — the usual assertion |
| `result.values` | what each worker returned, in the order given |
| `result.errors` | exceptions, kept rather than raised, so that one failure |
| `result.raise_for_errors()` | …does not strand the others at the barrier |
| `result.spread_ms` | how far apart they actually started |
| `result.overlapped` | whether that was tight enough to mean anything |

## Settings

Per call, per test, or per run:

```python
together(worker, times=20, tolerance_ms=50)  # this call
```

```python
@pytest.mark.together(tolerance_ms=50, strict=False)  # this test
```

```bash
pytest --together-tolerance=50   # a slow shared runner
pytest --together-lenient        # report a wide spread instead of failing on it
```

The default tolerance is **5ms**, which is roughly the widest a check-then-act window gets
— a lock acquisition, a database round trip. Raising it is allowed and sometimes correct.
Raising it to make a red test green is the thing this package exists to prevent, so it has
to be typed out rather than happening by default.

## What it does not do

**It does not find races.** It runs what you give it and tells you whether the attempt was
real. Whether the assertion afterwards is the right one is yours.

**It does not prove the absence of a race.** One overlapping run that came out clean is one
sample. Races are probabilistic; `times=` and repetition are how you buy confidence, and
neither buys certainty.

**It does not schedule interleavings.** There is no control over *which* order the workers
reach the contended line in — only that they are all trying at once. For exhaustive
interleaving you want a model checker, not a barrier.

**It is not a load tester.** Twenty threads for the length of one test. It answers a
correctness question, not a throughput one.

## Prior art

[`pytest-race`](https://github.com/idlesign/pytest-race) has the same barrier idea and is
the obvious thing to reach for first. This exists because of three things it does not do:
it is threads only, it cannot be told where the race begins, and — the reason this was
written — it does not measure whether the workers overlapped, so a test that did not race
passes exactly like one that did.

## Requirements

Python 3.9 or newer, and pytest. **Nothing else** — `threading`, `asyncio` and `inspect`
from the standard library. A testing plugin that drags a dependency tree into somebody's
test environment is one more thing for them to resolve, pin and upgrade.

## Tests

```bash
pip install -e ".[dev]"
coverage run -m pytest && coverage report
```

No services. The suite races real threads and real tasks against real shared state — a
mock that reported a race would be the exact failure this package was written to catch —
and the plugin's own behaviour is checked by running pytest inside pytest, because what is
under test is whether somebody else's suite goes red.

`coverage run -m pytest` rather than `pytest --cov`: a plugin is imported through its entry
point before pytest-cov starts measuring, so `--cov` reports most of this package as
unreached and the number is a lie.

## Where this came from

A rate limiter with a check-then-increment race, and a concurrency test that passed against
it. The test used a barrier. What it did not do was warm the connection pool, so the fifty
"simultaneous" attempts were fifty TLS handshakes arriving one after another, and the
limiter had all the time it needed to look correct.

That lesson became [redlimit](https://github.com/Nappuccino-tlg/redlimit)'s headline test,
then [ratecheck](https://github.com/Nappuccino-tlg/ratecheck) for HTTP endpoints. This is
the same idea for any Python test at all.

## License

MIT
