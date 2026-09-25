"""
Case-study management user-interface component.

This module owns the Tkinter controls used to create, save, select, load, and
refresh named case studies. It does not load pickle artifacts, access
application state, display dialogs, or make overwrite decisions.
"""

from __future__ import annotations

from collections.abc import Callable

import tkinter as tk
from tkinter import ttk


class CaseStudyControls:
    """
    Tkinter view for named case-study management controls.
    """

    def __init__(
        self,
        parent: ttk.Frame,
        current_study_text: tk.StringVar,
        selected_study_text: tk.StringVar,
        create_study_callback: Callable[[], None],
        save_study_callback: Callable[[], None],
        load_study_callback: Callable[[], None],
        refresh_studies_callback: Callable[[], None],
    ) -> None:
        """
        Create the case-study control bar.

        Args:
            parent:
                The application root content frame.

            current_study_text:
                Controller-owned variable showing the active study name.

            selected_study_text:
                Controller-owned variable holding the selected study name in
                the readonly load combobox.

            create_study_callback:
                Controller action used to prompt for and create a new study.

            save_study_callback:
                Controller action used to save the current study.

            load_study_callback:
                Controller action used to load the selected study.

            refresh_studies_callback:
                Controller action used to refresh study names from trusted
                application-managed artifacts.
        """

        self.frame = ttk.LabelFrame(
            parent,
            text="Case Study",
            padding=8,
        )

        self.save_study_button: ttk.Button
        self.study_selector_combobox: ttk.Combobox

        self._build_interface(
            current_study_text=current_study_text,
            selected_study_text=selected_study_text,
            create_study_callback=create_study_callback,
            save_study_callback=save_study_callback,
            load_study_callback=load_study_callback,
            refresh_studies_callback=refresh_studies_callback,
        )

    def _build_interface(
        self,
        current_study_text: tk.StringVar,
        selected_study_text: tk.StringVar,
        create_study_callback: Callable[[], None],
        save_study_callback: Callable[[], None],
        load_study_callback: Callable[[], None],
        refresh_studies_callback: Callable[[], None],
    ) -> None:
        """Build the static case-study management layout."""

        ttk.Label(
            self.frame,
            text="Current study:",
        ).pack(side=tk.LEFT)

        ttk.Label(
            self.frame,
            textvariable=current_study_text,
            foreground="#1f4e79",
        ).pack(
            side=tk.LEFT,
            padx=(6, 14),
        )

        ttk.Button(
            self.frame,
            text="New Study",
            command=create_study_callback,
        ).pack(side=tk.LEFT)

        self.save_study_button = ttk.Button(
            self.frame,
            text="Save Current Study",
            command=save_study_callback,
            state=tk.DISABLED,
        )
        self.save_study_button.pack(
            side=tk.LEFT,
            padx=(8, 0),
        )

        ttk.Label(
            self.frame,
            text="Load:",
        ).pack(
            side=tk.LEFT,
            padx=(18, 6),
        )

        self.study_selector_combobox = ttk.Combobox(
            self.frame,
            textvariable=selected_study_text,
            width=28,
            state="readonly",
        )
        self.study_selector_combobox.pack(side=tk.LEFT)

        ttk.Button(
            self.frame,
            text="Load Selected Study",
            command=load_study_callback,
        ).pack(
            side=tk.LEFT,
            padx=(8, 0),
        )

        ttk.Button(
            self.frame,
            text="Refresh",
            command=refresh_studies_callback,
        ).pack(
            side=tk.LEFT,
            padx=(8, 0),
        )

    def set_save_enabled(
        self,
        enabled: bool,
    ) -> None:
        """Enable or disable the Save Current Study control."""

        self.save_study_button.configure(
            state=tk.NORMAL if enabled else tk.DISABLED
        )

    def set_available_studies(
        self,
        study_names: list[str],
    ) -> None:
        """Replace the list of selectable case-study display names."""

        self.study_selector_combobox.configure(
            values=study_names
        )