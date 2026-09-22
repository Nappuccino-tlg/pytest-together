"""Run things at the same instant, and find out whether they really did.

A barrier is the easy half of a concurrency test. The hard half is knowing the workers
actually overlapped -- and a test that quietly did not overlap is counted as evidence
while proving nothing.
"""

from ._core import DEFAULT_TOLERANCE_MS, NotConcurrent, Outcome, Result, Together

__all__ = [
    "DEFAULT_TOLERANCE_MS",
    "NotConcurrent",
    "Outcome",
    "Result",
    "Together",
]
__version__ = "0.1.0"
