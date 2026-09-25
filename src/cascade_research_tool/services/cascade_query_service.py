"""
Part 6 cascade-system query service.

This module evaluates constructed NetworkX cascade graphs against state, NID
ID, and river/stream criteria. It contains no Tkinter controls, dialogs,
logging, map generation, or application state mutation.
"""

from __future__ import annotations

from typing import Any, Optional

import networkx as nx
import pandas as pd

from cascade_research_tool.models import CascadeQueryResult
from cascade_research_tool.utilities.identifiers import (
    normalize_identifier,
)


def get_node_attribute_case_insensitive(
    attributes: dict[str, Any],
    candidate_names: tuple[str, ...],
) -> Any:
    """
    Return one node attribute using case-insensitive candidate field names.

    NID source field capitalization can vary by source release. This helper
    prevents query behavior from depending on one exact source-column spelling.
    """

    normalized_attributes = {
        str(key).casefold(): value
        for key, value in attributes.items()
    }

    for candidate_name in candidate_names:
        value = normalized_attributes.get(
            candidate_name.casefold()
        )

        if value is not None:
            return value

    return None


def get_dam_state_code(
    dam_id: str,
    attributes: dict[str, Any],
) -> Optional[str]:
    """
    Resolve a two-letter state code for one cascade dam node.

    The NID State attribute is preferred. If absent or invalid, use the first
    two alphabetic characters of the NID ID as the application's compatibility
    fallback.
    """

    raw_state = get_node_attribute_case_insensitive(
        attributes,
        ("State",),
    )

    if raw_state is not None:
        state_text = str(raw_state).strip().upper()

        if len(state_text) == 2 and state_text.isalpha():
            return state_text

    normalized_dam_id = normalize_identifier(dam_id)

    if normalized_dam_id is not None:
        prefix = normalized_dam_id[:2].upper()

        if len(prefix) == 2 and prefix.isalpha():
            return prefix

    return None


def get_dam_river_name(
    attributes: dict[str, Any],
) -> Optional[str]:
    """
    Return a normalized dam river/stream name, if available.

    The authoritative NID field is ``River or Stream Name``. A case-insensitive
    lookup is used defensively for future source-column capitalization changes.
    """

    raw_river_name = get_node_attribute_case_insensitive(
        attributes,
        ("River or Stream Name",),
    )

    if raw_river_name is None:
        return None

    try:
        if pd.isna(raw_river_name):
            return None
    except (TypeError, ValueError):
        # Preserve normal string conversion for any non-scalar future value.
        pass

    river_name = str(raw_river_name).strip()

    return river_name if river_name else None


def system_root_dam_ids(
    cascade_graph: nx.DiGraph,
) -> list[str]:
    """
    Return stable sorted root dam IDs for one cascade system.
    """

    return sorted(
        str(dam_id)
        for dam_id in cascade_graph.nodes
        if cascade_graph.in_degree(dam_id) == 0
    )


def system_hydroelectric_dam_count(
    cascade_graph: nx.DiGraph,
) -> int:
    """
    Count hydroelectric dam nodes in one constructed cascade system.
    """

    return sum(
        1
        for _, attributes in cascade_graph.nodes(data=True)
        if bool(attributes.get("is_hydroelectric", False))
    )


def query_cascade_systems(
    cascade_graphs: dict[str, nx.DiGraph],
    requested_state: Optional[str] = None,
    requested_nid_id: Optional[str] = None,
    requested_river: Optional[str] = None,
) -> CascadeQueryResult:
    """
    Return systems satisfying all enabled Part 6 query criteria.

    A criterion is enabled when its argument is not None:

    - requested_state: Two-letter state code.
    - requested_nid_id: Exact NID ID match, case-insensitive.
    - requested_river: Case-insensitive partial river/stream-name match.

    Enabled criteria use AND logic at the system level. State and river
    criteria are existential: different dam nodes in the same connected system
    may satisfy different enabled criteria.

    Args:
        cascade_graphs:
            Constructed cascade graphs indexed by system ID.

        requested_state:
            Optional already-validated two-letter state code.

        requested_nid_id:
            Optional nonempty requested NID ID.

        requested_river:
            Optional nonempty river/stream query text.

    Raises:
        ValueError:
            If an enabled argument is malformed.
    """

    normalized_state: Optional[str] = None

    if requested_state is not None:
        normalized_state = requested_state.strip().upper()

        if (
            len(normalized_state) != 2
            or not normalized_state.isalpha()
        ):
            raise ValueError(
                "State search requires a two-letter state abbreviation."
            )

    normalized_nid_id: Optional[str] = None

    if requested_nid_id is not None:
        normalized_nid_id = requested_nid_id.strip().casefold()

        if not normalized_nid_id:
            raise ValueError(
                "NID ID query text cannot be blank when enabled."
            )

    normalized_river: Optional[str] = None

    if requested_river is not None:
        normalized_river = requested_river.strip().casefold()

        if not normalized_river:
            raise ValueError(
                "River/stream query text cannot be blank when enabled."
            )

    matching_system_ids: list[str] = []
    matching_details: dict[str, dict[str, list[str]]] = {}

    for system_id in sorted(cascade_graphs):
        cascade_graph = cascade_graphs[system_id]

        state_matching_dams: list[str] = []
        nid_matching_dams: list[str] = []
        river_matching_dams: list[str] = []

        for dam_id, attributes in cascade_graph.nodes(data=True):
            dam_id_text = str(dam_id)
            normalized_dam_id = normalize_identifier(dam_id)

            if (
                normalized_state is not None
                and get_dam_state_code(
                    dam_id_text,
                    attributes,
                ) == normalized_state
            ):
                state_matching_dams.append(dam_id_text)

            if (
                normalized_nid_id is not None
                and normalized_dam_id is not None
                and normalized_dam_id.casefold()
                == normalized_nid_id
            ):
                nid_matching_dams.append(dam_id_text)

            if normalized_river is not None:
                river_name = get_dam_river_name(attributes)

                if (
                    river_name is not None
                    and normalized_river in river_name.casefold()
                ):
                    river_matching_dams.append(
                        f"{dam_id_text} ({river_name})"
                    )

        matches_state = (
            normalized_state is None
            or bool(state_matching_dams)
        )
        matches_nid = (
            normalized_nid_id is None
            or bool(nid_matching_dams)
        )
        matches_river = (
            normalized_river is None
            or bool(river_matching_dams)
        )

        if matches_state and matches_nid and matches_river:
            matching_system_ids.append(system_id)

            matching_details[system_id] = {
                "state": sorted(state_matching_dams),
                "nid": sorted(nid_matching_dams),
                "river": sorted(river_matching_dams),
            }

    return CascadeQueryResult(
        matching_system_ids=matching_system_ids,
        matching_details=matching_details,
    )