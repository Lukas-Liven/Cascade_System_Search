"""
Downstream dam-link search service.

This module contains the graph-traversal logic used by Part 4 to locate the
first reachable downstream dam node within a configured river-distance limit.

The service has no Tkinter dependencies and does not read/write files. The UI
and worker code provide the graph, dam-node set, and configured search limit.
"""

from __future__ import annotations

import heapq
import itertools
import math
from typing import Any, Optional

from cascade_research_tool.constants import (
    DEFAULT_MAX_DISTANCE_MILES,
    KM_TO_MILES,
    LENGTH_ATTR,
)


def get_edge_length_km(
    graph: Any,
    upstream_node: Any,
    downstream_node: Any,
    length_attr: str = LENGTH_ATTR,
) -> float:
    """
    Retrieve a nonnegative downstream edge length in kilometers.

    The enhanced NHD graph is normally a NetworkX DiGraph. MultiDiGraph
    support is retained defensively in case a future source represents
    parallel flowline edges between the same graph nodes.

    Missing, nonnumeric, negative, NaN, and infinite lengths are treated as
    zero. This preserves the original tolerant missing-length behavior while
    preventing invalid values from entering route-distance calculations.
    """

    edge_data = graph.get_edge_data(
        upstream_node,
        downstream_node,
        default={},
    )

    # A NetworkX MultiDiGraph normally stores edge data as:
    #
    #     {edge_key: {attribute_name: value, ...}, ...}
    #
    # When multiple parallel flowline edges exist, use the shortest valid
    # segment as the defensive route-length value.
    if graph.is_multigraph():
        candidate_lengths: list[float] = []

        for attributes in edge_data.values():
            raw_length = attributes.get(length_attr, 0)

            try:
                length = float(raw_length or 0)
            except (TypeError, ValueError):
                length = 0.0

            if math.isfinite(length) and length >= 0:
                candidate_lengths.append(length)

        return min(candidate_lengths) if candidate_lengths else 0.0

    raw_length = edge_data.get(length_attr, 0)

    try:
        length = float(raw_length or 0)
    except (TypeError, ValueError):
        length = 0.0

    return length if math.isfinite(length) and length >= 0 else 0.0


def find_downstream_dam(
    graph: Any,
    start_node: Any,
    dam_nodes: set[Any],
    length_attr: str = LENGTH_ATTR,
    max_distance: float = DEFAULT_MAX_DISTANCE_MILES,
) -> tuple[Optional[Any], Optional[float]]:
    """
    Find the nearest reachable downstream dam node within a distance limit.

    In the current NHD network model, each node has at most one downstream
    successor and paths converge rather than split. The priority-queue
    implementation is intentionally retained from the existing application as
    defensive support for future graph representations.

    The starting node is not evaluated as its own downstream dam. The result
    is either:

    - a downstream NHD node ID plus cumulative river miles; or
    - ``(None, None)`` when no dam is reachable inside the configured limit.
    """

    if start_node not in graph:
        return None, None

    if not math.isfinite(max_distance) or max_distance < 0:
        raise ValueError(
            "Maximum downstream distance must be a nonnegative finite number."
        )

    # Include a monotonic sequence value so equal-distance queue entries do
    # not require Python to compare potentially heterogeneous NHD node IDs.
    sequence = itertools.count()

    search_queue: list[tuple[float, int, Any]] = [
        (
            0.0,
            next(sequence),
            start_node,
        )
    ]
    best_distance_by_node: dict[Any, float] = {
        start_node: 0.0,
    }

    while search_queue:
        distance_so_far, _, current_node = heapq.heappop(
            search_queue
        )

        # Ignore stale queue entries when a shorter route to the same node was
        # discovered after this entry was added.
        if distance_so_far > best_distance_by_node.get(
            current_node,
            float("inf"),
        ):
            continue

        for downstream_node in graph.successors(current_node):
            segment_km = get_edge_length_km(
                graph,
                current_node,
                downstream_node,
                length_attr,
            )

            downstream_distance = (
                distance_so_far
                + (segment_km * KM_TO_MILES)
            )

            if downstream_distance > max_distance:
                continue

            # A dam's downstream dam must be another dam location. The
            # starting node is deliberately not checked before traversal.
            if downstream_node in dam_nodes:
                return downstream_node, downstream_distance

            previous_best = best_distance_by_node.get(
                downstream_node
            )

            if (
                previous_best is None
                or downstream_distance < previous_best
            ):
                best_distance_by_node[
                    downstream_node
                ] = downstream_distance

                heapq.heappush(
                    search_queue,
                    (
                        downstream_distance,
                        next(sequence),
                        downstream_node,
                    ),
                )

    return None, None