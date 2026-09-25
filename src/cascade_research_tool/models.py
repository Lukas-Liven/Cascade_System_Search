"""
Typed data models shared between application services and user-interface code.

These models contain application data only. They must not contain Tkinter
widgets, tkinter.StringVar objects, threads, queues, or other UI runtime
objects. Keeping the models UI-independent makes them easier to test and
reuse during the ongoing refactor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class MappingStatistics:
    """
    Summary values produced by the Part 1 initialization and COMID-matching
    workflow and displayed to a researcher in the initialization interface.
    """

    total_nid_dams_after_basic_filter: int
    geo_connex_comid_mappings: int
    resnet_comid_mappings: int
    dams_matched_by_geoconnex: int
    dams_recovered_by_resnet: int
    total_dams_matched_to_network: int
    unmatched_dams: int
    match_rate_percent: float
    graph_nodes: int
    graph_edges: int


@dataclass
class InitializationResult:
    """
    In-memory state produced by the initialization workflow.

    Later workflow stages receive this object rather than independently
    reloading the NHD graph, NID inventory, GeoConnex records, or ResNet
    crosswalk. `Any` is retained for graph/node types because NetworkX graph
    node identifiers may vary across future NHD releases.
    """

    graph: Any
    node_to_comid: dict[Any, int]
    comid_to_node: dict[int, Any]
    dam_inventory: pd.DataFrame
    dam_inventory_matched: pd.DataFrame
    damid_to_comid: dict[str, int]
    damid_to_node: dict[str, Any]
    statistics: MappingStatistics

@dataclass
class CascadeConstructionResult:
    """
    Complete result produced by Part 5 cascade-system construction.

    This model contains analysis outputs and summary counts only. It does not
    contain Tkinter widgets, thread objects, queues, file paths, or UI state.
    The application worker persists and presents this result separately.
    """

    cascade_graphs: dict[str, Any]
    edge_dataframe: pd.DataFrame
    summary_dataframe: pd.DataFrame

    root_candidate_count: int
    covered_candidate_count: int
    true_root_count: int
    two_dam_chain_count: int
    mergeable_chain_count: int
    system_count: int
    edge_count: int
    multi_root_system_count: int
    cycle_count: int

@dataclass
class DamFilterResult:
    """
    Result of applying Part 3 dam-selection criteria.

    The result is intentionally independent of Tkinter. The UI/controller is
    responsible for presenting summary information and retaining the returned
    DataFrames in application state.
    """

    filtered_dam_inventory: pd.DataFrame
    filtered_dam_inventory_matched: pd.DataFrame

    after_storage_count: int
    after_purpose_count: int
    before_power_count: int

@dataclass
class CascadeQueryResult:
    """
    Result of evaluating Part 6 cascade-system query criteria.

    Query details use system IDs as keys. Each detail value identifies the
    dam nodes that satisfied enabled state, NID ID, and river/stream criteria.
    """

    matching_system_ids: list[str]
    matching_details: dict[str, dict[str, list[str]]]

@dataclass
class DownstreamLinkResult:
    """
    Result of constructing the Part 4 downstream-dam reference data.

    The result contains only application-analysis data and summary values.
    File persistence, worker-thread communication, and Tkinter presentation
    remain the responsibility of the application controller.
    """

    downstream_links: pd.DataFrame
    total_dams: int
    linked_dams: int
    duplicate_node_count: int