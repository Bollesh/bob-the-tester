"""Property tests over a copy of calculator.py. Self-contained: no env vars."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hypothesis import given, strategies as st

from calculator import add, factorial


@given(st.integers(), st.integers())
def test_prop_add_commutative(a, b):
    """TRUE invariant: addition commutes."""
    assert add(a, b) == add(b, a)


@given(st.integers(min_value=0, max_value=12))
def test_prop_factorial_strictly_exceeds_n(n):
    """FALSE invariant: factorial(n) > n breaks at n=1 and n=2."""
    assert factorial(n) > n
