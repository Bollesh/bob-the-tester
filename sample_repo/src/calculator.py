"""
calculator.py — P1 smoke-test target (placeholder).

PLACEHOLDER: P5 will replace sample_repo/src/ with the real demo modules
(pricing.py, rounding.py, parser.py, api.py) seeded with intentional bugs.
This file exists solely so smoke.py has something to run against without
requiring the full sample repo to be present.

Contains deliberately uncovered code paths so validate_and_keep has
something to find.

DO NOT import this from server/ — sample_repo/ is never a server dependency
(see AGENTS.md §6 repo layout rules).
"""
from __future__ import annotations


def add(a: float, b: float) -> float:
    """Return a + b."""
    return a + b


def subtract(a: float, b: float) -> float:
    """Return a - b."""
    return a - b


def multiply(a: float, b: float) -> float:
    """Return a * b."""
    return a * b


def divide(a: float, b: float) -> float:
    """
    Return a / b.

    Raises:
        ZeroDivisionError: when b is 0.
        TypeError: when inputs are not numeric.
    """
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError(f"Operands must be numeric, got {type(a)} and {type(b)}")
    if b == 0:
        raise ZeroDivisionError("Division by zero")
    return a / b


def power(base: float, exp: int) -> float:
    """Return base ** exp.  Negative exponents are allowed."""
    if not isinstance(exp, int):
        raise TypeError(f"Exponent must be an integer, got {type(exp)}")
    return base ** exp


def factorial(n: int) -> int:
    """
    Return n! for non-negative integer n.

    Raises:
        ValueError: when n < 0.
        TypeError: when n is not an integer.
    """
    if not isinstance(n, int):
        raise TypeError(f"n must be an integer, got {type(n)}")
    if n < 0:
        raise ValueError(f"Factorial is not defined for negative numbers, got {n}")
    if n == 0:
        return 1
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result


def is_prime(n: int) -> bool:
    """Return True if n is a prime number."""
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False
    for i in range(3, int(n ** 0.5) + 1, 2):
        if n % i == 0:
            return False
    return True


def gcd(a: int, b: int) -> int:
    """Return the greatest common divisor of a and b (Euclidean algorithm)."""
    while b:
        a, b = b, a % b
    return abs(a)
