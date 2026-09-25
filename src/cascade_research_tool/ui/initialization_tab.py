"""
Part 1 initialization-tab user interface.

This module owns only Tkinter widget construction and presentation for the
initialization workflow. It does not start worker threads, download data,
access application state, or invoke services directly.
"""

from __future__ import annotations

from collections.abc import Callable

import tkinter as tk
from tkinter import ttk

from cascade_research_tool.models import MappingStatistics


class InitializationTab:
    """
    Tkinter view for Part 1 data initialization and mapping statistics.

    The controller supplies StringVar instances and callbacks so this view
    remains independent of the main application class.
    """

    def __init__(
        self,
        parent_notebook: ttk.Notebook,
        cache_path_text: tk.StringVar,
        status_text: tk.StringVar,
        progress_text: tk.StringVar,
        start_initialization_callback: Callable[[], None],
    ) -> None:
        """
        Create the initialization tab and all Part 1 widgets.

        Args:
            parent_notebook:
                The application's main workflow notebook.

            cache_path_text:
                Controller-owned text variable containing the local cache path.

            status_text:
                Controller-owned text variable for initialization status.

            progress_text:
                Controller-owned text variable describing current activity.

            start_initialization_callback:
                Controller callback invoked when the user selects the
                initialization button.
        """

        self.frame = ttk.Frame(
            parent_notebook,
            padding=12,
        )

        self.initialize_button: ttk.Button
        self.stats_tree: ttk.Treeview

        self._build_interface(
            cache_path_text=cache_path_text,
            status_text=status_text,
            progress_text=progress_text,
            start_initialization_callback=start_initialization_callback,
        )

    def _build_interface(
        self,
        cache_path_text: tk.StringVar,
        status_text: tk.StringVar,
        progress_text: tk.StringVar,
        start_initialization_callback: Callable[[], None],
    ) -> None:
        """Build the static Part 1 widget layout."""

        ttk.Label(
            self.frame,
            text="Part 1: Data Initialization and COMID Mapping",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.frame,
            text=(
                "Download or load cached NHD, GeoConnex, ResNet, and NID data. "
                "Then map NID dam records to nodes in the NHD river network."
            ),
            wraplength=950,
        ).pack(
            anchor=tk.W,
            pady=(4, 12),
        )

        cache_frame = ttk.LabelFrame(
            self.frame,
            text="Local Cache",
            padding=10,
        )
        cache_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Label(
            cache_frame,
            text="Cache directory:",
        ).grid(
            row=0,
            column=0,
            sticky=tk.NW,
            padx=(0, 8),
        )

        ttk.Label(
            cache_frame,
            textvariable=cache_path_text,
            wraplength=780,
        ).grid(
            row=0,
            column=1,
            sticky=tk.W,
        )

        controls = ttk.Frame(self.frame)
        controls.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        self.initialize_button = ttk.Button(
            controls,
            text="Initialize / Refresh Required Data",
            command=start_initialization_callback,
        )
        self.initialize_button.pack(side=tk.LEFT)

        ttk.Label(
            controls,
            textvariable=status_text,
            foreground="#1f4e79",
        ).pack(
            side=tk.LEFT,
            padx=(16, 0),
        )

        progress_frame = ttk.LabelFrame(
            self.frame,
            text="Current Activity",
            padding=10,
        )
        progress_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Label(
            progress_frame,
            textvariable=progress_text,
            wraplength=950,
        ).pack(anchor=tk.W)

        stats_frame = ttk.LabelFrame(
            self.frame,
            text="NHD / COMID Mapping Statistics",
            padding=10,
        )
        stats_frame.pack(
            fill=tk.BOTH,
            expand=True,
        )

        self.stats_tree = ttk.Treeview(
            stats_frame,
            columns=("metric", "value"),
            show="headings",
            height=10,
        )
        self.stats_tree.heading(
            "metric",
            text="Metric",
        )
        self.stats_tree.heading(
            "value",
            text="Value",
        )
        self.stats_tree.column(
            "metric",
            width=550,
            anchor=tk.W,
        )
        self.stats_tree.column(
            "value",
            width=250,
            anchor=tk.E,
        )
        self.stats_tree.pack(
            fill=tk.BOTH,
            expand=True,
        )

    def set_initialize_button_enabled(
        self,
        enabled: bool,
    ) -> None:
        """Enable or disable the initialization action button."""

        self.initialize_button.configure(
            state=tk.NORMAL if enabled else tk.DISABLED
        )

    def display_statistics(
        self,
        statistics: MappingStatistics,
    ) -> None:
        """Replace the displayed Part 1 mapping statistics."""

        for item_id in self.stats_tree.get_children():
            self.stats_tree.delete(item_id)

        metric_rows = [
            (
                "NHD river-network nodes",
                f"{statistics.graph_nodes:,}",
            ),
            (
                "NHD river-network edges",
                f"{statistics.graph_edges:,}",
            ),
            (
                "NID dams after required-field and primary-dam filters",
                f"{statistics.total_nid_dams_after_basic_filter:,}",
            ),
            (
                "GeoConnex provider ID → COMID mappings",
                f"{statistics.geo_connex_comid_mappings:,}",
            ),
            (
                "ResNet NID ID → COMID mappings",
                f"{statistics.resnet_comid_mappings:,}",
            ),
            (
                "Dams matched to NHD through GeoConnex",
                f"{statistics.dams_matched_by_geoconnex:,}",
            ),
            (
                "Additional dams recovered through ResNet",
                f"{statistics.dams_recovered_by_resnet:,}",
            ),
            (
                "Total NID dams matched to an NHD node",
                f"{statistics.total_dams_matched_to_network:,}",
            ),
            (
                "Unmatched NID dams",
                f"{statistics.unmatched_dams:,}",
            ),
            (
                "Final NID-to-network match rate",
                f"{statistics.match_rate_percent:.1f}%",
            ),
        ]

        for metric, value in metric_rows:
            self.stats_tree.insert(
                "",
                tk.END,
                values=(metric, value),
            )