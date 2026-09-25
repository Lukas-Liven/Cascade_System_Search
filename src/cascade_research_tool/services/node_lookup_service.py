"""
Part 2 dam and NHD-network node lookup service.

This module provides DataFrame lookup logic used by the node-explorer tab.
It does not use Tkinter widgets, dialogs, logging, network access, or cache
artifacts.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

def node_id_matches(
    stored_node_id: Any,
    requested_node_id: int,
) -> bool:
    """
    Compare a stored node ID with a researcher-entered integer node ID.

    Pandas can store integer NHD node IDs as floats when the source column has
    contained missing values. For example, graph node 123 can appear as 123.0.
    This helper treats integer-equivalent values as equal while rejecting
    missing, invalid, and fractional values.
    """

    if pd.isna(stored_node_id):
        return False

    try:
        numeric_node_id = float(stored_node_id)

        if not numeric_node_id.is_integer():
            return False

        return int(numeric_node_id) == requested_node_id

    except (TypeError, ValueError):
        return False

def find_matched_dams_at_node(
    dam_inventory_matched: pd.DataFrame,
    requested_node_id: int,
) -> pd.DataFrame:
    """
    Return matched NID dam rows assigned to one requested NHD node ID.

    The returned DataFrame is a filtered view/copy-compatible result from the
    supplied matched inventory. The caller is responsible for presentation.
    """

    if "node_id" not in dam_inventory_matched.columns:
        raise ValueError(
            "Matched dam inventory does not contain the required node_id "
            "column."
        )

    return dam_inventory_matched[
        dam_inventory_matched["node_id"].map(
            lambda stored_node_id: node_id_matches(
                stored_node_id,
                requested_node_id,
            )
        )
    ]

def find_matched_dams_by_nid(
    dam_inventory_matched: pd.DataFrame,
    requested_nid_id: str,
) -> pd.DataFrame:
    """
    Return matched dam rows for an exact normalized NID ID lookup.

    This preserves the existing application behavior: source NID IDs are
    converted to strings and trimmed before an exact comparison. The UI
    currently treats NID IDs as case-preserving identifiers.
    """

    if "NID ID" not in dam_inventory_matched.columns:
        raise ValueError(
            "Matched dam inventory does not contain the required NID ID "
            "column."
        )

    return dam_inventory_matched[
        dam_inventory_matched["NID ID"].astype(str).str.strip()
        == requested_nid_id
    ]