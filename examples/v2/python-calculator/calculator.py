"""Finite arithmetic CLI: python calculator.py 12 / 4."""
import argparse
import math


def calculate(left: float, operation: str, right: float) -> float:
    if not math.isfinite(left) or not math.isfinite(right):
        raise ValueError("Operands must be finite numbers")
    if operation == "+":
        return left + right
    if operation == "-":
        return left - right
    if operation == "*":
        return left * right
    if operation == "/":
        if right == 0:
            raise ValueError("Cannot divide by zero")
        return left / right
    raise ValueError("Unknown operation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=float)
    parser.add_argument("operation", choices=("+", "-", "*", "/"))
    parser.add_argument("right", type=float)
    args = parser.parse_args()
    try:
        print(calculate(args.left, args.operation, args.right))
    except ValueError as error:
        parser.error(str(error))
