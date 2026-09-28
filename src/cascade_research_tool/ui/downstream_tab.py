"""
Part 4 downstream-search user interface.

This module owns Tkinter widget construction and result presentation for the
direct downstream-dam reference workflow. It does not perform graph traversal,
artifact persistence, worker-thread coordination, or CSV export itself.
"""

from __future__ import annotations

from collections.abc import Callable

import tkinter as tk
from tkinter import ttk


class DownstreamTab:
    """
    Tkinter view for Part 4 downstream-dam search controls and results.
    """

    def __init__(
        self,
        parent_notebook: ttk.Notebook,
        maximum_distance_text: tk.StringVar,
        search_status_text: tk.StringVar,
        artifact_path_text: tk.StringVar,
        start_search_callback: Callable[[], None],
        export_csv_callback: Callable[[], None],
    ) -> None:
        """
        Create the Part 4 tab and its controls.

        The controller retains ownership of StringVar values because it reads
        the configured maximum distance and updates status/path text from
        worker-message processing.
        """

        self.frame = ttk.Frame(
            parent_notebook,
            padding=12,
        )

        self.run_search_button: ttk.Button
        self.export_links_button: ttk.Button
        self.results_tree: ttk.Treeview

        self._build_interface(
            maximum_distance_text=maximum_distance_text,
            search_status_text=search_status_text,
            artifact_path_text=artifact_path_text,
            start_search_callback=start_search_callback,
            export_csv_callback=export_csv_callback,
        )

    def _build_interface(
        self,
        maximum_distance_text: tk.StringVar,
        search_status_text: tk.StringVar,
        artifact_path_text: tk.StringVar,
        start_search_callback: Callable[[], None],
        export_csv_callback: Callable[[], None],
    ) -> None:
        """Build the static Part 4 widget layout."""

        ttk.Label(
            self.frame,
            text="Part 4: Search for Downstream Dams",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.frame,
            text=(
                "Search the enhanced NHD river network downstream from every "
                "matched primary dam. The first reachable dam within the "
                "selected river-distance limit is recorded as that dam's "
                "direct downstream link. Results are saved as reusable "
                "application-managed reference data."
            ),
            wraplength=950,
        ).pack(
            anchor=tk.W,
            pady=(4, 14),
        )

        search_settings_frame = ttk.LabelFrame(
            self.frame,
            text="Search Settings",
            padding=12,
        )
        search_settings_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Label(
            search_settings_frame,
            text="Maximum downstream search distance (river miles):",
        ).grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
        )

        maximum_distance_entry = ttk.Entry(
            search_settings_frame,
            textvariable=maximum_distance_text,
            width=15,
        )
        maximum_distance_entry.grid(
            row=0,
            column=1,
            sticky=tk.W,
        )

        ttk.Label(
            search_settings_frame,
            text=(
                "Only downstream dam nodes reached within this limit are "
                "recorded."
            ),
            wraplength=520,
        ).grid(
            row=0,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
        )

        search_settings_frame.columnconfigure(
            2,
            weight=1,
        )

        action_frame = ttk.Frame(self.frame)
        action_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        self.run_search_button = ttk.Button(
            action_frame,
            text="Build Downstream Dam Reference",
            command=start_search_callback,
        )
        self.run_search_button.pack(side=tk.LEFT)

        self.export_links_button = ttk.Button(
            action_frame,
            text="Export Downstream Links CSV",
            command=export_csv_callback,
            state=tk.DISABLED,
        )
        self.export_links_button.pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        ttk.Label(
            action_frame,
            textvariable=search_status_text,
            foreground="#1f4e79",
        ).pack(
            side=tk.LEFT,
            padx=(16, 0),
        )

        output_frame = ttk.LabelFrame(
            self.frame,
            text="Application-Managed Reference Data",
            padding=12,
        )
        output_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Label(
            output_frame,
            text="Runtime/cache artifact:",
        ).grid(
            row=0,
            column=0,
            sticky=tk.NW,
            padx=(0, 8),
        )

        ttk.Label(
            output_frame,
            textvariable=artifact_path_text,
            wraplength=780,
        ).grid(
            row=0,
            column=1,
            sticky=tk.W,
        )

        results_frame = ttk.LabelFrame(
            self.frame,
            text="Latest Search Summary",
            padding=10,
        )
        results_frame.pack(
            fill=tk.BOTH,
            expand=True,
        )

        self.results_tree = ttk.Treeview(
            results_frame,
            columns=("metric", "value"),
            show="headings",
            height=9,
        )
        self.results_tree.heading(
            "metric",
            text="Metric",
        )
        self.results_tree.heading(
            "value",
            text="Value",
        )
        self.results_tree.column(
            "metric",
            width=600,
            anchor=tk.W,
        )
        self.results_tree.column(
            "value",
            width=260,
            anchor=tk.E,
        )
        self.results_tree.pack(
            fill=tk.BOTH,
            expand=True,
        )

    def set_search_enabled(
        self,
        enabled: bool,
    ) -> None:
        """Enable or disable the Part 4 search button."""

        self.run_search_button.configure(
            state=tk.NORMAL if enabled else tk.DISABLED
        )

    def set_export_enabled(
        self,
        enabled: bool,
    ) -> None:
        """Enable or disable the optional downstream-link CSV export button."""

        self.export_links_button.configure(
            state=tk.NORMAL if enabled else tk.DISABLED
        )

    def display_results(
        self,
        result_rows: list[tuple[str, str]],
    ) -> None:
        """Replace the Part 4 search-summary table contents."""

        for item_id in self.results_tree.get_children():
            self.results_tree.delete(item_id)

        for metric, value in result_rows:
            self.results_tree.insert(
                "",
                tk.END,
                values=(metric, value),
            )