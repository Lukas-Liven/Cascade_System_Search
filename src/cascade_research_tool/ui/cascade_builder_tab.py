"""
Part 5 cascade-construction user interface.

This module owns widget construction, button state, and summary-table display
for the cascade-builder workflow. It does not construct cascade graphs,
persist artifacts, export files, or access application session state.
"""

from __future__ import annotations

from collections.abc import Callable

import tkinter as tk
from tkinter import ttk


class CascadeBuilderTab:
    """
    Tkinter view for Part 5 cascade-construction settings and results.
    """

    def __init__(
        self,
        parent_notebook: ttk.Notebook,
        minimum_hydroelectric_dams_text: tk.StringVar,
        require_same_owner_var: tk.BooleanVar,
        construction_status_text: tk.StringVar,
        artifact_path_text: tk.StringVar,
        export_path_text: tk.StringVar,
        construct_callback: Callable[[], None],
        export_edges_callback: Callable[[], None],
        export_summary_callback: Callable[[], None],
        restore_defaults_callback: Callable[[], None],
    ) -> None:
        """Create the Part 5 tab and its controller-connected controls."""

        self.frame = ttk.Frame(
            parent_notebook,
            padding=12,
        )

        self.construct_button: ttk.Button
        self.export_edges_button: ttk.Button
        self.export_summary_button: ttk.Button
        self.results_tree: ttk.Treeview

        self._build_interface(
            minimum_hydroelectric_dams_text=(
                minimum_hydroelectric_dams_text
            ),
            require_same_owner_var=require_same_owner_var,
            construction_status_text=construction_status_text,
            artifact_path_text=artifact_path_text,
            export_path_text=export_path_text,
            construct_callback=construct_callback,
            export_edges_callback=export_edges_callback,
            export_summary_callback=export_summary_callback,
            restore_defaults_callback=restore_defaults_callback,
        )

    def _build_interface(
        self,
        minimum_hydroelectric_dams_text: tk.StringVar,
        require_same_owner_var: tk.BooleanVar,
        construction_status_text: tk.StringVar,
        artifact_path_text: tk.StringVar,
        export_path_text: tk.StringVar,
        construct_callback: Callable[[], None],
        export_edges_callback: Callable[[], None],
        export_summary_callback: Callable[[], None],
        restore_defaults_callback: Callable[[], None],
    ) -> None:
        """Build the static Part 5 widget layout."""

        ttk.Label(
            self.frame,
            text="Part 5: Construct Cascading Systems",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.frame,
            text=(
                "Build directed cascading systems from the Part 4 downstream "
                "dam-pair reference dataset. Part 3 selected and matched dams "
                "are used as cascade root candidates. Each system must contain "
                "at least two dams, regardless of the hydroelectric criterion."
            ),
            wraplength=950,
        ).pack(
            anchor=tk.W,
            pady=(4, 14),
        )

        settings_frame = ttk.LabelFrame(
            self.frame,
            text="Cascade Construction Criteria",
            padding=12,
        )
        settings_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Label(
            settings_frame,
            text="Minimum hydroelectric dams per cascading system:",
        ).grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
            pady=(0, 12),
        )

        minimum_hydroelectric_dams_entry = ttk.Entry(
            settings_frame,
            textvariable=minimum_hydroelectric_dams_text,
            width=12,
        )
        minimum_hydroelectric_dams_entry.grid(
            row=0,
            column=1,
            sticky=tk.W,
            pady=(0, 12),
        )

        ttk.Label(
            settings_frame,
            text=(
                "Enter a whole number greater than or equal to 0. "
                "The system must still contain at least two dams."
            ),
            wraplength=520,
        ).grid(
            row=0,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
            pady=(0, 12),
        )

        ttk.Checkbutton(
            settings_frame,
            text=(
                "Require every dam in each cascade chain to have the same "
                "owner as its root dam"
            ),
            variable=require_same_owner_var,
        ).grid(
            row=1,
            column=0,
            columnspan=3,
            sticky=tk.W,
        )

        settings_frame.columnconfigure(2, weight=1)

        action_frame = ttk.Frame(self.frame)
        action_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        self.construct_button = ttk.Button(
            action_frame,
            text="Construct Cascade Systems",
            command=construct_callback,
        )
        self.construct_button.pack(side=tk.LEFT)

        self.export_edges_button = ttk.Button(
            action_frame,
            text="Export System Edge List CSV",
            command=export_edges_callback,
            state=tk.DISABLED,
        )
        self.export_edges_button.pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        self.export_summary_button = ttk.Button(
            action_frame,
            text="Export System Summary CSV",
            command=export_summary_callback,
            state=tk.DISABLED,
        )
        self.export_summary_button.pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        ttk.Button(
            action_frame,
            text="Restore Defaults",
            command=restore_defaults_callback,
        ).pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        ttk.Label(
            action_frame,
            textvariable=construction_status_text,
            foreground="#1f4e79",
        ).pack(
            side=tk.LEFT,
            padx=(16, 0),
        )

        output_frame = ttk.LabelFrame(
            self.frame,
            text="Application-Managed Cascade Data",
            padding=12,
        )
        output_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Label(
            output_frame,
            text="Cascade artifact:",
        ).grid(
            row=0,
            column=0,
            sticky=tk.NW,
            padx=(0, 8),
            pady=(0, 8),
        )

        ttk.Label(
            output_frame,
            textvariable=artifact_path_text,
            wraplength=760,
        ).grid(
            row=0,
            column=1,
            sticky=tk.W,
            pady=(0, 8),
        )

        ttk.Label(
            output_frame,
            text="Optional exports:",
        ).grid(
            row=1,
            column=0,
            sticky=tk.NW,
            padx=(0, 8),
        )

        ttk.Label(
            output_frame,
            textvariable=export_path_text,
            wraplength=760,
        ).grid(
            row=1,
            column=1,
            sticky=tk.W,
        )

        summary_frame = ttk.LabelFrame(
            self.frame,
            text="Latest Construction Summary",
            padding=10,
        )
        summary_frame.pack(
            fill=tk.BOTH,
            expand=True,
        )

        self.results_tree = ttk.Treeview(
            summary_frame,
            columns=("metric", "value"),
            show="headings",
            height=9,
        )
        self.results_tree.heading("metric", text="Metric")
        self.results_tree.heading("value", text="Value")
        self.results_tree.column(
            "metric",
            width=630,
            anchor=tk.W,
        )
        self.results_tree.column(
            "value",
            width=240,
            anchor=tk.E,
        )
        self.results_tree.pack(
            fill=tk.BOTH,
            expand=True,
        )

    def set_construct_enabled(
        self,
        enabled: bool,
    ) -> None:
        """Enable or disable cascade construction."""

        self.construct_button.configure(
            state=tk.NORMAL if enabled else tk.DISABLED
        )

    def set_edge_export_enabled(
        self,
        enabled: bool,
    ) -> None:
        """Enable or disable the cascade edge-list export action."""

        self.export_edges_button.configure(
            state=tk.NORMAL if enabled else tk.DISABLED
        )

    def set_summary_export_enabled(
        self,
        enabled: bool,
    ) -> None:
        """Enable or disable the cascade summary export action."""

        self.export_summary_button.configure(
            state=tk.NORMAL if enabled else tk.DISABLED
        )

    def display_results(
        self,
        result_rows: list[tuple[str, str]],
    ) -> None:
        """Replace the Part 5 construction-summary table contents."""

        for item_id in self.results_tree.get_children():
            self.results_tree.delete(item_id)

        for metric, value in result_rows:
            self.results_tree.insert(
                "",
                tk.END,
                values=(metric, value),
            )