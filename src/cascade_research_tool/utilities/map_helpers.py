"""
Map and display helper utilities.

These helpers do not create Folium maps, access Tkinter, open browsers, or
write files. They support Part 6 reporting and later map-service extraction.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import pandas as pd


def display_value(
    value: Any,
    fallback: str = "Unavailable",
) -> str:
    """
    Convert a potentially missing source value to safe readable text.

    NID-derived graph attributes commonly use None for missing values, but
    pandas NaN values are also handled. Non-scalar future source values are
    converted normally rather than causing output/reporting failures.
    """

    if value is None:
        return fallback

    try:
        if pd.isna(value):
            return fallback
    except (TypeError, ValueError):
        # Some non-scalar objects cannot be evaluated directly as a Boolean
        # after pd.isna(). Preserve them as readable text instead.
        pass

    text = str(value).strip()

    return text if text else fallback


def get_valid_map_coordinate(
    value: Any,
    minimum: float,
    maximum: float,
) -> Optional[float]:
    """
    Convert and validate a latitude or longitude for map rendering.

    Invalid, nonnumeric, non-finite, and out-of-range coordinate values return
    None. Callers should omit invalid points rather than allowing an erroneous
    source record to prevent valid cascade systems from being mapped.
    """

    try:
        coordinate = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(coordinate):
        return None

    if coordinate < minimum or coordinate > maximum:
        return None

    return coordinate


def bearing_degrees(
    latitude_1: float,
    longitude_1: float,
    latitude_2: float,
    longitude_2: float,
) -> float:
    """
    Calculate compass bearing from the first coordinate to the second.

    Returns a value in degrees where:

    - 0 points north;
    - 90 points east;
    - 180 points south;
    - 270 points west.

    Folium map arrow icons use this value to rotate an upward-facing triangle
    from an upstream dam toward its direct downstream dam.
    """

    latitude_1_radians = math.radians(latitude_1)
    latitude_2_radians = math.radians(latitude_2)
    longitude_delta_radians = math.radians(
        longitude_2 - longitude_1
    )

    x_value = math.sin(longitude_delta_radians) * math.cos(
        latitude_2_radians
    )
    y_value = (
        math.cos(latitude_1_radians)
        * math.sin(latitude_2_radians)
        - math.sin(latitude_1_radians)
        * math.cos(latitude_2_radians)
        * math.cos(longitude_delta_radians)
    )

    return (
        math.degrees(
            math.atan2(
                x_value,
                y_value,
            )
        )
        + 360
    ) % 360