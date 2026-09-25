"""
Safe CSV export utilities.

CSV values can be interpreted as formulas by spreadsheet applications such as
Microsoft Excel when they begin with certain characters. These utilities
sanitize externally sourced text before export and write files through a
temporary path so an incomplete output is not treated as a finished export.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd


def sanitize_csv_cell(value: Any) -> Any:
    """
    Reduce spreadsheet formula-injection risk for a CSV cell value.

    Spreadsheet software may interpret cells starting with =, +, -, or @ as
    formulas. Prefixing an apostrophe causes common spreadsheet applications
    to interpret the value as text while preserving the displayed content.

    The original in-memory value is not modified; this function is applied
    only to a copy used for CSV export.
    """

    if value is None or pd.isna(value):
        return None

    if isinstance(value, str):
        trimmed_value = value.lstrip()

        if trimmed_value.startswith(("=", "+", "-", "@")):
            return "'" + value

    return value


def export_dataframe_to_csv(
    dataframe: pd.DataFrame,
    output_file: Path,
) -> None:
    """
    Export a DataFrame as a user-readable, spreadsheet-safe CSV file.

    This function is intentionally one-way: exported CSV files are not
    application workflow inputs or operational sources of truth. A temporary
    neighboring file is written first, then atomically replaced to prevent
    partially written output files from being presented as complete.
    """

    temporary_file = output_file.with_suffix(
        output_file.suffix + ".tmp"
    )
    export_dataframe = dataframe.copy()

    # Apply formula-injection mitigation to every exported value, while
    # leaving the caller's application DataFrame unchanged.
    for column_name in export_dataframe.columns:
        export_dataframe[column_name] = export_dataframe[column_name].map(
            sanitize_csv_cell
        )

    try:
        export_dataframe.to_csv(
            temporary_file,
            index=False,
            encoding="utf-8",
        )

        # Set restrictive permissions on POSIX before replacement. Windows
        # access is instead governed by filesystem ACLs.
        if os.name != "nt":
            os.chmod(temporary_file, 0o600)

        os.replace(
            temporary_file,
            output_file,
        )

    finally:
        # On success os.replace() removes the temporary path; on a failure,
        # remove any incomplete temporary export.
        temporary_file.unlink(missing_ok=True)