"""
Part 3 dam filtering service.

This module contains the data-processing portion of the dam filter workflow.
It does not access Tkinter widgets, application state, dialogs, logs, or
worker threads.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from cascade_research_tool.constants import POWER_CONVERSION_FACTOR
from cascade_research_tool.models import DamFilterResult


def has_hydroelectric_purpose(
    purposes: Any,
) -> bool:
    """
    Return whether a purpose value contains the word Hydroelectric.

    This implements the same case-insensitive behavior as:

        purposes.str.contains("Hydroelectric", case=False, na=False)

    while safely supporting one value at a time.
    """

    return bool(
        pd.notna(purposes)
        and "hydroelectric" in str(purposes).casefold()
    )


def apply_dam_filters(
    dam_inventory: pd.DataFrame,
    dam_inventory_matched: pd.DataFrame,
    minimum_storage: float,
    hydroelectric_only: bool,
    power_threshold: float | None,
) -> DamFilterResult:
    """
    Apply Part 3 storage, purpose, mapping, and optional power filters.

    Args:
        dam_inventory:
            Part 1 primary-dam inventory. This source inventory is never
            modified.

        dam_inventory_matched:
            Part 1 inventory rows that were successfully mapped to NHD nodes.

        minimum_storage:
            Strict lower maximum-storage threshold in acre-feet.

        hydroelectric_only:
            If true, retain only dams whose purposes mention Hydroelectric.

        power_threshold:
            Strict estimated-power lower threshold in MW. It must be supplied
            when hydroelectric_only is true and is ignored otherwise.

    Returns:
        DamFilterResult with filtered inventory DataFrames and intermediate
        counts used by the Part 3 UI and application log.

    Raises:
        ValueError:
            If mandatory DataFrame columns are unavailable or filtering
            configuration is internally inconsistent.
    """

    required_inventory_columns = {
        "NID ID",
        "Max Storage (Acre-Ft)",
        "Purposes",
    }
    missing_inventory_columns = (
        required_inventory_columns - set(dam_inventory.columns)
    )

    if missing_inventory_columns:
        raise ValueError(
            "Dam inventory is missing required filtering columns: "
            f"{sorted(missing_inventory_columns)}"
        )

    required_matched_columns = {
        "NID ID",
        "node_id",
    }
    missing_matched_columns = (
        required_matched_columns - set(dam_inventory_matched.columns)
    )

    if missing_matched_columns:
        raise ValueError(
            "Matched dam inventory is missing required mapping columns: "
            f"{sorted(missing_matched_columns)}"
        )

    if hydroelectric_only:
        if power_threshold is None:
            raise ValueError(
                "A power threshold is required when hydroelectric filtering "
                "is enabled."
            )

        required_power_columns = {
            "Hydraulic Height (Ft)",
            "Max Discharge (Cubic Ft/Second)",
        }
        missing_power_columns = (
            required_power_columns - set(dam_inventory.columns)
        )

        if missing_power_columns:
            raise ValueError(
                "Dam inventory is missing required estimated-power columns: "
                f"{sorted(missing_power_columns)}"
            )

    # Preserve the Part 1 inventory so a researcher can revise criteria
    # repeatedly without cumulative filtering effects.
    filtered_inventory = dam_inventory.copy()

    # Retain the original strict greater-than storage behavior.
    filtered_inventory = filtered_inventory[
        filtered_inventory["Max Storage (Acre-Ft)"] > minimum_storage
    ].copy()

    after_storage_count = len(filtered_inventory)

    if hydroelectric_only:
        filtered_inventory = filtered_inventory[
            filtered_inventory["Purposes"].map(
                has_hydroelectric_purpose
            )
        ].copy()

    after_purpose_count = len(filtered_inventory)

    # Join selected inventory records to their NHD node IDs. The Part 1
    # inventory is authoritative for dam attributes; the matched inventory is
    # authoritative only for node mapping.
    matched_node_data = (
        dam_inventory_matched[
            ["NID ID", "node_id"]
        ]
        .drop_duplicates(
            subset=["NID ID"],
            keep="first",
        )
        .copy()
    )

    filtered_matched = filtered_inventory.merge(
        matched_node_data,
        on="NID ID",
        how="inner",
        suffixes=("", "_matched"),
    )

    # The Part 1 inventory may already carry node_id from initialization.
    # Preserve one explicit node_id field for downstream stages.
    if "node_id_matched" in filtered_matched.columns:
        filtered_matched["node_id"] = (
            filtered_matched["node_id_matched"]
        )
        filtered_matched = filtered_matched.drop(
            columns=["node_id_matched"]
        )

    before_power_count = len(filtered_matched)

    if hydroelectric_only and power_threshold is not None:
        hydraulic_height = pd.to_numeric(
            filtered_matched["Hydraulic Height (Ft)"],
            errors="coerce",
        )
        maximum_discharge = pd.to_numeric(
            filtered_matched["Max Discharge (Cubic Ft/Second)"],
            errors="coerce",
        )

        filtered_matched["estimated_power_capacity"] = (
            hydraulic_height
            * maximum_discharge
            / POWER_CONVERSION_FACTOR
        )

        filtered_matched = filtered_matched[
            filtered_matched["estimated_power_capacity"]
            > power_threshold
        ].copy()

        # Keep inventory-level output consistent with power-qualified,
        # network-matched hydroelectric root candidates.
        qualified_ids = set(
            filtered_matched["NID ID"]
        )

        filtered_inventory = filtered_inventory[
            filtered_inventory["NID ID"].isin(qualified_ids)
        ].copy()

    return DamFilterResult(
        filtered_dam_inventory=filtered_inventory,
        filtered_dam_inventory_matched=filtered_matched,
        after_storage_count=after_storage_count,
        after_purpose_count=after_purpose_count,
        before_power_count=before_power_count,
    )