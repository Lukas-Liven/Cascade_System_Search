"""
Part 6 cascade-query user interface.

This module owns Tkinter widget creation and view presentation for querying,
inspecting, and mapping constructed cascade systems. Query evaluation, graph
access, logging, map generation, and dialog behavior remain in the main
application controller.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import tkinter as tk
from tkinter import ttk


class CascadeQueryTab:
    """
    Tkinter view for Part 6 cascade-system querying and inspection.
    """

    def __init__(
        self,
        parent_notebook: ttk.Notebook,
        query_state_enabled_var: tk.BooleanVar,
        query_state_text: tk.StringVar,
        query_nid_enabled_var: tk.BooleanVar,
        query_nid_text: tk.StringVar,
        query_river_enabled_var: tk.BooleanVar,
        query_river_text: tk.StringVar,
        selected_system_id_text: tk.StringVar,
        show_overview_distance_labels_var: tk.BooleanVar,
        query_status_text: tk.StringVar,
        run_query_callback: Callable[[], None],
        clear_query_callback: Callable[[], None],
        generate_query_map_callback: Callable[[], None],
        refresh_system_list_callback: Callable[[], None],
        print_system_callback: Callable[[], None],
        generate_system_map_callback: Callable[[], None],
        select_system_callback: Callable[[str], None],
        selected_system_changed_callback: Callable[[], None],
    ) -> None:
        """
        Create the Part 6 interface.

        Tkinter variables remain controller-owned because the controller reads
        them while evaluating queries and updates them when restoring studies.
        Callbacks keep this view independent of the application controller.
        """

        self.frame = ttk.Frame(
            parent_notebook,
            padding=12,
        )

        self.query_state_enabled_var = query_state_enabled_var
        self.query_nid_enabled_var = query_nid_enabled_var
        self.query_river_enabled_var = query_river_enabled_var

        self.query_state_entry: ttk.Entry
        self.query_nid_entry: ttk.Entry
        self.query_river_entry: ttk.Entry
        self.matches_tree: ttk.Treeview
        self.system_combobox: ttk.Combobox
        self.system_details_tree: ttk.Treeview

        self._select_system_callback = select_system_callback
        self._selected_system_changed_callback = (
            selected_system_changed_callback
        )

        self._build_interface(
            query_state_text=query_state_text,
            query_nid_text=query_nid_text,
            query_river_text=query_river_text,
            selected_system_id_text=selected_system_id_text,
            show_overview_distance_labels_var=(
                show_overview_distance_labels_var
            ),
            query_status_text=query_status_text,
            run_query_callback=run_query_callback,
            clear_query_callback=clear_query_callback,
            generate_query_map_callback=generate_query_map_callback,
            refresh_system_list_callback=refresh_system_list_callback,
            print_system_callback=print_system_callback,
            generate_system_map_callback=generate_system_map_callback,
        )

        self.update_query_control_states()

    def _build_interface(
        self,
        query_state_text: tk.StringVar,
        query_nid_text: tk.StringVar,
        query_river_text: tk.StringVar,
        selected_system_id_text: tk.StringVar,
        show_overview_distance_labels_var: tk.BooleanVar,
        query_status_text: tk.StringVar,
        run_query_callback: Callable[[], None],
        clear_query_callback: Callable[[], None],
        generate_query_map_callback: Callable[[], None],
        refresh_system_list_callback: Callable[[], None],
        print_system_callback: Callable[[], None],
        generate_system_map_callback: Callable[[], None],
    ) -> None:
        """Build the static Part 6 widget layout."""

        ttk.Label(
            self.frame,
            text="Part 6: Query and Visualize Cascade Systems",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.frame,
            text=(
                "Filter constructed cascading systems by state, dam NID ID, "
                "and river/stream name. Enabled criteria use AND logic. "
                "Query results can be inspected, reported, and mapped."
            ),
            wraplength=950,
        ).pack(
            anchor=tk.W,
            pady=(4, 12),
        )

        query_frame = ttk.LabelFrame(
            self.frame,
            text="Cascade-System Filters",
            padding=12,
        )
        query_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Checkbutton(
            query_frame,
            text="At least one dam in state:",
            variable=self.query_state_enabled_var,
            command=self.update_query_control_states,
        ).grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
            pady=(0, 8),
        )

        self.query_state_entry = ttk.Entry(
            query_frame,
            textvariable=query_state_text,
            width=12,
        )
        self.query_state_entry.grid(
            row=0,
            column=1,
            sticky=tk.W,
            pady=(0, 8),
        )

        ttk.Label(
            query_frame,
            text="Two-letter postal abbreviation, for example: GA",
        ).grid(
            row=0,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
            pady=(0, 8),
        )

        ttk.Checkbutton(
            query_frame,
            text="System contains dam with NID ID:",
            variable=self.query_nid_enabled_var,
            command=self.update_query_control_states,
        ).grid(
            row=1,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
            pady=(0, 8),
        )

        self.query_nid_entry = ttk.Entry(
            query_frame,
            textvariable=query_nid_text,
            width=20,
        )
        self.query_nid_entry.grid(
            row=1,
            column=1,
            sticky=tk.W,
            pady=(0, 8),
        )

        ttk.Label(
            query_frame,
            text="Exact NID ID match; case-insensitive.",
        ).grid(
            row=1,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
            pady=(0, 8),
        )

        ttk.Checkbutton(
            query_frame,
            text="At least one dam on river/stream:",
            variable=self.query_river_enabled_var,
            command=self.update_query_control_states,
        ).grid(
            row=2,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
        )

        self.query_river_entry = ttk.Entry(
            query_frame,
            textvariable=query_river_text,
            width=30,
        )
        self.query_river_entry.grid(
            row=2,
            column=1,
            sticky=tk.W,
        )

        ttk.Label(
            query_frame,
            text=(
                "Case-insensitive partial match against the NID river/stream "
                "name field."
            ),
            wraplength=470,
        ).grid(
            row=2,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
        )

        query_frame.columnconfigure(2, weight=1)

        self.query_state_entry.bind(
            "<Return>",
            lambda _event: run_query_callback(),
        )
        self.query_nid_entry.bind(
            "<Return>",
            lambda _event: run_query_callback(),
        )
        self.query_river_entry.bind(
            "<Return>",
            lambda _event: run_query_callback(),
        )

        query_actions_frame = ttk.Frame(self.frame)
        query_actions_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Button(
            query_actions_frame,
            text="Search Cascading Systems",
            command=run_query_callback,
        ).pack(side=tk.LEFT)

        ttk.Button(
            query_actions_frame,
            text="Clear Filters / Show All Systems",
            command=clear_query_callback,
        ).pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        ttk.Button(
            query_actions_frame,
            text="Generate Map of Query Results",
            command=generate_query_map_callback,
        ).pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        ttk.Checkbutton(
            query_actions_frame,
            text="Show distance labels",
            variable=show_overview_distance_labels_var,
        ).pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        ttk.Label(
            query_actions_frame,
            textvariable=query_status_text,
            foreground="#1f4e79",
        ).pack(
            side=tk.LEFT,
            padx=(16, 0),
        )

        results_frame = ttk.LabelFrame(
            self.frame,
            text="Matching Cascade Systems",
            padding=10,
        )
        results_frame.pack(
            fill=tk.BOTH,
            expand=True,
            pady=(0, 10),
        )

        self.matches_tree = ttk.Treeview(
            results_frame,
            columns=(
                "system_id",
                "root_dams",
                "total_dams",
                "hydroelectric_dams",
            ),
            show="headings",
            height=7,
        )

        self.matches_tree.heading(
            "system_id",
            text="System ID",
        )
        self.matches_tree.heading(
            "root_dams",
            text="Root Dam ID(s)",
        )
        self.matches_tree.heading(
            "total_dams",
            text="Total Dams",
        )
        self.matches_tree.heading(
            "hydroelectric_dams",
            text="Hydroelectric Dams",
        )

        self.matches_tree.column(
            "system_id",
            width=150,
            anchor=tk.W,
        )
        self.matches_tree.column(
            "root_dams",
            width=350,
            anchor=tk.W,
        )
        self.matches_tree.column(
            "total_dams",
            width=120,
            anchor=tk.E,
        )
        self.matches_tree.column(
            "hydroelectric_dams",
            width=160,
            anchor=tk.E,
        )

        matches_scrollbar = ttk.Scrollbar(
            results_frame,
            orient=tk.VERTICAL,
            command=self.matches_tree.yview,
        )
        self.matches_tree.configure(
            yscrollcommand=matches_scrollbar.set
        )

        self.matches_tree.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
        )
        matches_scrollbar.pack(
            side=tk.RIGHT,
            fill=tk.Y,
        )

        self.matches_tree.bind(
            "<<TreeviewSelect>>",
            self._handle_match_selection,
        )

        selection_frame = ttk.LabelFrame(
            self.frame,
            text="Selected Cascade System",
            padding=12,
        )
        selection_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Label(
            selection_frame,
            text="System ID:",
        ).grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
        )

        self.system_combobox = ttk.Combobox(
            selection_frame,
            textvariable=selected_system_id_text,
            width=34,
            state="normal",
        )
        self.system_combobox.grid(
            row=0,
            column=1,
            sticky=tk.W,
        )

        self.system_combobox.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._selected_system_changed_callback(),
        )
        self.system_combobox.bind(
            "<Return>",
            lambda _event: self._selected_system_changed_callback(),
        )

        ttk.Button(
            selection_frame,
            text="Show All System IDs",
            command=refresh_system_list_callback,
        ).grid(
            row=0,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
        )

        selection_frame.columnconfigure(3, weight=1)

        system_actions_frame = ttk.Frame(self.frame)
        system_actions_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Button(
            system_actions_frame,
            text="Print System Information to Log",
            command=print_system_callback,
        ).pack(side=tk.LEFT)

        ttk.Button(
            system_actions_frame,
            text="Generate and Open Interactive Map",
            command=generate_system_map_callback,
        ).pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        details_frame = ttk.LabelFrame(
            self.frame,
            text="Selected System Overview",
            padding=10,
        )
        details_frame.pack(
            fill=tk.BOTH,
            expand=True,
        )

        self.system_details_tree = ttk.Treeview(
            details_frame,
            columns=("metric", "value"),
            show="headings",
            height=7,
        )
        self.system_details_tree.heading(
            "metric",
            text="Metric",
        )
        self.system_details_tree.heading(
            "value",
            text="Value",
        )
        self.system_details_tree.column(
            "metric",
            width=360,
            anchor=tk.W,
        )
        self.system_details_tree.column(
            "value",
            width=540,
            anchor=tk.W,
        )
        self.system_details_tree.pack(
            fill=tk.BOTH,
            expand=True,
        )

    def _handle_match_selection(
        self,
        _event: Any = None,
    ) -> None:
        """Notify the controller that a query-result system was selected."""

        selected_items = self.matches_tree.selection()

        if selected_items:
            self._select_system_callback(selected_items[0])

    def update_query_control_states(self) -> None:
        """Enable a query field only while its criterion is active."""

        self.query_state_entry.configure(
            state=(
                tk.NORMAL
                if self.query_state_enabled_var.get()
                else tk.DISABLED
            )
        )
        self.query_nid_entry.configure(
            state=(
                tk.NORMAL
                if self.query_nid_enabled_var.get()
                else tk.DISABLED
            )
        )
        self.query_river_entry.configure(
            state=(
                tk.NORMAL
                if self.query_river_enabled_var.get()
                else tk.DISABLED
            )
        )

    def set_system_ids(
        self,
        system_ids: list[str],
    ) -> None:
        """Replace the available selected-system combobox values."""

        self.system_combobox.configure(values=system_ids)

    def display_matches(
        self,
        match_rows: list[tuple[str, str, int, int]],
    ) -> None:
        """
        Replace the query-results table.

        Each row contains:
        - system ID;
        - semicolon-separated root dam IDs;
        - total dam count;
        - hydroelectric dam count.
        """

        for item_id in self.matches_tree.get_children():
            self.matches_tree.delete(item_id)

        for (
            system_id,
            root_dams,
            total_dams,
            hydroelectric_dams,
        ) in match_rows:
            self.matches_tree.insert(
                "",
                tk.END,
                iid=system_id,
                values=(
                    system_id,
                    root_dams,
                    total_dams,
                    hydroelectric_dams,
                ),
            )

    def display_system_details(
        self,
        result_rows: list[tuple[str, str]],
    ) -> None:
        """Replace the selected-system overview table."""

        for item_id in self.system_details_tree.get_children():
            self.system_details_tree.delete(item_id)

        for metric, value in result_rows:
            self.system_details_tree.insert(
                "",
                tk.END,
                values=(metric, value),
            )