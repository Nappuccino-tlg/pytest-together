"""The pytest fixtures. See _core for what they actually do."""

from __future__ import annotations

import pytest

from ._core import DEFAULT_TOLERANCE_MS, Together


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("together")
    group.addoption(
        "--together-tolerance",
        type=float,
        default=DEFAULT_TOLERANCE_MS,
        metavar="MS",
        help=(
            "how far apart workers may start and still count as simultaneous "
            f"(default: {DEFAULT_TOLERANCE_MS}ms). A slow shared runner may need more; "
            "raise it deliberately rather than to make a red test green."
        ),
    )
    group.addoption(
        "--together-lenient",
        action="store_true",
        help=(
            "report a wide spread instead of failing on it. Off by default, because a "
            "concurrency test that did not overlap is a false pass, and a false pass is "
            "worse than no test."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "together(tolerance_ms=..., strict=...): settings for the `together` fixture",
    )


@pytest.fixture
def together(request: pytest.FixtureRequest) -> Together:
    """Release several callables at one instant and report whether they overlapped.

        def test_one_seat_left(together):
            result = together(book_the_seat, times=20)
            assert result.count(bool) == 1

    Fails the test if the workers did not start close enough together to have raced --
    which is the failure this exists to surface.
    """
    tolerance = request.config.getoption("--together-tolerance")
    strict = not request.config.getoption("--together-lenient")

    marker = request.node.get_closest_marker("together")
    if marker is not None:
        tolerance = marker.kwargs.get("tolerance_ms", tolerance)
        strict = marker.kwargs.get("strict", strict)

    return Together(tolerance_ms=tolerance, strict=strict)
