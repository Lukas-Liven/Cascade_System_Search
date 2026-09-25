"""
Cascade-system construction service.

This module implements the Part 5 analysis algorithm independently from the
Tkinter application. It converts Part 3 candidate dams and Part 4 direct
downstream links into connected directed cascade systems, output tables, and
NetworkX DiGraphs.

The service does not access widgets, worker queues, files, or dialogs.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import networkx as nx
import pandas as pd

from cascade_research_tool.models import CascadeConstructionResult
from cascade_research_tool.utilities.identifiers import normalize_identifier


def _has_hydroelectric_purpose(purposes: object) -> bool:
    """
    Return whether a dam-purpose value includes Hydroelectric.

    This uses the same case-insensitive semantics as the Part 3 filter while
    safely handling missing values.
    """

    return bool(
        pd.notna(purposes)
        and "hydroelectric" in str(purposes).casefold()
    )


def _normalize_missing_value(value: Any) -> Any:
    """
    Convert pandas scalar missing values to None for graph attributes.

    Some future source values could be non-scalar objects for which pd.isna()
    does not produce a simple Boolean. Preserve those values rather than
    causing graph construction to fail.
    """

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    return value


def construct_cascade_systems(
    matched_inventory: pd.DataFrame,
    filtered_dam_inventory_matched: pd.DataFrame,
    downstream_links: pd.DataFrame,
    minimum_hydroelectric_dams: int,
    require_same_owner: bool,
) -> CascadeConstructionResult:
    """
    Construct directed cascade systems from matched dams and direct links.

    Args:
        matched_inventory:
            Full Part 1 matched dam inventory. Each dam must have a unique
            normalized NID ID after duplicate removal.

        filtered_dam_inventory_matched:
            Part 3 selected matched dams. These serve as possible cascade
            roots.

        downstream_links:
            Part 4 direct dam-to-dam relationship table. Expected columns are
            Dam, Downstream Dam, and Distance (Miles).

        minimum_hydroelectric_dams:
            Minimum hydroelectric dams required in a final merged cascade
            system. This criterion is evaluated after convergent chains merge.

        require_same_owner:
            When true, each downstream dam in a root chain must have a known
            owner equal to the original root dam's owner.

    Returns:
        A CascadeConstructionResult containing constructed graphs, export
        DataFrames, and analysis counts.

    Raises:
        RuntimeError:
            If the filtered input contains no usable matched root candidates.

        ValueError:
            If the hydroelectric threshold is invalid.
    """

    if minimum_hydroelectric_dams < 0:
        raise ValueError(
            "Minimum hydroelectric dams must be greater than or equal to 0."
        )

    # Retain the first source row for each NID ID, matching current application
    # behavior. NID IDs are normalized to preserve state prefixes and leading
    # zeros consistently across Parts 1, 3, and 4.
    normalized_inventory = (
        matched_inventory.copy()
        .drop_duplicates(subset=["NID ID"], keep="first")
    )

    normalized_inventory["NID ID"] = normalized_inventory["NID ID"].map(
        normalize_identifier
    )

    normalized_inventory = normalized_inventory.dropna(
        subset=["NID ID"]
    ).copy()

    dam_records_by_id: dict[str, dict[str, Any]] = (
        normalized_inventory
        .set_index("NID ID", drop=False)
        .to_dict(orient="index")
    )

    # Build the direct Part 4 dam-to-dam mapping. Links are included only when
    # both endpoints remain valid members of the current Part 1 matched
    # inventory, preventing stale data from introducing unvalidated nodes.
    downstream_map: dict[str, str] = {}
    distance_by_edge: dict[tuple[str, str], Optional[float]] = {}

    for _, link_row in downstream_links.iterrows():
        upstream_dam_id = normalize_identifier(link_row.get("Dam"))
        downstream_dam_id = normalize_identifier(
            link_row.get("Downstream Dam")
        )

        if (
            upstream_dam_id is None
            or downstream_dam_id is None
            or upstream_dam_id not in dam_records_by_id
            or downstream_dam_id not in dam_records_by_id
        ):
            continue

        downstream_map[upstream_dam_id] = downstream_dam_id

        raw_distance = link_row.get("Distance (Miles)")

        try:
            distance = float(raw_distance)

            if not math.isfinite(distance) or distance < 0:
                distance = None
        except (TypeError, ValueError):
            distance = None

        distance_by_edge[
            (upstream_dam_id, downstream_dam_id)
        ] = distance

    # Part 3 defines potential upstream roots. A set prevents duplicate source
    # rows from creating duplicate chains or systems.
    root_candidate_ids = {
        normalized_dam_id
        for dam_id in filtered_dam_inventory_matched["NID ID"]
        if (
            (normalized_dam_id := normalize_identifier(dam_id))
            is not None
            and normalized_dam_id in dam_records_by_id
        )
    }

    if not root_candidate_ids:
        raise RuntimeError(
            "Part 3 produced no matched root candidates for cascade "
            "construction."
        )

    def get_owner(dam_id: str) -> Optional[str]:
        """Return a normalized owner name, or None if no usable value exists."""

        owner = dam_records_by_id[dam_id].get("Owner Names")

        if owner is None or pd.isna(owner):
            return None

        normalized_owner = str(owner).strip()

        return normalized_owner if normalized_owner else None

    def have_same_owner(
        root_dam_id: str,
        candidate_dam_id: str,
    ) -> bool:
        """
        Return whether the candidate dam has the known same owner as the root.

        Missing owner values do not satisfy same-owner mode because ownership
        cannot be affirmatively verified.
        """

        root_owner = get_owner(root_dam_id)
        candidate_owner = get_owner(candidate_dam_id)

        return (
            root_owner is not None
            and candidate_owner is not None
            and root_owner == candidate_owner
        )

    def is_hydroelectric_dam(dam_id: str) -> bool:
        """Return whether one matched dam has a hydroelectric purpose."""

        return _has_hydroelectric_purpose(
            dam_records_by_id[dam_id].get("Purposes")
        )

    def build_full_chain(
        start_dam_id: str,
    ) -> tuple[list[str], bool]:
        """
        Follow direct downstream links until a terminal, cycle, or boundary.

        Returns:
            - Ordered dam IDs from root to terminal dam.
            - Whether a malformed circular downstream relationship occurred.
        """

        chain = [start_dam_id]
        visited = {start_dam_id}
        current_dam_id = start_dam_id
        cycle_detected = False

        while True:
            downstream_dam_id = downstream_map.get(current_dam_id)

            if downstream_dam_id is None:
                break

            if downstream_dam_id in visited:
                cycle_detected = True
                break

            # Owner continuity is always evaluated against the initial root,
            # not merely against the preceding dam in the chain.
            if (
                require_same_owner
                and not have_same_owner(
                    start_dam_id,
                    downstream_dam_id,
                )
            ):
                break

            chain.append(downstream_dam_id)
            visited.add(downstream_dam_id)
            current_dam_id = downstream_dam_id

        return chain, cycle_detected

    # Build a chain from every Part 3 candidate root.
    candidate_chains: dict[str, list[str]] = {}
    cycle_count = 0

    for candidate_dam_id in sorted(root_candidate_ids):
        chain, cycle_detected = build_full_chain(candidate_dam_id)
        candidate_chains[candidate_dam_id] = chain

        if cycle_detected:
            cycle_count += 1

    # A candidate that occurs downstream in another selected candidate's chain
    # is not an upstream root of an independent initial cascade.
    covered_candidate_ids: set[str] = set()

    for chain in candidate_chains.values():
        for downstream_dam_id in chain[1:]:
            if downstream_dam_id in root_candidate_ids:
                covered_candidate_ids.add(downstream_dam_id)

    true_root_ids = sorted(
        root_candidate_ids - covered_candidate_ids
    )

    # Every preliminary chain requires at least two total dams. This universal
    # requirement remains independent of the configured hydroelectric count.
    initial_chains = [
        candidate_chains[root_dam_id]
        for root_dam_id in true_root_ids
        if len(candidate_chains[root_dam_id]) >= 2
    ]

    # Preserve all two-or-more-dam chains until after convergence-based merging.
    # This is essential: two converging chains may each contain fewer
    # hydroelectric dams than the threshold while their combined connected
    # system meets the system-level criterion.
    mergeable_chains = initial_chains

    # -----------------------------------------------------------------------
    # Union-find grouping of chains that share one or more dam nodes.
    # -----------------------------------------------------------------------

    parent: dict[str, str] = {}

    def dsu_find(dam_id: str) -> str:
        """Find a disjoint-set root, using path compression."""

        parent.setdefault(dam_id, dam_id)

        while parent[dam_id] != dam_id:
            parent[dam_id] = parent[parent[dam_id]]
            dam_id = parent[dam_id]

        return dam_id

    def dsu_union(
        dam_id_a: str,
        dam_id_b: str,
    ) -> None:
        """Merge two disjoint-set groups."""

        root_a = dsu_find(dam_id_a)
        root_b = dsu_find(dam_id_b)

        if root_a != root_b:
            parent[root_a] = root_b

    for chain in mergeable_chains:
        for dam_id in chain:
            dsu_find(dam_id)

        for upstream_dam_id, downstream_dam_id in zip(
            chain[:-1],
            chain[1:],
        ):
            dsu_union(
                upstream_dam_id,
                downstream_dam_id,
            )

    group_nodes: dict[str, set[str]] = {}
    group_edges: dict[str, set[tuple[str, str]]] = {}

    for chain in mergeable_chains:
        group_key = dsu_find(chain[0])

        group_nodes.setdefault(group_key, set()).update(chain)
        group_edges.setdefault(group_key, set()).update(
            zip(chain[:-1], chain[1:])
        )

    edge_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    cascade_graphs: dict[str, nx.DiGraph] = {}

    # Generate a validated directed graph and export rows for each connected
    # system formed by converging preliminary chains.
    for group_key in sorted(group_nodes):
        system_nodes = group_nodes[group_key]
        system_edges = group_edges[group_key]

        if len(system_nodes) < 2:
            continue

        incoming_edge_count = {
            dam_id: 0
            for dam_id in system_nodes
        }

        for _, downstream_dam_id in system_edges:
            incoming_edge_count[downstream_dam_id] += 1

        root_dams_in_system = sorted(
            dam_id
            for dam_id, count in incoming_edge_count.items()
            if count == 0
        )

        # A valid acyclic directed cascade must contain at least one root.
        if not root_dams_in_system:
            continue

        system_id = min(root_dams_in_system)

        # This is intentionally a final system-level validation. It applies
        # after all convergent chains have been merged.
        hydroelectric_count = sum(
            1
            for dam_id in system_nodes
            if is_hydroelectric_dam(dam_id)
        )

        if hydroelectric_count < minimum_hydroelectric_dams:
            continue

        cascade_graph = nx.DiGraph()

        for dam_id in sorted(system_nodes):
            dam_record = dam_records_by_id[dam_id]

            node_attributes = {
                key: _normalize_missing_value(value)
                for key, value in dam_record.items()
            }

            node_attributes["NID ID"] = dam_id
            node_attributes["name"] = dam_record.get("Dam Name")
            node_attributes["purposes"] = dam_record.get("Purposes")
            node_attributes["owner"] = dam_record.get("Owner Names")
            node_attributes["is_hydroelectric"] = (
                is_hydroelectric_dam(dam_id)
            )

            cascade_graph.add_node(
                dam_id,
                **node_attributes,
            )

        for upstream_dam_id, downstream_dam_id in sorted(system_edges):
            distance = distance_by_edge.get(
                (upstream_dam_id, downstream_dam_id)
            )

            cascade_graph.add_edge(
                upstream_dam_id,
                downstream_dam_id,
                distance_miles=distance,
            )

            upstream_record = dam_records_by_id[upstream_dam_id]
            downstream_record = dam_records_by_id[downstream_dam_id]

            edge_rows.append(
                {
                    "System ID": system_id,
                    "Upstream Dam ID": upstream_dam_id,
                    "Upstream Dam Name": upstream_record.get("Dam Name"),
                    "Upstream Dam Purposes": upstream_record.get("Purposes"),
                    "Upstream Dam Owner": upstream_record.get("Owner Names"),
                    "Downstream Dam ID": downstream_dam_id,
                    "Downstream Dam Name": downstream_record.get("Dam Name"),
                    "Downstream Dam Purposes": downstream_record.get(
                        "Purposes"
                    ),
                    "Downstream Dam Owner": downstream_record.get(
                        "Owner Names"
                    ),
                    "Distance (Miles)": (
                        round(distance, 2)
                        if distance is not None
                        else None
                    ),
                }
            )

        summary_rows.append(
            {
                "System ID": system_id,
                "Root Dam IDs": "; ".join(root_dams_in_system),
                "Root Dam Names": "; ".join(
                    str(
                        dam_records_by_id[dam_id].get(
                            "Dam Name",
                            dam_id,
                        )
                    )
                    for dam_id in root_dams_in_system
                ),
                "Total Dams": len(system_nodes),
                "Hydroelectric Dams": hydroelectric_count,
            }
        )

        cascade_graphs[system_id] = cascade_graph

    edge_dataframe = pd.DataFrame(
        edge_rows,
        columns=[
            "System ID",
            "Upstream Dam ID",
            "Upstream Dam Name",
            "Upstream Dam Purposes",
            "Upstream Dam Owner",
            "Downstream Dam ID",
            "Downstream Dam Name",
            "Downstream Dam Purposes",
            "Downstream Dam Owner",
            "Distance (Miles)",
        ],
    )

    summary_dataframe = pd.DataFrame(
        summary_rows,
        columns=[
            "System ID",
            "Root Dam IDs",
            "Root Dam Names",
            "Total Dams",
            "Hydroelectric Dams",
        ],
    )

    if not edge_dataframe.empty:
        edge_dataframe = edge_dataframe.sort_values(
            [
                "System ID",
                "Upstream Dam ID",
                "Downstream Dam ID",
            ],
            kind="stable",
        ).reset_index(drop=True)

    if not summary_dataframe.empty:
        summary_dataframe = summary_dataframe.sort_values(
            "System ID",
            kind="stable",
        ).reset_index(drop=True)

    multi_root_system_count = sum(
        1
        for graph in cascade_graphs.values()
        if sum(
            1
            for dam_id in graph.nodes
            if graph.in_degree(dam_id) == 0
        ) > 1
    )

    return CascadeConstructionResult(
        cascade_graphs=cascade_graphs,
        edge_dataframe=edge_dataframe,
        summary_dataframe=summary_dataframe,
        root_candidate_count=len(root_candidate_ids),
        covered_candidate_count=len(covered_candidate_ids),
        true_root_count=len(true_root_ids),
        two_dam_chain_count=len(initial_chains),
        mergeable_chain_count=len(mergeable_chains),
        system_count=len(cascade_graphs),
        edge_count=len(edge_dataframe),
        multi_root_system_count=multi_root_system_count,
        cycle_count=cycle_count,
    )