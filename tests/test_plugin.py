"""The pytest surface: the fixture, the flags and the marker.

These run pytest inside pytest with the `pytester` fixture, because the thing under test
is what happens to somebody else's test run -- whether it goes red, and what it says when
it does. Calling the fixture's object directly would skip exactly that.
"""

import textwrap

import pytest

pytest_plugins = ["pytester"]

# A worker that arrives late, and differently late each time.
#
# Sleeping the same amount in every worker does not do it: released together they also
# finish together, and the spread stays small. Staggering is what a real queue for one
# connection does -- the fifth caller waits for the four ahead of it.
STAGGERED = """
import time, threading

_queue = {"n": 0}
_lock = threading.Lock()

def staggered(mark):
    with _lock:
        mine = _queue["n"]
        _queue["n"] += 1
    time.sleep(mine * 0.01)
    mark()
    return True
"""


@pytest.fixture(autouse=True)
def _clean_ini(pytester: pytest.Pytester) -> None:
    """Every inner run starts from a bare config, so nothing here inherits this repo's."""
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")


def write(pytester: pytest.Pytester, body: str, *, staggered: bool = True) -> None:
    """Write the inner test file.

    The body is dedented here rather than left to makepyfile: prepending STAGGERED, which
    starts at column zero, means the two have no common indent, so makepyfile's own dedent
    leaves the body exactly where it was. The result is an IndentationError at collection
    instead of the test that was meant to run.
    """
    source = textwrap.dedent(body)
    pytester.makepyfile((STAGGERED + source) if staggered else source)


def test_a_race_that_did_not_race_turns_the_test_red(pytester: pytest.Pytester) -> None:
    """The whole product in one run: without this, the inner test passes and is counted
    as evidence that the code is safe under concurrency."""
    write(
        pytester,
        """
        def test_looks_concurrent(together):
            together(staggered, times=5)
        """,
    )
    result = pytester.runpytest()

    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "did not overlap" in output
    assert "proved nothing" in output
    assert "tolerance_ms=" in output, "the failure has to say how to accept it on purpose"


def test_lenient_turns_the_failure_into_a_pass(pytester: pytest.Pytester) -> None:
    """An escape hatch has to exist, and it has to be typed out on purpose."""
    write(
        pytester,
        """
        def test_slow(together):
            result = together(staggered, times=5)
            assert not result.overlapped
        """,
    )
    assert pytester.runpytest().ret != 0
    pytester.runpytest("--together-lenient").assert_outcomes(passed=1)


def test_the_tolerance_flag_is_honoured(pytester: pytest.Pytester) -> None:
    write(
        pytester,
        """
        def test_slow(together):
            together(staggered, times=5)
        """,
    )
    assert pytester.runpytest().ret != 0
    pytester.runpytest("--together-tolerance=5000").assert_outcomes(passed=1)


def test_the_marker_overrides_the_command_line(pytester: pytest.Pytester) -> None:
    """Per-test settings belong next to the test, not in the invocation -- a tolerance
    raised on the command line quietly raises it for every other test too."""
    write(
        pytester,
        """
        import pytest

        @pytest.mark.together(tolerance_ms=5000)
        def test_allowed_to_be_slow(together):
            together(staggered, times=5)

        def test_still_strict(together):
            together(staggered, times=5)
        """,
    )
    pytester.runpytest().assert_outcomes(passed=1, failed=1)


def test_the_marker_can_relax_strictness(pytester: pytest.Pytester) -> None:
    write(
        pytester,
        """
        import pytest

        @pytest.mark.together(strict=False)
        def test_reports_instead_of_failing(together):
            result = together(staggered, times=5)
            assert not result.overlapped
        """,
    )
    pytester.runpytest().assert_outcomes(passed=1)


def test_a_real_race_still_passes(pytester: pytest.Pytester) -> None:
    """The plugin must not fail an honest test. A suite that goes red on correct usage
    gets uninstalled before anybody reads the message."""
    write(
        pytester,
        """
        def test_overlaps(together):
            result = together(lambda: True, times=8)
            assert result.overlapped
            assert result.count(bool) == 8
        """,
        staggered=False,
    )
    pytester.runpytest().assert_outcomes(passed=1)


def test_the_marker_is_registered(pytester: pytest.Pytester) -> None:
    """An unregistered marker is a warning, and a warning in somebody else's suite that
    came from an installed plugin is the plugin's fault."""
    result = pytester.runpytest("--markers")
    result.stdout.fnmatch_lines(["*together(tolerance_ms=*"])


def test_the_flags_are_documented(pytester: pytest.Pytester) -> None:
    result = pytester.runpytest("--help")
    output = result.stdout.str()
    assert "--together-tolerance" in output
    assert "--together-lenient" in output
