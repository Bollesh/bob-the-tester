"""
test_placeholder.py — minimal baseline tests for smoke.py.

These tests give calculator.py ~40% coverage deliberately, leaving
large uncovered gaps for validate_and_keep to exploit.

DO NOT hand-edit this file.  Bob writes new tests here during runs.
Use scripts/reset_demo.py to restore this pristine state.
"""
import sys
import os

# Allow running from the repo root or tests/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from calculator import add, subtract


def test_add_positive():
    """Basic addition works."""
    assert add(2, 3) == 5


def test_add_zero():
    """Adding zero is a no-op."""
    assert add(10, 0) == 10


def test_subtract_positive():
    """Basic subtraction works."""
    assert subtract(10, 3) == 7
