"""
Part 3 dam-filter user interface.

This module owns Tkinter widget construction and result presentation for the
dam-filter workflow. It does not access application state, invoke filtering
services, write logs, or display dialogs.
"""

from __future__ import annotations

from collections.abc import Callable

import tkinter as tk
from tkinter import ttk


class FilterTab:
    """
    Tkinter view for Part 3 dam-selection controls and summary results.
    """

    def __init__(
        self,
        parent_notebook: ttk.Notebook,
        minimum_storage_text: tk.StringVar,
        hydroelectric_only_var: tk.BooleanVar,
        power_threshold_text: tk.StringVar,
        filter_status_text: tk.StringVar,
        apply_filters_callback: Callable[[], None],
        restore_defaults_callback: Callable[[], None],
    ) -> None:
        """
        Create the Part 3 tab and filtering controls.

        The controller retains ownership of Tkinter variables because it reads
        their current values when it invokes the non-UI filtering service.
        """

        self.frame = ttk.Frame(
            parent_notebook,
            padding=12,
        )

        self.power_threshold_entry: ttk.Entry
        self.apply_filters_button: ttk.Button
        self.reset_filters_button: ttk.Button
        self.filter_results_tree: ttk.Treeview
        self.hydroelectric_only_var = hydroelectric_only_var

        self._build_interface(
            minimum_storage_text=minimum_storage_text,
            hydroelectric_only_var=hydroelectric_only_var,
            power_threshold_text=power_threshold_text,
            filter_status_text=filter_status_text,
            apply_filters_callback=apply_filters_callback,
            restore_defaults_callback=restore_defaults_callback,
        )

    def _build_interface(
        self,
        minimum_storage_text: tk.StringVar,
        hydroelectric_only_var: tk.BooleanVar,
        power_threshold_text: tk.StringVar,
        filter_status_text: tk.StringVar,
        apply_filters_callback: Callable[[], None],
        restore_defaults_callback: Callable[[], None],
    ) -> None:
        """Build the static Part 3 filtering layout."""

        ttk.Label(
            self.frame,
            text="Part 3: Filter Dam Inventory",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.frame,
            text=(
                "The inventory already contains only primary dam records, "
                "where NID ID equals Federal ID. Set a lower storage limit, "
                "optionally retain dams whose purposes include Hydroelectric, "
                "and optionally apply the estimated power-capacity threshold "
                "to that hydroelectric subset."
            ),
            wraplength=950,
        ).pack(
            anchor=tk.W,
            pady=(4, 14),
        )

        baseline_frame = ttk.LabelFrame(
            self.frame,
            text="Automatic Baseline Rule",
            padding=10,
        )
        baseline_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Label(
            baseline_frame,
            text=(
                "Always applied during Part 1 initialization: "
                "NID ID = Federal ID. Associated structures are excluded so "
                "the primary dam record is retained."
            ),
            wraplength=930,
        ).pack(anchor=tk.W)

        criteria_frame = ttk.LabelFrame(
            self.frame,
            text="Researcher-Selected Filters",
            padding=12,
        )
        criteria_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Label(
            criteria_frame,
            text="Maximum storage lower limit (acre-feet):",
        ).grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
            pady=(0, 10),
        )

        minimum_storage_entry = ttk.Entry(
            criteria_frame,
            textvariable=minimum_storage_text,
            width=18,
        )
        minimum_storage_entry.grid(
            row=0,
            column=1,
            sticky=tk.W,
            pady=(0, 10),
        )

        ttk.Label(
            criteria_frame,
            text="Keeps dams where maximum storage is greater than this value.",
        ).grid(
            row=0,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
            pady=(0, 10),
        )

        hydroelectric_checkbox = ttk.Checkbutton(
            criteria_frame,
            text="Filter for hydroelectric dams",
            variable=hydroelectric_only_var,
            command=self._handle_hydroelectric_toggle,
        )
        hydroelectric_checkbox.grid(
            row=1,
            column=0,
            columnspan=3,
            sticky=tk.W,
            pady=(0, 10),
        )

        ttk.Label(
            criteria_frame,
            text="Estimated power threshold lower limit (MW):",
        ).grid(
            row=2,
            column=0,
            sticky=tk.W,
        )

        self.power_threshold_entry = ttk.Entry(
            criteria_frame,
            textvariable=power_threshold_text,
            width=18,
        )
        self.power_threshold_entry.grid(
            row=2,
            column=1,
            sticky=tk.W,
        )

        ttk.Label(
            criteria_frame,
            text=(
                "Applies: (Hydraulic Height × Max Discharge) / "
                "POWER_CONVERSION_FACTOR > threshold."
            ),
        ).grid(
            row=2,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
        )

        criteria_frame.columnconfigure(2, weight=1)

        action_frame = ttk.Frame(self.frame)
        action_frame.pack(
            fill=tk.X,
            pady=(4, 10),
        )

        self.apply_filters_button = ttk.Button(
            action_frame,
            text="Apply Filters",
            command=apply_filters_callback,
        )
        self.apply_filters_button.pack(side=tk.LEFT)

        self.reset_filters_button = ttk.Button(
            action_frame,
            text="Restore Original Defaults",
            command=restore_defaults_callback,
        )
        self.reset_filters_button.pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        ttk.Label(
            action_frame,
            textvariable=filter_status_text,
            foreground="#1f4e79",
        ).pack(
            side=tk.LEFT,
            padx=(16, 0),
        )

        results_frame = ttk.LabelFrame(
            self.frame,
            text="Current Filter Results",
            padding=10,
        )
        results_frame.pack(
            fill=tk.BOTH,
            expand=True,
        )

        self.filter_results_tree = ttk.Treeview(
            results_frame,
            columns=("metric", "value"),
            show="headings",
            height=9,
        )
        self.filter_results_tree.heading(
            "metric",
            text="Metric",
        )
        self.filter_results_tree.heading(
            "value",
            text="Value",
        )
        self.filter_results_tree.column(
            "metric",
            width=600,
            anchor=tk.W,
        )
        self.filter_results_tree.column(
            "value",
            width=250,
            anchor=tk.E,
        )
        self.filter_results_tree.pack(
            fill=tk.BOTH,
            expand=True,
        )

        self.set_power_filter_enabled(
            enabled=hydroelectric_only_var.get()
        )

    def _handle_hydroelectric_toggle(self) -> None:
        """
        Update the power-threshold entry after the checkbox changes.

        The controller owns the BooleanVar, while this view owns only the
        related widget state.
        """

        self.set_power_filter_enabled(
            enabled=self.hydroelectric_only_var.get()
        )

    def set_power_filter_enabled(
        self,
        enabled: bool,
    ) -> None:
        """Enable power-threshold input only for hydroelectric filtering."""

        self.power_threshold_entry.configure(
            state=tk.NORMAL if enabled else tk.DISABLED
        )

    def display_results(
        self,
        result_rows: list[tuple[str, str]],
    ) -> None:
        """Replace the Part 3 filter-summary table contents."""

        for item_id in self.filter_results_tree.get_children():
            self.filter_results_tree.delete(item_id)

        for metric, value in result_rows:
            self.filter_results_tree.insert(
                "",
                tk.END,
                values=(metric, value),
            )