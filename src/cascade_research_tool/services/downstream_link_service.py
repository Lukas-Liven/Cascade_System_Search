"""
Part 4 downstream dam-link construction service.

This module transforms the initialized matched dam inventory and NHD river
network into the reusable direct downstream-dam reference table used by
cascade construction.

The service performs no Tkinter operations, file writes, artifact persistence,
or logging. The caller may supply an optional progress callback for worker
thread progress reporting.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import pandas as pd

from cascade_research_tool.models import DownstreamLinkResult
from cascade_research_tool.services.downstream_service import (
    find_downstream_dam,
)
from cascade_research_tool.utilities.identifiers import (
    normalize_identifier,
)


def _normalize_graph_node_id(
    node_id: Any,
) -> Any:
    """
    Normalize pandas-stored integer-like graph node IDs.

    Pandas can represent graph IDs as floats when a column previously included
    missing values. Python considers integer ``123`` and float ``123.0``
    equivalent dictionary/set keys, but converting integer-valued floats keeps
    graph lookups explicit and consistent.
    """

    if pd.isna(node_id):
        return None

    if isinstance(node_id, float) and node_id.is_integer():
        return int(node_id)

    return node_id


def build_downstream_links(
    graph: Any,
    matched_dams: pd.DataFrame,
    maximum_distance: float,
    progress_callback: Optional[
        Callable[[int, int], None]
    ] = None,
) -> DownstreamLinkResult:
    """
    Build the Part 4 direct downstream-dam reference table.

    Args:
        graph:
            Initialized NHD directed river network.

        matched_dams:
            Part 1 matched primary-dam inventory. Required columns are
            ``NID ID`` and ``node_id``. Dam Name and Purposes are included in
            output when present.

        maximum_distance:
            Maximum downstream river distance in miles.

        progress_callback:
            Optional callback invoked periodically with
            ``(processed_dams, total_dams)``. The callback must not update
            Tkinter widgets directly when invoked from a worker thread.

    Returns:
        A DownstreamLinkResult with the complete sorted Part 4 DataFrame and
        summary counts.

    Raises:
        ValueError:
            If required matched-dam columns are unavailable.

        RuntimeError:
            If no usable matched dams remain after node-ID normalization.
    """

    required_columns = {
        "NID ID",
        "node_id",
    }
    missing_columns = required_columns - set(matched_dams.columns)

    if missing_columns:
        raise ValueError(
            "Matched dam inventory is missing required Part 4 columns: "
            f"{sorted(missing_columns)}"
        )

    normalized_dams = matched_dams.copy()

    normalized_dams["node_id"] = normalized_dams["node_id"].map(
        _normalize_graph_node_id
    )

    normalized_dams = normalized_dams.dropna(
        subset=["node_id"]
    ).copy()

    if normalized_dams.empty:
        raise RuntimeError(
            "No usable matched dams remain after graph-node normalization."
        )

    # Map each NHD node to all matched NID dam IDs at that node. A target node
    # containing multiple dams uses the stable alphanumerically first NID ID
    # as the direct downstream reference.
    node_to_dam_ids: dict[Any, list[str]] = {}

    for _, dam_row in normalized_dams.iterrows():
        node_id = dam_row["node_id"]
        dam_id = normalize_identifier(
            dam_row.get("NID ID")
        )

        if node_id is not None and dam_id is not None:
            node_to_dam_ids.setdefault(
                node_id,
                [],
            ).append(dam_id)

    for dam_ids in node_to_dam_ids.values():
        dam_ids.sort()

    dam_nodes = set(node_to_dam_ids)

    matched_by_nid = (
        normalized_dams
        .drop_duplicates(
            subset=["NID ID"],
            keep="first",
        )
        .set_index(
            "NID ID",
            drop=False,
        )
    )

    duplicate_node_count = sum(
        1
        for dam_ids in node_to_dam_ids.values()
        if len(dam_ids) > 1
    )

    total_dams = len(normalized_dams)
    result_rows: list[dict[str, Any]] = []

    for index, (_, dam_row) in enumerate(
        normalized_dams.iterrows(),
        start=1,
    ):
        upstream_node = dam_row["node_id"]
        upstream_dam_id = normalize_identifier(
            dam_row.get("NID ID")
        )

        downstream_node, distance_miles = find_downstream_dam(
            graph=graph,
            start_node=upstream_node,
            dam_nodes=dam_nodes,
            max_distance=maximum_distance,
        )

        downstream_dam_id: Optional[str] = None
        downstream_dam_name: Optional[str] = None
        downstream_dam_purposes: Optional[str] = None

        if downstream_node is not None:
            target_dam_ids = node_to_dam_ids.get(
                downstream_node,
                [],
            )

            if target_dam_ids:
                downstream_dam_id = target_dam_ids[0]

                if downstream_dam_id in matched_by_nid.index:
                    downstream_record = matched_by_nid.loc[
                        downstream_dam_id
                    ]

                    downstream_dam_name = downstream_record.get(
                        "Dam Name"
                    )
                    downstream_dam_purposes = downstream_record.get(
                        "Purposes"
                    )

        result_rows.append(
            {
                "Dam": upstream_dam_id,
                "Dam Name": dam_row.get("Dam Name"),
                "Purposes": dam_row.get("Purposes"),
                "Downstream Dam": downstream_dam_id,
                "Downstream Dam Name": downstream_dam_name,
                "Downstream Dam Purposes": (
                    downstream_dam_purposes
                ),
                "Distance (Miles)": (
                    round(distance_miles, 2)
                    if distance_miles is not None
                    else None
                ),
            }
        )

        # Preserve the current progress cadence of 500 dams, without requiring
        # the service to know about elapsed time, queue processing, or UI.
        if (
            progress_callback is not None
            and (index % 500 == 0 or index == total_dams)
        ):
            progress_callback(
                index,
                total_dams,
            )

    downstream_links = pd.DataFrame(
        result_rows,
        columns=[
            "Dam",
            "Dam Name",
            "Purposes",
            "Downstream Dam",
            "Downstream Dam Name",
            "Downstream Dam Purposes",
            "Distance (Miles)",
        ],
    )

    downstream_links = downstream_links.sort_values(
        "Dam",
        kind="stable",
    ).reset_index(drop=True)

    linked_dams = int(
        downstream_links["Downstream Dam"].notna().sum()
    )

    return DownstreamLinkResult(
        downstream_links=downstream_links,
        total_dams=total_dams,
        linked_dams=linked_dams,
        duplicate_node_count=duplicate_node_count,
    )