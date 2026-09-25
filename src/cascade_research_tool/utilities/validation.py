"""
Input-validation utilities.

These functions validate application configuration values without depending on
Tkinter widgets, dialogs, or controller state. User-interface code is
responsible for displaying returned validation errors appropriately.
"""

from __future__ import annotations

import math


def parse_nonnegative_float(
    raw_value: str,
    field_label: str,
) -> float:
    """
    Parse a finite nonnegative floating-point value.

    Args:
        raw_value:
            User-provided text representation of a numeric value.

        field_label:
            Human-readable field name included in validation error messages.

    Raises:
        ValueError:
            If the value is blank, nonnumeric, NaN, infinite, or negative.
    """

    try:
        value = float(raw_value.strip())
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(
            f"{field_label} must be a valid number."
        ) from error

    if not math.isfinite(value):
        raise ValueError(
            f"{field_label} must be a finite number."
        )

    if value < 0:
        raise ValueError(
            f"{field_label} cannot be negative."
        )

    return value


def parse_nonnegative_integer(
    raw_value: str,
    field_label: str,
) -> int:
    """
    Parse a nonnegative whole-number value.

    Floating-point text such as ``2.5`` is not accepted because this parser
    is intended for counts and other discrete values.

    Args:
        raw_value:
            User-provided text representation of an integer.

        field_label:
            Human-readable field name included in validation error messages.

    Raises:
        ValueError:
            If the value is blank, non-integer, or negative.
    """

    try:
        cleaned_value = raw_value.strip()
    except AttributeError as error:
        raise ValueError(
            f"{field_label} must be a whole number greater than or equal to 0."
        ) from error

    if not cleaned_value:
        raise ValueError(
            f"{field_label} is required."
        )

    try:
        value = int(cleaned_value)
    except ValueError as error:
        raise ValueError(
            f"{field_label} must be a whole number greater than or equal to 0."
        ) from error

    if value < 0:
        raise ValueError(
            f"{field_label} must be greater than or equal to 0."
        )

    return value