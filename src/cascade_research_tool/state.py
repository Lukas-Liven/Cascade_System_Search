"""
Application session-state model.

This module contains the current research data and workflow outputs for one
running Cascade Research Tool session. It intentionally excludes Tkinter
widgets, tkinter variables, worker threads, queues, and other UI runtime
objects.

The Tkinter controller owns presentation and orchestration. Services receive
plain inputs and return results. This state model holds the controller's
current data between workflow stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import networkx as nx
import pandas as pd

from cascade_research_tool.models import InitializationResult


@dataclass
class ApplicationState:
    """
    Mutable in-memory state for the active application session.

    Part 1 initialization data are shared baseline data. Parts 3 through 6
    contain researcher-specific derived data that may be reset when beginning
    or loading a different named case study.
    """

    # -----------------------------------------------------------------------
    # Active named case-study identity
    # -----------------------------------------------------------------------

    current_study_name: Optional[str] = None
    current_study_file: Optional[Path] = None
    current_initialization_fingerprint: Optional[str] = None

    # -----------------------------------------------------------------------
    # Part 1: shared initialized baseline data
    # -----------------------------------------------------------------------

    initialization_result: Optional[InitializationResult] = None

    # -----------------------------------------------------------------------
    # Part 3: filtered candidate dams
    # -----------------------------------------------------------------------

    filtered_dam_inventory: Optional[pd.DataFrame] = None
    filtered_dam_inventory_matched: Optional[pd.DataFrame] = None

    # -----------------------------------------------------------------------
    # Part 4: direct downstream-dam reference data
    # -----------------------------------------------------------------------

    downstream_links: Optional[pd.DataFrame] = None
    downstream_links_cache_file: Optional[Path] = None

    # -----------------------------------------------------------------------
    # Part 5: constructed cascade systems
    # -----------------------------------------------------------------------

    cascade_graphs: dict[str, nx.DiGraph] = field(
        default_factory=dict
    )
    cascade_systems_edges: Optional[pd.DataFrame] = None
    cascade_systems_summary: Optional[pd.DataFrame] = None
    cascade_systems_cache_file: Optional[Path] = None

    # -----------------------------------------------------------------------
    # Part 6: latest cascade-system query
    # -----------------------------------------------------------------------

    last_cascade_query_system_ids: list[str] = field(
        default_factory=list
    )

    def reset_study_derived_data(self) -> None:
        """
        Reset Part 3 through Part 6 data for the active in-memory study.

        Part 1 initialization is intentionally preserved because NHD, NID,
        GeoConnex, and ResNet data are shared baseline context rather than
        case-study-specific outputs.
        """

        self.filtered_dam_inventory = None
        self.filtered_dam_inventory_matched = None

        self.downstream_links = None
        self.downstream_links_cache_file = None

        self.cascade_graphs = {}
        self.cascade_systems_edges = None
        self.cascade_systems_summary = None
        self.cascade_systems_cache_file = None

        self.last_cascade_query_system_ids = []