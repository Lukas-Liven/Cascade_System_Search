"""
Part 2 node-explorer user interface.

This module creates the Tkinter controls for NHD node and NID ID lookup.
Search behavior, application-state access, dialogs, and log output remain in
the main application controller.
"""

from __future__ import annotations

from collections.abc import Callable

import tkinter as tk
from tkinter import ttk


class NodeExplorerTab:
    """
    Tkinter view for Part 2 NHD node and NID dam lookup controls.
    """

    def __init__(
        self,
        parent_notebook: ttk.Notebook,
        node_id_text: tk.StringVar,
        nid_id_text: tk.StringVar,
        explore_node_callback: Callable[[], None],
        explore_nid_callback: Callable[[], None],
    ) -> None:
        """
        Create the Part 2 tab and lookup controls.

        Args:
            parent_notebook:
                Main workflow notebook that owns the tab frame.

            node_id_text:
                Controller-owned text variable for the NHD node ID field.

            nid_id_text:
                Controller-owned text variable for the NID ID field.

            explore_node_callback:
                Controller action invoked for node-ID lookup.

            explore_nid_callback:
                Controller action invoked for NID-ID lookup.
        """

        self.frame = ttk.Frame(
            parent_notebook,
            padding=12,
        )

        self.node_id_entry: ttk.Entry
        self.nid_id_entry: ttk.Entry

        self._build_interface(
            node_id_text=node_id_text,
            nid_id_text=nid_id_text,
            explore_node_callback=explore_node_callback,
            explore_nid_callback=explore_nid_callback,
        )

    def _build_interface(
        self,
        node_id_text: tk.StringVar,
        nid_id_text: tk.StringVar,
        explore_node_callback: Callable[[], None],
        explore_nid_callback: Callable[[], None],
    ) -> None:
        """Build the static Part 2 widget layout."""

        ttk.Label(
            self.frame,
            text="Part 2: Explore Dams at an NHD Network Node",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.frame,
            text=(
                "Search by NHD network node ID to list matched dams at that "
                "location, or search by NID ID to identify a dam's mapped "
                "network node and purposes. Results are written to the "
                "shared Application Log below."
            ),
            wraplength=950,
        ).pack(
            anchor=tk.W,
            pady=(4, 16),
        )

        lookup_frame = ttk.LabelFrame(
            self.frame,
            text="Node Lookup",
            padding=14,
        )
        lookup_frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        ttk.Label(
            lookup_frame,
            text="NHD Node ID:",
        ).grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 8),
        )

        self.node_id_entry = ttk.Entry(
            lookup_frame,
            textvariable=node_id_text,
            width=30,
        )
        self.node_id_entry.grid(
            row=0,
            column=1,
            sticky=tk.W,
        )

        explore_node_button = ttk.Button(
            lookup_frame,
            text="Explore Node",
            command=explore_node_callback,
        )
        explore_node_button.grid(
            row=0,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
        )

        self.node_id_entry.bind(
            "<Return>",
            lambda _event: explore_node_callback(),
        )

        ttk.Label(
            lookup_frame,
            text="NID ID:",
        ).grid(
            row=1,
            column=0,
            sticky=tk.W,
            padx=(0, 8),
            pady=(12, 0),
        )

        self.nid_id_entry = ttk.Entry(
            lookup_frame,
            textvariable=nid_id_text,
            width=30,
        )
        self.nid_id_entry.grid(
            row=1,
            column=1,
            sticky=tk.W,
            pady=(12, 0),
        )

        explore_nid_button = ttk.Button(
            lookup_frame,
            text="Find Dam by NID ID",
            command=explore_nid_callback,
        )
        explore_nid_button.grid(
            row=1,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
            pady=(12, 0),
        )

        self.nid_id_entry.bind(
            "<Return>",
            lambda _event: explore_nid_callback(),
        )

        lookup_frame.columnconfigure(3, weight=1)

        guidance_frame = ttk.LabelFrame(
            self.frame,
            text="Use",
            padding=12,
        )
        guidance_frame.pack(fill=tk.X)

        ttk.Label(
            guidance_frame,
            text=(
                "Node IDs come from the enhanced NHD network used during "
                "initialization. A node may contain zero, one, or multiple "
                "matched NID dam records. If no matched dam exists at the "
                "entered node, the log will report that result."
            ),
            wraplength=930,
        ).pack(anchor=tk.W)