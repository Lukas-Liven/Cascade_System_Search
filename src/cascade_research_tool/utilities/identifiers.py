"""
Identifier and case-study-name normalization utilities.

These functions establish canonical representations used across initialization,
crosswalk matching, cache artifact naming, and case-study management.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd


def normalize_identifier(value: Any) -> Optional[str]:
    """
    Normalize NID/provider identifiers to stable strings.

    NID IDs may be loaded as strings, numbers, or missing values depending on
    the source and pandas type inference. This prevents mismatches caused by
    inconsistent representations such as numeric-looking IDs.
    """

    if value is None or pd.isna(value):
        return None

    text = str(value).strip()

    return text if text else None


def parse_comid(value: Any) -> Optional[int]:
    """
    Extract an integer COMID from a plain value or URL-like GeoConnex value.

    Example accepted GeoConnex value:
    https://geoconnex.us/ref/nhdplusv2/comid/12345678
    """

    if value is None or pd.isna(value):
        return None

    text = str(value).strip().rstrip("/")

    if not text:
        return None

    match = re.search(r"(\d+)$", text)

    if not match:
        return None

    try:
        return int(match.group(1))
    except ValueError:
        return None


def normalize_study_name(study_name: str) -> str:
    """
    Normalize and validate a user-provided case-study display name.

    Study names are only used to create files inside the private
    case_studies directory. Path separators and null bytes are prohibited so
    a name cannot escape that directory.
    """

    normalized_name = re.sub(
        r"\s+",
        " ",
        study_name.strip(),
    )

    if not normalized_name:
        raise ValueError("A case study name is required.")

    if len(normalized_name) > 80:
        raise ValueError(
            "A case study name must contain 80 characters or fewer."
        )

    if any(
        character in normalized_name
        for character in ("/", "\\", "\0")
    ):
        raise ValueError(
            "A case study name cannot contain path separators."
        )

    return normalized_name


def study_name_to_filename(study_name: str) -> str:
    """
    Convert a validated display name into a deterministic safe filename.

    The original display name remains in the case-study artifact payload.
    This filename is a private cache implementation detail.

    Different display names may normalize to the same safe filename. The UI
    must therefore evaluate existing-study checks using this post-normalized
    filename identity before allowing an overwrite.
    """

    safe_name = re.sub(
        r"[^A-Za-z0-9_. -]+",
        "_",
        study_name,
    ).strip(" .")

    if not safe_name:
        safe_name = "unnamed_study"

    return f"{safe_name}.study.pkl"