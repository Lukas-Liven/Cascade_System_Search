"""
Main Tkinter window controller for the Cascade Research Tool.

This module coordinates application state, workflow tabs, background workers,
shared logging, case-study actions, and tab enablement. It does not contain
the main analysis algorithms; those belong in services and utilities.
"""

from __future__ import annotations


import os
import queue
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import networkx as nx
import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from cascade_research_tool.constants import (
    APP_NAME,
    CASE_STUDIES_DIRECTORY_NAME,
    CASE_STUDY_SCHEMA_VERSION,
    CASCADE_MAPS_DIRECTORY_NAME,
    CASCADE_SYSTEMS_CACHE_NAME,
    DEFAULT_CASCADE_SUMMARY_EXPORT_NAME,
    DEFAULT_CASCADE_SYSTEMS_EXPORT_NAME,
    DEFAULT_DOWNSTREAM_LINKS_EXPORT_NAME,
    DEFAULT_MAX_DISTANCE_MILES,
    DEFAULT_MIN_HYDROELECTRIC_DAMS_PER_CASCADE,
    DEFAULT_REQUIRE_SAME_OWNER,
    DOWNSTREAM_LINKS_CACHE_NAME,
)
from cascade_research_tool.exceptions import DataValidationError
from cascade_research_tool.services.cache_service import (
    compute_initialization_fingerprint,
    get_application_cache_directory,
    save_application_artifact,
)
from cascade_research_tool.services.case_study_service import (
    find_existing_case_study,
    get_case_study_file,
    list_case_studies,
    load_case_study,
    save_case_study,
    validate_case_study_payload,
)
from cascade_research_tool.services.cascade_query_service import (
    query_cascade_systems,
    system_hydroelectric_dam_count,
    system_root_dam_ids,
)
from cascade_research_tool.services.cascade_service import (
    construct_cascade_systems,
)
from cascade_research_tool.services.downstream_link_service import (
    build_downstream_links,
)
from cascade_research_tool.services.filter_service import (
    apply_dam_filters as apply_dam_filters_service,
)
from cascade_research_tool.services.initialization_service import InitializationService
from cascade_research_tool.services.map_service import (
    build_query_results_overview_map,
    build_selected_cascade_map,
)
from cascade_research_tool.services.node_lookup_service import (
    find_matched_dams_at_node,
    find_matched_dams_by_nid,
)
from cascade_research_tool.state import ApplicationState
from cascade_research_tool.ui.cascade_builder_tab import (
    CascadeBuilderTab,
)
from cascade_research_tool.ui.cascade_query_tab import (
    CascadeQueryTab,
)
from cascade_research_tool.ui.case_study_controls import (
    CaseStudyControls,
)
from cascade_research_tool.ui.downstream_tab import DownstreamTab
from cascade_research_tool.ui.filter_tab import FilterTab
from cascade_research_tool.ui.initialization_tab import (
    InitializationTab,
)
from cascade_research_tool.ui.node_explorer_tab import (
    NodeExplorerTab,
)
from cascade_research_tool.utilities.csv_export import (
    export_dataframe_to_csv,
)
from cascade_research_tool.utilities.identifiers import (
    normalize_study_name,
)
from cascade_research_tool.utilities.map_helpers import display_value
from cascade_research_tool.utilities.validation import (
    parse_nonnegative_float,
    parse_nonnegative_integer,
)

# ---------------------------------------------------------------------------
# Tkinter application
# ---------------------------------------------------------------------------

class CascadeResearchApp(tk.Tk):
    """Initial Tkinter shell for the staged Cascade Research Tool."""

    def __init__(self) -> None:
        super().__init__()

        self.title(APP_NAME)
        self.minsize(900, 620)
        self.geometry("1040x720")

        self.cache_dir = get_application_cache_directory()
        # Named case studies are isolated from shared NHD, GeoConnex, NID,
        # ResNet, and workflow-cache artifacts. A case study stores the
        # researcher's selections and derived Part 3–6 results.
        self.case_studies_directory = (
            self.cache_dir / CASE_STUDIES_DIRECTORY_NAME
        )
        self.case_studies_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        if os.name != "nt":
            os.chmod(self.case_studies_directory, 0o700)

        # Holds the active case study and Parts 1–6 research data. Tkinter
        # widgets, variable objects, queues, and threads remain controller
        # attributes and are intentionally not included in this model.
        self.state = ApplicationState()

        self.study_name_text = tk.StringVar(
            value="No case study selected"
        )
        self.study_selector_text = tk.StringVar()
        # Application-managed workflow artifacts. These files are the
        # persistent equivalents of the in-memory Part 4 and Part 5 results.
        # They are not user-editable workflow inputs.
        self.downstream_links_cache_path = (
            self.cache_dir / DOWNSTREAM_LINKS_CACHE_NAME
        )
        self.cascade_systems_cache_path = (
            self.cache_dir / CASCADE_SYSTEMS_CACHE_NAME
        )

        self.node_id_text = tk.StringVar()
        self.nid_id_text = tk.StringVar()
        
        self.minimum_storage_text = tk.StringVar(value="100")
        self.hydroelectric_only_var = tk.BooleanVar(value=True)
        self.power_threshold_text = tk.StringVar(value="10")

        self.filter_status_text = tk.StringVar(
            value="Configure Initial Filters."
        )

        self.maximum_downstream_distance_text = tk.StringVar(
            value=str(DEFAULT_MAX_DISTANCE_MILES)
        )

        self.downstream_search_status_text = tk.StringVar(
            value="Click to start downstream search."
        )

        self.downstream_csv_path_text = tk.StringVar(
            value=(
                "The application cache path will be shown after the "
                "downstream search completes."
            )
        )

        # The downstream search has its own worker thread because it can
        # process thousands of matched dams and must not block Tkinter.
        self.downstream_search_thread: Optional[threading.Thread] = None

        self.minimum_hydroelectric_dams_text = tk.StringVar(
            value=str(DEFAULT_MIN_HYDROELECTRIC_DAMS_PER_CASCADE)
        )

        self.require_same_owner_var = tk.BooleanVar(
            value=DEFAULT_REQUIRE_SAME_OWNER
        )

        self.cascade_construction_status_text = tk.StringVar(
            value=(
                "Apply Part 3 filters and build Part 4 downstream links "
                "before constructing cascade systems."
            )
        )

        self.cascade_systems_csv_path_text = tk.StringVar(
            value="The cascade artifact will be shown after construction."
        )

        self.cascade_summary_csv_path_text = tk.StringVar(
            value="Optional CSV exports will be available after construction."
        )

        # Cascade construction can involve many chains, graph merges, and CSV
        # exports. Run it separately from the Tkinter event thread.
        self.cascade_construction_thread: Optional[threading.Thread] = None

        self.query_state_enabled_var = tk.BooleanVar(value=False)
        self.query_state_text = tk.StringVar()

        self.query_nid_enabled_var = tk.BooleanVar(value=False)
        self.query_nid_text = tk.StringVar()

        self.query_river_enabled_var = tk.BooleanVar(value=False)
        self.query_river_text = tk.StringVar()

        self.selected_system_id_text = tk.StringVar()

        self.show_overview_distance_labels_var = tk.BooleanVar(
            value=False
        )

        self.cascade_query_status_text = tk.StringVar(
            value=(
                "Construct cascade systems in Part 5 before querying or "
                "visualizing them."
            )
        )

        # Part 6 stores generated HTML maps in the private user-local
        # application cache directory. Each selected system receives its own
        # independently referenceable interactive HTML map file.
        self.cascade_maps_directory = (
            self.cache_dir / CASCADE_MAPS_DIRECTORY_NAME
        )

        # Background worker threads communicate strictly through this queue.
        # Tkinter widgets are updated only on the main/UI thread.
        self.ui_message_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.initialization_thread: Optional[threading.Thread] = None

        self.status_text = tk.StringVar(value="Ready. Select Initialize Data.")
        self.progress_text = tk.StringVar(value="No initialization has been run.")
        self.cache_path_text = tk.StringVar(value=str(self.cache_dir))

        self._build_interface()
        self.after(100, self._process_worker_messages)
        self.protocol("WM_DELETE_WINDOW", self._handle_window_close)

    def _build_interface(self) -> None:
        """
        Build the primary Tkinter layout.

        The notebook places workflow stages at the top of the interface.
        Part 1 contains the existing initialization workflow; Part 2 contains
        node/dam exploration; Part 3 contains the filtering stage. Parts 4–6 are visible placeholders so the
        application structure matches the planned research workflow.
        """

        # The root frame provides consistent padding around both the tabs and
        # the shared log display.
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            root,
            text=APP_NAME,
            font=("TkDefaultFont", 16, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            root,
            text=(
                "A staged research environment for initializing dam data, "
                "exploring river-network nodes, filtering candidates, "
                "constructing cascades, and reviewing results."
            ),
            wraplength=980,
        ).pack(anchor=tk.W, pady=(4, 12))

        # ------------------------------------------------------------------
        # Named case-study management
        # ------------------------------------------------------------------
        self.case_study_controls_view = CaseStudyControls(
            parent=root,
            current_study_text=self.study_name_text,
            selected_study_text=self.study_selector_text,
            create_study_callback=self.create_new_study,
            save_study_callback=self.save_current_study,
            load_study_callback=self.load_selected_study,
            refresh_studies_callback=self.refresh_case_study_list,
        )

        self.case_study_controls_view.frame.pack(
            fill=tk.X,
            pady=(0, 10),
        )

        self.refresh_case_study_list()

        # A vertical PanedWindow lets the user drag the horizontal separator
        # between the workflow tabs and the shared application log. This makes
        # the log area larger for reviewing detailed output or smaller when
        # the user wants more room for a workflow tab.
        self.main_pane = ttk.PanedWindow(root, orient=tk.VERTICAL)
        self.main_pane.pack(fill=tk.BOTH, expand=True)

        # This frame contains the notebook and occupies the upper resizable
        # section of the window.
        notebook_container = ttk.Frame(self.main_pane)
        self.main_pane.add(notebook_container, weight=4)

        # The notebook renders the application's workflow-stage tabs.
        self.notebook = ttk.Notebook(notebook_container)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # Create one frame for each planned workflow stage.

        self.initialization_tab_view = InitializationTab(
            parent_notebook=self.notebook,
            cache_path_text=self.cache_path_text,
            status_text=self.status_text,
            progress_text=self.progress_text,
            start_initialization_callback=self.start_initialization,
        )
        self.initialization_tab = self.initialization_tab_view.frame

        self.node_explorer_tab_view = NodeExplorerTab(
            parent_notebook=self.notebook,
            node_id_text=self.node_id_text,
            nid_id_text=self.nid_id_text,
            explore_node_callback=self.explore_selected_node,
            explore_nid_callback=self.explore_selected_nid,
        )

        self.node_explorer_tab = self.node_explorer_tab_view.frame

        self.filter_tab_view = FilterTab(
            parent_notebook=self.notebook,
            minimum_storage_text=self.minimum_storage_text,
            hydroelectric_only_var=self.hydroelectric_only_var,
            power_threshold_text=self.power_threshold_text,
            filter_status_text=self.filter_status_text,
            apply_filters_callback=self.apply_dam_filters,
            restore_defaults_callback=self.restore_default_filters,
        )

        self.filter_tab = self.filter_tab_view.frame

        self.downstream_tab_view = DownstreamTab(
            parent_notebook=self.notebook,
            maximum_distance_text=(
                self.maximum_downstream_distance_text
            ),
            search_status_text=self.downstream_search_status_text,
            artifact_path_text=self.downstream_csv_path_text,
            start_search_callback=self.start_downstream_search,
            export_csv_callback=self.export_downstream_links_csv,
        )

        self.downstream_tab = self.downstream_tab_view.frame

        self.cascade_builder_tab_view = CascadeBuilderTab(
            parent_notebook=self.notebook,
            minimum_hydroelectric_dams_text=(
                self.minimum_hydroelectric_dams_text
            ),
            require_same_owner_var=self.require_same_owner_var,
            construction_status_text=(
                self.cascade_construction_status_text
            ),
            artifact_path_text=self.cascade_systems_csv_path_text,
            export_path_text=self.cascade_summary_csv_path_text,
            construct_callback=self.start_cascade_construction,
            export_edges_callback=self.export_cascade_edges_csv,
            export_summary_callback=self.export_cascade_summary_csv,
            restore_defaults_callback=self.restore_default_cascade_settings,
        )

        self.cascade_builder_tab = self.cascade_builder_tab_view.frame

        self.cascade_query_tab_view = CascadeQueryTab(
            parent_notebook=self.notebook,
            query_state_enabled_var=self.query_state_enabled_var,
            query_state_text=self.query_state_text,
            query_nid_enabled_var=self.query_nid_enabled_var,
            query_nid_text=self.query_nid_text,
            query_river_enabled_var=self.query_river_enabled_var,
            query_river_text=self.query_river_text,
            selected_system_id_text=self.selected_system_id_text,
            show_overview_distance_labels_var=(
                self.show_overview_distance_labels_var
            ),
            query_status_text=self.cascade_query_status_text,
            run_query_callback=self.run_cascade_system_query,
            clear_query_callback=self.clear_cascade_system_query,
            generate_query_map_callback=(
                self.generate_query_results_conus_map
            ),
            refresh_system_list_callback=(
                self.refresh_cascade_system_list
            ),
            print_system_callback=self.print_selected_cascade_graph,
            generate_system_map_callback=(
                self.generate_selected_cascade_map
            ),
            select_system_callback=(
                self.select_cascade_system_from_query_result
            ),
            selected_system_changed_callback=(
                self.selected_cascade_system_changed
            ),
        )

        self.cascade_query_tab = self.cascade_query_tab_view.frame

        self.notebook.add(
            self.initialization_tab,
            text="Part 1 — Initialize Data",
        )
        self.notebook.add(
            self.node_explorer_tab,
            text="Part 2 — Explore Nodes",
        )
        self.notebook.add(
            self.filter_tab,
            text="Part 3 — Filter Dams",
        )
        self.notebook.add(
            self.downstream_tab,
            text="Part 4 — Search Downstream",
        )
        self.notebook.add(
            self.cascade_builder_tab,
            text="Part 5 — Construct Cascades",
        )
        self.notebook.add(
            self.cascade_query_tab,
            text="Part 6 — Query Cascades",
        )

        # Part 2 depends on successful Part 1 initialization.
        self.notebook.tab(self.node_explorer_tab, state="disabled")

        # Part 3 also depends on the initialized NID inventory and COMID/node
        # mapping created during Part 1.
        self.notebook.tab(self.filter_tab, state="disabled")

        # Part 4 requires the NHD graph and the full matched primary-dam
        # inventory produced during Part 1 initialization.
        self.notebook.tab(self.downstream_tab, state="disabled")

        # Part 5 remains unavailable until the user has both selected a Part 3
        # candidate set and built the Part 4 downstream-link reference CSV.
        self.notebook.tab(self.cascade_builder_tab, state="disabled")

        # Part 6 is enabled when Part 5 has successfully constructed one or
        # more in-memory NetworkX cascade graphs.
        self.notebook.tab(self.cascade_query_tab, state="disabled")

        # The log receives its own lower resizable pane. It remains visible
        # regardless of the active workflow tab.
        log_container = ttk.Frame(self.main_pane)
        self.main_pane.add(log_container, weight=1)

        log_frame = ttk.LabelFrame(
            log_container,
            text="Application Log — Drag the divider above to resize",
            padding=10,
        )
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_widget = tk.Text(
            log_frame,
            height=10,
            wrap=tk.WORD,
            state=tk.DISABLED,
            background="#fbfbfb",
        )

        log_scrollbar = ttk.Scrollbar(
            log_frame,
            orient=tk.VERTICAL,
            command=self.log_widget.yview,
        )
        self.log_widget.configure(yscrollcommand=log_scrollbar.set)

        self.log_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._append_log(
            "Application started. Initialize the required datasets in Part 1 "
            "before continuing."
        )

    def selected_cascade_system_changed(self) -> None:
        """
        Handle View-generated changes to the selected Part 6 system ID.
        """

        self._update_selected_system_details()

    def refresh_case_study_list(self) -> None:
        """
        Refresh the private case-study selector from application-owned files.

        The application intentionally lists only files in its controlled
        case_studies directory. It does not load user-selected pickle files.
        """

        study_names: list[str] = []

        study_names, warnings = list_case_studies(
            self.case_studies_directory
        )

        for warning in warnings:
            self._append_log(warning)
        study_names = sorted(set(study_names), key=str.casefold)

        self.case_study_controls_view.set_available_studies(
            study_names
        )

        if (
            self.state.current_study_name is not None
            and self.state.current_study_name in study_names
        ):
            self.study_selector_text.set(
                self.state.current_study_name
            )
        elif study_names:
            self.study_selector_text.set(study_names[0])
        else:
            self.study_selector_text.set("")

    def create_new_study(self) -> None:
        """
        Create a new named study and reset derived Parts 3–6 state.

        Part 1 initialization remains available because NHD/NID source data
        are shared application caches rather than study-specific data.
        """

        study_name = simpledialog.askstring(
            APP_NAME,
            "Enter a name for the new case study:",
            parent=self,
        )

        if study_name is None:
            return

        try:
            study_name = normalize_study_name(study_name)
            study_file = get_case_study_file(
                case_studies_directory=self.case_studies_directory,
                study_name=study_name
            )
        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error))
            return

        try:
            existing_study = find_existing_case_study(
                self.case_studies_directory,
                study_name,
            )
        except DataValidationError as error:
            self._append_log(
                f"Could not validate an existing case-study artifact: "
                f"{error}"
            )

            messagebox.showerror(
                APP_NAME,
                "A case-study file already exists for this normalized study "
                "name, but it could not be safely validated.\n\n"
                f"Details: {error}",
                parent=self,
            )
            return

        if existing_study is not None:
            _, existing_study_name = existing_study

            overwrite_existing = messagebox.askyesno(
                APP_NAME,
                (
                    f"The case study '{existing_study_name}' already "
                    "exists.\n\nDo you want to overwrite it?"
                ),
                parent=self,
            )

            if not overwrite_existing:
                self._append_log(
                    "New case-study creation canceled because the normalized "
                    "study name resolves to existing study "
                    f"'{existing_study_name}'."
                )
                return

        self.state.current_study_name = study_name
        self.state.current_study_file = study_file
        self.study_name_text.set(study_name)
        self.study_selector_text.set(study_name)

        self._reset_study_derived_state()

        # A study can be named before initialization, but it cannot be saved
        # until an initialized baseline exists.
        self.case_study_controls_view.set_save_enabled(
            enabled=self.state.initialization_result is not None
        )

        self._append_log(
            f"Created new case study: {study_name}"
        )

        self.refresh_case_study_list()

    def _reset_study_derived_state(self) -> None:
        """
        Reset all researcher-specific Part 3–6 state.

        This does not delete existing application cache artifacts or other
        named studies. It only starts the current in-memory study fresh.
        """

        self.state.reset_study_derived_data()

        # Restore Part 3 defaults.
        if hasattr(self, "minimum_storage_text"):
            self.minimum_storage_text.set("100")

        if hasattr(self, "hydroelectric_only_var"):
            self.hydroelectric_only_var.set(True)

        if hasattr(self, "power_threshold_text"):
            self.power_threshold_text.set("10")

        if hasattr(self, "_update_power_filter_state"):
            self.filter_tab_view.set_power_filter_enabled(
            enabled=self.hydroelectric_only_var.get()
            )

        # Restore Part 5 defaults.
        if hasattr(self, "minimum_hydroelectric_dams_text"):
            self.minimum_hydroelectric_dams_text.set(
                str(DEFAULT_MIN_HYDROELECTRIC_DAMS_PER_CASCADE)
            )

        if hasattr(self, "require_same_owner_var"):
            self.require_same_owner_var.set(
                DEFAULT_REQUIRE_SAME_OWNER
            )

        # Clear Part 6 query controls.
        if hasattr(self, "query_state_enabled_var"):
            self.query_state_enabled_var.set(False)
            self.query_state_text.set("")

        if hasattr(self, "query_nid_enabled_var"):
            self.query_nid_enabled_var.set(False)
            self.query_nid_text.set("")

        if hasattr(self, "query_river_enabled_var"):
            self.query_river_enabled_var.set(False)
            self.query_river_text.set("")

        if hasattr(self, "_update_cascade_query_control_states"):
            self.cascade_query_tab_view.update_query_control_states()

        # Reset UI states that depend on generated Part 4/5 data.
        if hasattr(self, "export_downstream_links_button"):
            self.downstream_tab_view.set_export_enabled(
                enabled=False
            )

        if hasattr(self, "export_cascade_edges_button"):
            self.cascade_builder_tab_view.set_edge_export_enabled(enabled=False)

        if hasattr(self, "export_cascade_summary_button"):
            self.cascade_builder_tab_view.set_summary_export_enabled(enabled=False)

        if hasattr(self, "notebook"):
            self.notebook.tab(
                self.cascade_builder_tab,
                state="disabled",
            )
            self.notebook.tab(
                self.cascade_query_tab,
                state="disabled",
            )

        if hasattr(self, "downstream_csv_path_text"):
            self.downstream_csv_path_text.set(
                "No downstream-link reference has been created for this study."
            )

        if hasattr(self, "cascade_systems_csv_path_text"):
            self.cascade_systems_csv_path_text.set(
                "No cascade artifact has been created for this study."
            )

        if hasattr(self, "cascade_summary_csv_path_text"):
            self.cascade_summary_csv_path_text.set(
                "No cascade summary has been created for this study."
            )

        if hasattr(self, "cascade_system_combobox"):
            self.cascade_query_tab_view.set_system_ids([])

        if hasattr(self, "selected_system_id_text"):
            self.selected_system_id_text.set("")

        if hasattr(self, "_display_cascade_query_matches"):
            self._display_cascade_query_matches([])


    def _build_case_study_payload(self) -> dict[str, Any]:
        """
        Build the complete serializable state for the active case study.

        The NHD graph and complete base NID inventory are deliberately omitted.
        Those are common application caches restored through Part 1. The study
        stores only researcher-specific workflow state and derived outputs.
        """

        if self.state.initialization_result is None:
            raise RuntimeError(
                "Part 1 initialization is required before saving a study."
            )

        if self.state.current_study_name is None:
            raise RuntimeError(
                "Create or load a named case study before saving."
            )

        return {
            "study_name": self.state.current_study_name,
            "saved_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "study_schema_version": CASE_STUDY_SCHEMA_VERSION,
            "initialization_fingerprint": (
                self.state.current_initialization_fingerprint
            ),
            "part_3": {
                "minimum_storage": self.minimum_storage_text.get(),
                "hydroelectric_only": (
                    self.hydroelectric_only_var.get()
                ),
                "power_threshold": self.power_threshold_text.get(),
                "filtered_dam_inventory": (
                    self.state.filtered_dam_inventory.copy()
                    if self.state.filtered_dam_inventory is not None
                    else None
                ),
                "filtered_dam_inventory_matched": (
                    self.state.filtered_dam_inventory_matched.copy()
                    if self.state.filtered_dam_inventory_matched is not None
                    else None
                ),
            },
            "part_4": {
                "maximum_distance_miles": (
                    self.maximum_downstream_distance_text.get()
                ),
                "downstream_links": (
                    self.state.downstream_links.copy()
                    if self.state.downstream_links is not None
                    else None
                ),
            },
            "part_5": {
                "minimum_hydroelectric_dams": (
                    self.minimum_hydroelectric_dams_text.get()
                ),
                "require_same_owner": (
                    self.require_same_owner_var.get()
                ),
                "cascade_graphs": self.state.cascade_graphs,
                "cascade_systems_edges": (
                    self.state.cascade_systems_edges.copy()
                    if self.state.cascade_systems_edges is not None
                    else None
                ),
                "cascade_systems_summary": (
                    self.state.cascade_systems_summary.copy()
                    if self.state.cascade_systems_summary is not None
                    else None
                ),
            },
            "part_6": {
                "state_enabled": self.query_state_enabled_var.get(),
                "state_value": self.query_state_text.get(),
                "nid_enabled": self.query_nid_enabled_var.get(),
                "nid_value": self.query_nid_text.get(),
                "river_enabled": self.query_river_enabled_var.get(),
                "river_value": self.query_river_text.get(),
                "last_query_system_ids": (
                    self.state.last_cascade_query_system_ids.copy()
                ),
            },
        }


    def save_current_study(self) -> None:
        """
        Save current Part 3–6 progress to the active named study artifact.
        """

        if self.state.current_study_name is None:
            self.create_new_study()

            if self.state.current_study_name is None:
                return

        if self.state.current_study_file is None:
            try:
                self.state.current_study_file = get_case_study_file(
                    case_studies_directory=self.case_studies_directory,
                    study_name=self.state.current_study_name
                )
            except ValueError as error:
                messagebox.showerror(APP_NAME, str(error))
                return

        try:
            payload = self._build_case_study_payload()

            save_case_study(
                study_file=self.state.current_study_file,
                payload=payload,
            )

            self._append_log(
                f"Saved case study '{self.state.current_study_name}' to "
                f"{self.state.current_study_file}"
            )

            self.refresh_case_study_list()

        except Exception as error:
            self._append_log(
                f"Case-study save failed: {error}"
            )

            messagebox.showerror(
                APP_NAME,
                f"Could not save the current case study.\n\nDetails: {error}",
            )


    def load_selected_study(self) -> None:
        """
        Load the selected named study after Part 1 initialization.

        Requiring initialization first ensures NHD/NID base data exist before
        restored derived DataFrames and NetworkX graphs are used.
        """

        if self.state.initialization_result is None:
            messagebox.showwarning(
                APP_NAME,
                "Initialize data in Part 1 before loading a case study.",
            )
            return

        study_name = self.study_selector_text.get().strip()

        if not study_name:
            messagebox.showwarning(
                APP_NAME,
                "Select a case study to load.",
            )
            return

        try:
            study_file = get_case_study_file(
                case_studies_directory=self.case_studies_directory,
                study_name=study_name
            )

            payload = load_case_study(study_file)

            self._restore_case_study_payload(
                payload,
                study_file,
            )

        except Exception as error:
            self._append_log(
                f"Case-study load failed for '{study_name}': {error}"
            )

            messagebox.showerror(
                APP_NAME,
                f"Could not load case study '{study_name}'.\n\n"
                f"Details: {error}",
            )


    def _restore_case_study_payload(
        self,
        payload: dict[str, Any],
        study_file: Path,
    ) -> None:
        """
        Restore a validated named study into the current application session.
        """
        study_name = validate_case_study_payload(payload)

        saved_fingerprint = payload.get(
            "initialization_fingerprint"
        )

        if (
            saved_fingerprint
            and self.state.current_initialization_fingerprint
            and saved_fingerprint
            != self.state.current_initialization_fingerprint
        ):
            continue_load = messagebox.askyesno(
                APP_NAME,
                "The initialized NHD/NID data context differs from the "
                "context used when this study was saved.\n\n"
                "Loading may restore stale downstream links or cascade "
                "systems. Continue anyway?",
            )

            if not continue_load:
                return

        part_3 = payload.get("part_3", {})
        part_4 = payload.get("part_4", {})
        part_5 = payload.get("part_5", {})
        part_6 = payload.get("part_6", {})

        if not isinstance(part_3, dict):
            raise DataValidationError("Invalid Part 3 study data.")

        if not isinstance(part_4, dict):
            raise DataValidationError("Invalid Part 4 study data.")

        if not isinstance(part_5, dict):
            raise DataValidationError("Invalid Part 5 study data.")

        if not isinstance(part_6, dict):
            raise DataValidationError("Invalid Part 6 study data.")

        # Reset existing researcher-specific results before restoring the
        # selected study.
        self._reset_study_derived_state()

        # --------------------------------------------------------------
        # Restore Part 3
        # --------------------------------------------------------------
        self.minimum_storage_text.set(
            str(part_3.get("minimum_storage", "100"))
        )
        self.hydroelectric_only_var.set(
            bool(part_3.get("hydroelectric_only", True))
        )
        self.power_threshold_text.set(
            str(part_3.get("power_threshold", "10"))
        )
        self.filter_tab_view.set_power_filter_enabled(
            enabled=self.hydroelectric_only_var.get()
        )

        filtered_inventory = part_3.get("filtered_dam_inventory")
        filtered_inventory_matched = part_3.get(
            "filtered_dam_inventory_matched"
        )

        if filtered_inventory is not None:
            if not isinstance(filtered_inventory, pd.DataFrame):
                raise DataValidationError(
                    "Invalid filtered dam inventory in case study."
                )

            self.state.filtered_dam_inventory = filtered_inventory.copy()

        if filtered_inventory_matched is not None:
            if not isinstance(filtered_inventory_matched, pd.DataFrame):
                raise DataValidationError(
                    "Invalid matched filtered dam inventory in case study."
                )

            self.state.filtered_dam_inventory_matched = (
                filtered_inventory_matched.copy()
            )

        # --------------------------------------------------------------
        # Restore Part 4
        # --------------------------------------------------------------
        self.maximum_downstream_distance_text.set(
            str(
                part_4.get(
                    "maximum_distance_miles",
                    DEFAULT_MAX_DISTANCE_MILES,
                )
            )
        )

        downstream_links = part_4.get("downstream_links")

        if downstream_links is not None:
            if not isinstance(downstream_links, pd.DataFrame):
                raise DataValidationError(
                    "Invalid downstream-link data in case study."
                )

            self.state.downstream_links = downstream_links.copy()
            self.state.downstream_links_cache_file = (
                self.downstream_links_cache_path
            )

            self.cascade_builder_tab_view.set_edge_export_enabled(
                enabled=True
            )
            self.cascade_builder_tab_view.set_summary_export_enabled(
                enabled=True
            )

            self.downstream_csv_path_text.set(
                f"Restored from study: {study_file.name}"
            )

            self.notebook.tab(
                self.cascade_builder_tab,
                state="normal",
            )

        # --------------------------------------------------------------
        # Restore Part 5
        # --------------------------------------------------------------
        self.minimum_hydroelectric_dams_text.set(
            str(
                part_5.get(
                    "minimum_hydroelectric_dams",
                    DEFAULT_MIN_HYDROELECTRIC_DAMS_PER_CASCADE,
                )
            )
        )
        self.require_same_owner_var.set(
            bool(
                part_5.get(
                    "require_same_owner",
                    DEFAULT_REQUIRE_SAME_OWNER,
                )
            )
        )

        cascade_graphs = part_5.get("cascade_graphs", {})

        if not isinstance(cascade_graphs, dict):
            raise DataValidationError(
                "Invalid cascade graph data in case study."
            )

        for system_id, cascade_graph in cascade_graphs.items():
            if not isinstance(system_id, str):
                raise DataValidationError(
                    "Cascade system IDs must be strings."
                )

            if not isinstance(cascade_graph, nx.DiGraph):
                raise DataValidationError(
                    "Invalid cascade graph structure in case study."
                )

        self.state.cascade_graphs = cascade_graphs

        cascade_edges = part_5.get("cascade_systems_edges")
        cascade_summary = part_5.get("cascade_systems_summary")

        if cascade_edges is not None:
            if not isinstance(cascade_edges, pd.DataFrame):
                raise DataValidationError(
                    "Invalid cascade edge-list data in case study."
                )

            self.state.cascade_systems_edges = cascade_edges.copy()

        if cascade_summary is not None:
            if not isinstance(cascade_summary, pd.DataFrame):
                raise DataValidationError(
                    "Invalid cascade summary data in case study."
                )

            self.state.cascade_systems_summary = cascade_summary.copy()

        if self.state.cascade_graphs:
            self.state.cascade_systems_cache_file = (
                self.cascade_systems_cache_path
            )

            self.cascade_builder_tab_view.set_edge_export_enabled(enabled=True)
            self.cascade_builder_tab_view.set_summary_export_enabled(enabled=True)

            self.cascade_systems_csv_path_text.set(
                f"Restored from study: {study_file.name}"
            )
            self.cascade_summary_csv_path_text.set(
                "Optional CSV exports are available."
            )

            self.notebook.tab(
                self.cascade_query_tab,
                state="normal",
            )

        # --------------------------------------------------------------
        # Restore Part 6
        # --------------------------------------------------------------
        self.query_state_enabled_var.set(
            bool(part_6.get("state_enabled", False))
        )
        self.query_state_text.set(
            str(part_6.get("state_value", ""))
        )

        self.query_nid_enabled_var.set(
            bool(part_6.get("nid_enabled", False))
        )
        self.query_nid_text.set(
            str(part_6.get("nid_value", ""))
        )

        self.query_river_enabled_var.set(
            bool(part_6.get("river_enabled", False))
        )
        self.query_river_text.set(
            str(part_6.get("river_value", ""))
        )

        self.cascade_query_tab_view.update_query_control_states()

        restored_query_ids = part_6.get(
            "last_query_system_ids",
            [],
        )

        if not isinstance(restored_query_ids, list):
            restored_query_ids = []

        self.state.last_cascade_query_system_ids = [
            system_id
            for system_id in restored_query_ids
            if isinstance(system_id, str)
            and system_id in self.state.cascade_graphs
        ]

        if self.state.cascade_graphs:
            available_system_ids = sorted(
                self.state.cascade_graphs.keys()
            )

            self.cascade_query_tab_view.set_system_ids(
                available_system_ids
            )

            if self.state.last_cascade_query_system_ids:
                self._display_cascade_query_matches(
                    self.state.last_cascade_query_system_ids
                )
                self.selected_system_id_text.set(
                    self.state.last_cascade_query_system_ids[0]
                )
            else:
                self._display_cascade_query_matches(
                    available_system_ids
                )
                self.selected_system_id_text.set(
                    available_system_ids[0]
                )

            self._update_selected_system_details()

        self.state.current_study_name = study_name
        self.state.current_study_file = study_file
        self.study_name_text.set(study_name)
        self.study_selector_text.set(study_name)
        self.case_study_controls_view.set_save_enabled(
            enabled=True
        )

        self._append_log(
            f"Loaded case study '{study_name}' from {study_file}"
        )

    def explore_selected_node(self) -> None:
        """
        Validate the node-ID field and write dam information to the shared log.

        The output intentionally mirrors the original explore_node_dams()
        function from cascade_search:
        - no-dam message when no matched dam is assigned to the node;
        - node ID;
        - count of dams;
        - latitude/longitude;
        - dam name, NID ID, purposes, and operational status for each dam.
        """

        if self.state.initialization_result is None:
            messagebox.showwarning(
                APP_NAME,
                "Initialize the NHD and dam datasets in Part 1 before exploring nodes.",
            )
            return

        raw_node_id = self.node_id_text.get().strip()

        if not raw_node_id:
            messagebox.showwarning(
                APP_NAME,
                "Enter an NHD node ID before selecting Explore Node.",
            )
            return

        try:
            requested_node_id = int(raw_node_id)
        except ValueError:
            messagebox.showwarning(
                APP_NAME,
                "The NHD node ID must be a whole number.",
            )
            return

        try:
            dams_at_node = find_matched_dams_at_node(
                self.state.initialization_result.dam_inventory_matched,
                requested_node_id,
            )
        except ValueError as error:
            self._append_log(
                f"Part 2 node lookup could not be completed: {error}"
            )
            messagebox.showerror(
                APP_NAME,
                f"Could not search the matched dam inventory.\n\nDetails: {error}",
            )
            return

        if len(dams_at_node) == 0:
            self._append_log(f"No dams found at node {requested_node_id}")
            return

        first_dam = dams_at_node.iloc[0]

        self._append_log("")
        self._append_log("=" * 60)
        self._append_log(f"Node ID: {requested_node_id}")
        self._append_log(f"Number of dams: {len(dams_at_node)}")
        self._append_log(
            f"Location: ({first_dam['Latitude']}, {first_dam['Longitude']})"
        )
        self._append_log("=" * 60)
        self._append_log("")

        for _, dam in dams_at_node.iterrows():
            self._append_log(f"Dam: {dam.get('Dam Name', 'Unknown')}")
            self._append_log(f"  NID ID: {dam.get('NID ID', 'Unknown')}")
            self._append_log(f"  Purposes: {dam.get('Purposes', 'Unknown')}")
            self._append_log(
                f"  Operational Status: "
                f"{dam.get('Operational Status', 'Unknown')}"
            )
            self._append_log("")

    def explore_selected_nid(self) -> None:
        """
        Find a matched dam using its NID ID and report its NHD node location
        and declared purposes in the shared application log.

        This lookup intentionally uses dam_inventory_matched. Therefore, a
        record returned by this function has successfully been mapped through
        GeoConnex or the ResNet fallback to an NHD network node.
        """

        # Part 2 depends on the data structures created by Part 1.
        if self.state.initialization_result is None:
            messagebox.showwarning(
                APP_NAME,
                "Initialize the NHD and dam datasets in Part 1 before searching by NID ID.",
            )
            return

        requested_nid_id = self.nid_id_text.get().strip()

        if not requested_nid_id:
            messagebox.showwarning(
                APP_NAME,
                "Enter an NID ID before selecting Find Dam by NID ID.",
            )
            return

        try:
            matching_dams = find_matched_dams_by_nid(
                self.state.initialization_result.dam_inventory_matched,
                requested_nid_id,
            )
        except ValueError as error:
            self._append_log(
                f"Part 2 NID lookup could not be completed: {error}"
            )
            messagebox.showerror(
                APP_NAME,
                f"Could not search the matched dam inventory.\n\nDetails: {error}",
            )
            return

        if matching_dams.empty:
            self._append_log(
                f"No matched dam found for NID ID {requested_nid_id}. "
                "The dam may not exist in the filtered NID inventory or may "
                "not have been mapped to an NHD network node."
            )
            return

        # Initialization removes duplicate NID IDs, but iteration is retained
        # to keep the behavior safe if future input datasets contain duplicates.
        for _, dam in matching_dams.iterrows():
            dam_name = dam.get("Dam Name", "Unknown")
            node_id = dam.get("node_id", "Unavailable")
            purposes = dam.get("Purposes", "Unknown")

            self._append_log("")
            self._append_log("=" * 60)
            self._append_log(f"Dam: {dam_name}")
            self._append_log(f"NID ID: {requested_nid_id}")
            self._append_log(f"Node ID: {int(node_id) if pd.notna(node_id) else 'Unavailable'}")
            self._append_log(f"Purposes: {purposes}")
            self._append_log("=" * 60)
            self._append_log("")

    def restore_default_filters(self) -> None:
        """
        Restore thresholds equivalent to the original cascade-search script.

        Original values:
        - maximum storage greater than 100 acre-feet;
        - hydroelectric-purpose filter active;
        - estimated power-capacity threshold greater than 10.
        """

        self.minimum_storage_text.set("100")
        self.hydroelectric_only_var.set(True)
        self.power_threshold_text.set("10")
        self.filter_tab_view.set_power_filter_enabled(
            enabled=self.hydroelectric_only_var.get()
        )

        self._append_log(
            "Part 3 filters restored to the original algorithm defaults: "
            "storage > 100 acre-feet, hydroelectric only, and power threshold > 10 MW."
        )

    def apply_dam_filters(self) -> None:
        """
        Apply Part 3 filters to the initialized NID inventory.

        Workflow:
        1. Start with the Part 1 inventory, which has already had the automatic
           primary-dam condition applied: NID ID == Federal ID.
        2. Apply the user-defined maximum-storage lower limit.
        3. If requested, retain records whose Purposes contain Hydroelectric.
        4. For that hydroelectric subset, use mapped records only and apply the
           original estimated power calculation and lower threshold.

        Two result datasets are preserved:
        - filtered_dam_inventory: all selected inventory rows;
        - filtered_dam_inventory_matched: selected rows with NHD node IDs.

        The latter is intended for the downstream-search and cascade stages.
        """

        if self.state.initialization_result is None:
            messagebox.showwarning(
                APP_NAME,
                "Initialize the datasets in Part 1 before applying filters.",
            )
            return

        try:
            minimum_storage = parse_nonnegative_float(
                self.minimum_storage_text.get(),
                "Maximum storage lower limit",
            )

            hydroelectric_only = self.hydroelectric_only_var.get()

            power_threshold: Optional[float] = None
            if hydroelectric_only:
                power_threshold = parse_nonnegative_float(
                    self.power_threshold_text.get(),
                    "Estimated power threshold lower limit",
                )

        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error))
            return

        try:
            filter_result = apply_dam_filters_service(
                dam_inventory=self.state.initialization_result.dam_inventory,
                dam_inventory_matched=(
                    self.state.initialization_result.dam_inventory_matched
                ),
                minimum_storage=minimum_storage,
                hydroelectric_only=hydroelectric_only,
                power_threshold=power_threshold,
            )
        except ValueError as error:
            messagebox.showerror(
                APP_NAME,
                f"Could not apply dam filters.\n\nDetails: {error}",
            )
            return

        filtered_inventory = filter_result.filtered_dam_inventory
        filtered_matched = filter_result.filtered_dam_inventory_matched

        after_storage_count = filter_result.after_storage_count
        after_purpose_count = filter_result.after_purpose_count
        before_power_count = filter_result.before_power_count

        # Store service outputs as application state for Parts 4–6.
        self.state.filtered_dam_inventory = filtered_inventory
        self.state.filtered_dam_inventory_matched = filtered_matched

        matched_count = len(filtered_matched)
        selected_count = len(filtered_inventory)

        if hydroelectric_only:
            power_description = f"> {power_threshold:g}"
        else:
            power_description = "Not applied"

        result_rows = [
            (
                "Primary-dam rule",
                "Automatically applied: NID ID = Federal ID",
            ),
            (
                "Maximum storage lower limit",
                f"> {minimum_storage:g} acre-feet",
            ),
            (
                "Dams remaining after storage filter",
                f"{after_storage_count:,}",
            ),
            (
                "Hydroelectric-purpose filter",
                "Enabled" if hydroelectric_only else "Disabled",
            ),
            (
                "Dams remaining after purpose filter",
                f"{after_purpose_count:,}",
            ),
            (
                "Dams with NHD node mappings before power filter",
                f"{before_power_count:,}",
            ),
            (
                "Estimated power threshold",
                power_description,
            ),
            (
                "Final selected dams",
                f"{selected_count:,}",
            ),
            (
                "Final selected dams with NHD node mappings",
                f"{matched_count:,}",
            ),
        ]

        self.filter_tab_view.display_results(result_rows)

        self.filter_status_text.set(
            f"Filters applied: {selected_count:,} selected dam(s), "
            f"{matched_count:,} mapped to NHD nodes."
        )

        self._append_log("")
        self._append_log("=" * 60)
        self._append_log("Part 3 — Dam Filters Applied")
        self._append_log("=" * 60)
        self._append_log(
            "Automatic primary-dam rule: NID ID = Federal ID"
        )
        self._append_log(
            f"Maximum storage criterion: > {minimum_storage:g} acre-feet"
        )
        self._append_log(
            f"Dams after storage criterion: {after_storage_count:,}"
        )
        self._append_log(
            "Hydroelectric-purpose filter: "
            f"{'Enabled' if hydroelectric_only else 'Disabled'}"
        )

        if hydroelectric_only:
            self._append_log(
                f"Dams after hydroelectric-purpose criterion: "
                f"{after_purpose_count:,}"
            )
            self._append_log(
                f"Mapped hydroelectric dams before power criterion: "
                f"{before_power_count:,}"
            )
            self._append_log(
                f"Estimated power criterion: {power_threshold:g} MW"
            )


        self._append_log(f"Final selected dams: {selected_count:,}")
        self._append_log(
            f"Final selected dams with NHD node mappings: {matched_count:,}"
        )
        self._append_log(
            "The filtered datasets are now available to later application stages."
        )
        self._append_log("")

    def start_downstream_search(self) -> None:
        """
        Validate Part 4 settings and start the graph search in a worker thread.

        The search always uses the complete initialized matched inventory:
        self.state.initialization_result.dam_inventory_matched

        It does not use Part 3's filtered set. This is intentional because
        Part 4 produces a broad reusable dam-to-dam connectivity reference.
        """

        if self.state.initialization_result is None:
            messagebox.showwarning(
                APP_NAME,
                "Initialize the datasets in Part 1 before running the downstream search.",
            )
            return

        if (
            self.downstream_search_thread is not None
            and self.downstream_search_thread.is_alive()
        ):
            messagebox.showinfo(
                APP_NAME,
                "A downstream dam search is already running.",
            )
            return

        try:
            maximum_distance = parse_nonnegative_float(
                self.maximum_downstream_distance_text.get(),
                "Maximum downstream search distance",
            )
        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error))
            return

        if maximum_distance == 0:
            messagebox.showwarning(
                APP_NAME,
                "Maximum downstream search distance must be greater than zero.",
            )
            return

        matched_dams = self.state.initialization_result.dam_inventory_matched

        if matched_dams.empty:
            messagebox.showwarning(
                APP_NAME,
                "No matched dams are available for downstream searching.",
            )
            return

        output_file = self.downstream_links_cache_path

        self.downstream_tab_view.set_search_enabled(
            enabled=False
        )
        self.downstream_search_status_text.set(
            "Searching downstream dam links..."
        )

        self._append_log("")
        self._append_log("=" * 60)
        self._append_log("Part 4 — Downstream Dam Search Started")
        self._append_log("=" * 60)
        self._append_log(
            f"Matched dams to process: {len(matched_dams):,}"
        )
        self._append_log(
            f"Maximum downstream search distance: {maximum_distance:g} miles"
        )
        self._append_log(
            f"Application-managed downstream-link cache: {output_file}"
        )

        self.downstream_search_thread = threading.Thread(
            target=self._run_downstream_search_worker,
            args=(maximum_distance, output_file),
            daemon=True,
        )
        self.downstream_search_thread.start()

    def _run_downstream_search_worker(
        self,
        maximum_distance: float,
        output_file: Path,
    ) -> None:
        """
        Build the Part 4 downstream-link reference table in a worker thread.

        The service performs graph traversal and DataFrame construction. This
        worker remains responsible for elapsed-time progress estimates,
        application-managed artifact persistence, and main-thread messages.
        """

        try:
            if self.state.initialization_result is None:
                raise RuntimeError(
                    "Initialization state was unavailable when the search "
                    "began."
                )

            matched_dams = (
                self.state.initialization_result.dam_inventory_matched
            )
            start_time = time.time()

            def report_progress(
                processed: int,
                total: int,
            ) -> None:
                """Send worker progress without accessing Tkinter widgets."""

                elapsed_seconds = time.time() - start_time
                processing_rate = (
                    processed / elapsed_seconds
                    if elapsed_seconds > 0
                    else 0.0
                )
                remaining_seconds = (
                    (total - processed) / processing_rate
                    if processing_rate > 0
                    else 0.0
                )

                self.ui_message_queue.put(
                    (
                        "downstream_progress",
                        {
                            "processed": processed,
                            "total": total,
                            "elapsed_seconds": elapsed_seconds,
                            "remaining_seconds": remaining_seconds,
                        },
                    )
                )

            downstream_result = build_downstream_links(
                graph=self.state.initialization_result.graph,
                matched_dams=matched_dams,
                maximum_distance=maximum_distance,
                progress_callback=report_progress,
            )

            if downstream_result.duplicate_node_count:
                self.ui_message_queue.put(
                    (
                        "log",
                        (
                            "Part 4 notice: "
                            f"{downstream_result.duplicate_node_count:,} "
                            "NHD node(s) contain multiple matched NID dams. "
                            "The alphanumerically first NID ID at each "
                            "downstream target node is used as the direct "
                            "downstream reference."
                        ),
                    )
                )

            save_application_artifact(
                artifact_file=output_file,
                artifact_type="downstream_links",
                payload={
                    "downstream_links": (
                        downstream_result.downstream_links
                    ),
                    "maximum_distance_miles": maximum_distance,
                    "total_matched_dams": (
                        downstream_result.total_dams
                    ),
                    "linked_dam_count": (
                        downstream_result.linked_dams
                    ),
                },
            )

            elapsed_total_seconds = time.time() - start_time

            self.ui_message_queue.put(
                (
                    "downstream_success",
                    {
                        "dataframe": (
                            downstream_result.downstream_links
                        ),
                        "cache_file": output_file,
                        "total_dams": downstream_result.total_dams,
                        "linked_dams": downstream_result.linked_dams,
                        "duplicate_node_count": (
                            downstream_result.duplicate_node_count
                        ),
                        "maximum_distance": maximum_distance,
                        "elapsed_seconds": elapsed_total_seconds,
                    },
                )
            )

        except Exception as error:
            self.ui_message_queue.put(
                (
                    "downstream_failure",
                    str(error),
                )
            )

    def export_downstream_links_csv(self) -> None:
        """
        Export the current Part 4 DataFrame to a user-selected CSV file.

        This action never changes self.state.downstream_links and does not replace
        the application-managed pickle artifact. The export is read-only from
        the application's point of view.
        """

        if self.state.downstream_links is None:
            messagebox.showwarning(
                APP_NAME,
                "No downstream-link data are available to export.",
            )
            return

        selected_path = filedialog.asksaveasfilename(
            title="Export Downstream Dam Links",
            defaultextension=".csv",
            initialfile=DEFAULT_DOWNSTREAM_LINKS_EXPORT_NAME,
            filetypes=[
                ("CSV files", "*.csv"),
                ("All files", "*.*"),
            ],
        )

        if not selected_path:
            return

        output_file = Path(selected_path)

        try:
            export_dataframe_to_csv(
                self.state.downstream_links,
                output_file,
            )

            self._append_log(
                f"Exported downstream-link CSV: {output_file}"
            )

            messagebox.showinfo(
                APP_NAME,
                f"Downstream-link CSV exported successfully:\n{output_file}",
            )

        except Exception as error:
            self._append_log(
                f"Downstream-link CSV export failed: {error}"
            )

            messagebox.showerror(
                APP_NAME,
                f"Could not export downstream-link CSV.\n\nDetails: {error}",
            )

    def restore_default_cascade_settings(self) -> None:
        """
        Restore the requested default Part 5 settings.

        Default behavior:
        - at least two hydroelectric dams per system;
        - ownership continuity is not required.
        """

        self.minimum_hydroelectric_dams_text.set(
            str(DEFAULT_MIN_HYDROELECTRIC_DAMS_PER_CASCADE)
        )
        self.require_same_owner_var.set(DEFAULT_REQUIRE_SAME_OWNER)

        self._append_log(
            "Part 5 settings restored: minimum hydroelectric dams = 2; "
            "same-owner continuation requirement = False."
        )

    def start_cascade_construction(self) -> None:
        """
        Validate prerequisites and begin cascade construction in a worker.

        Cascade construction requires:
        1. Part 1 initialized graph/inventory state;
        2. Part 3 selected, matched root candidates;
        3. Part 4 downstream direct-link reference results.

        The Part 4 table—not a new river-network traversal—is the sole source
        of dam-to-dam graph edges in this stage.
        """

        if self.state.initialization_result is None:
            messagebox.showwarning(
                APP_NAME,
                "Initialize data in Part 1 before constructing cascades.",
            )
            return

        if self.state.filtered_dam_inventory_matched is None:
            messagebox.showwarning(
                APP_NAME,
                "Apply Part 3 filters before constructing cascade systems.",
            )
            return

        if self.state.downstream_links is None:
            messagebox.showwarning(
                APP_NAME,
                "Build the Part 4 downstream dam reference CSV before constructing cascades.",
            )
            return

        if (
            self.cascade_construction_thread is not None
            and self.cascade_construction_thread.is_alive()
        ):
            messagebox.showinfo(
                APP_NAME,
                "Cascade-system construction is already running.",
            )
            return

        try:
            minimum_hydroelectric_dams = parse_nonnegative_integer(
                self.minimum_hydroelectric_dams_text.get(),
                "Minimum hydroelectric dams per cascading system",
            )
        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error))
            return

        require_same_owner = self.require_same_owner_var.get()

        cascade_cache_file = self.cascade_systems_cache_path

        self.cascade_builder_tab_view.set_construct_enabled(enabled=False)
        self.cascade_construction_status_text.set(
            "Constructing cascade-system graphs..."
        )

        self._append_log("")
        self._append_log("=" * 60)
        self._append_log("Part 5 — Cascade-System Construction Started")
        self._append_log("=" * 60)
        self._append_log(
            f"Part 3 matched root candidates: "
            f"{len(self.state.filtered_dam_inventory_matched):,}"
        )
        self._append_log(
            f"Minimum hydroelectric dams per system: "
            f"{minimum_hydroelectric_dams:,}"
        )
        self._append_log(
            f"Require same owner in each chain: {require_same_owner}"
        )
        self._append_log(
            "Minimum total dams per accepted cascading system: 2"
        )

        self.cascade_construction_thread = threading.Thread(
            target=self._run_cascade_construction_worker,
            args=(
                minimum_hydroelectric_dams,
                require_same_owner,
                cascade_cache_file,
            ),
            daemon=True,
        )
        self.cascade_construction_thread.start()


    def _run_cascade_construction_worker(
        self,
        minimum_hydroelectric_dams: int,
        require_same_owner: bool,
        cascade_cache_file: Path,
    ) -> None:
        """
        Build Part 5 cascade systems in a worker thread.

        Analysis is delegated to the cascade service. This worker remains
        responsible only for validating application state, persisting the
        application-managed artifact, and reporting results to the Tkinter
        main thread through ui_message_queue.
        """

        try:
            if self.state.initialization_result is None:
                raise RuntimeError(
                    "Initialization data are unavailable."
                )

            if self.state.filtered_dam_inventory_matched is None:
                raise RuntimeError(
                    "Part 3 filtered candidates are unavailable."
                )

            if self.state.downstream_links is None:
                raise RuntimeError(
                    "Part 4 downstream links are unavailable."
                )

            construction_result = construct_cascade_systems(
                matched_inventory=(
                    self.state.initialization_result.dam_inventory_matched
                ),
                filtered_dam_inventory_matched=(
                    self.state.filtered_dam_inventory_matched
                ),
                downstream_links=self.state.downstream_links,
                minimum_hydroelectric_dams=(
                    minimum_hydroelectric_dams
                ),
                require_same_owner=require_same_owner,
            )

            # Persist every Part 5 output together. This keeps graphs, edge
            # rows, summary rows, and construction settings aligned in one
            # trusted application-managed private artifact.
            save_application_artifact(
                artifact_file=cascade_cache_file,
                artifact_type="cascade_systems",
                payload={
                    "cascade_graphs": construction_result.cascade_graphs,
                    "edge_dataframe": (
                        construction_result.edge_dataframe
                    ),
                    "summary_dataframe": (
                        construction_result.summary_dataframe
                    ),
                    "minimum_hydroelectric_dams": (
                        minimum_hydroelectric_dams
                    ),
                    "require_same_owner": require_same_owner,
                    "source_downstream_cache": str(
                        self.downstream_links_cache_path
                    ),
                },
            )

            self.ui_message_queue.put(
                (
                    "cascade_construction_success",
                    {
                        "cascade_graphs": (
                            construction_result.cascade_graphs
                        ),
                        "edge_dataframe": (
                            construction_result.edge_dataframe
                        ),
                        "summary_dataframe": (
                            construction_result.summary_dataframe
                        ),
                        "cache_file": cascade_cache_file,
                        "root_candidate_count": (
                            construction_result.root_candidate_count
                        ),
                        "covered_candidate_count": (
                            construction_result.covered_candidate_count
                        ),
                        "true_root_count": (
                            construction_result.true_root_count
                        ),
                        "two_dam_chain_count": (
                            construction_result.two_dam_chain_count
                        ),
                        "mergeable_chain_count": (
                            construction_result.mergeable_chain_count
                        ),
                        "system_count": (
                            construction_result.system_count
                        ),
                        "edge_count": construction_result.edge_count,
                        "multi_root_system_count": (
                            construction_result.multi_root_system_count
                        ),
                        "cycle_count": construction_result.cycle_count,
                        "minimum_hydroelectric_dams": (
                            minimum_hydroelectric_dams
                        ),
                        "require_same_owner": require_same_owner,
                    },
                )
            )

        except Exception as error:
            self.ui_message_queue.put(
                (
                    "cascade_construction_failure",
                    str(error),
                )
            )
    def export_cascade_edges_csv(self) -> None:
        """
        Export the current cascade-system edge list as a user-readable CSV.

        This export does not alter the in-memory graph objects or the
        application-managed cascade artifact.
        """

        if self.state.cascade_systems_edges is None:
            messagebox.showwarning(
                APP_NAME,
                "No cascade-system edge data are available to export.",
            )
            return

        selected_path = filedialog.asksaveasfilename(
            title="Export Cascade System Edge List",
            defaultextension=".csv",
            initialfile=DEFAULT_CASCADE_SYSTEMS_EXPORT_NAME,
            filetypes=[
                ("CSV files", "*.csv"),
                ("All files", "*.*"),
            ],
        )

        if not selected_path:
            return

        output_file = Path(selected_path)

        try:
            export_dataframe_to_csv(
                self.state.cascade_systems_edges,
                output_file,
            )

            self._append_log(
                f"Exported cascade-system edge-list CSV: {output_file}"
            )

        except Exception as error:
            self._append_log(
                f"Cascade-system edge-list CSV export failed: {error}"
            )

            messagebox.showerror(
                APP_NAME,
                f"Could not export cascade-system edge list.\n\nDetails: {error}",
            )

    def export_cascade_summary_csv(self) -> None:
        """
        Export the current per-system cascade summary as a user-readable CSV.

        This export is intentionally one-way and is never read back into the
        application workflow.
        """

        if self.state.cascade_systems_summary is None:
            messagebox.showwarning(
                APP_NAME,
                "No cascade-system summary data are available to export.",
            )
            return

        selected_path = filedialog.asksaveasfilename(
            title="Export Cascade System Summary",
            defaultextension=".csv",
            initialfile=DEFAULT_CASCADE_SUMMARY_EXPORT_NAME,
            filetypes=[
                ("CSV files", "*.csv"),
                ("All files", "*.*"),
            ],
        )

        if not selected_path:
            return

        output_file = Path(selected_path)

        try:
            export_dataframe_to_csv(
                self.state.cascade_systems_summary,
                output_file,
            )

            self._append_log(
                f"Exported cascade-system summary CSV: {output_file}"
            )

        except Exception as error:
            self._append_log(
                f"Cascade-system summary CSV export failed: {error}"
            )

            messagebox.showerror(
                APP_NAME,
                f"Could not export cascade-system summary.\n\nDetails: {error}",
            )

    def select_cascade_system_from_query_result(
        self,
        system_id: str,
    ) -> None:
        """
        Activate a system selected from the Part 6 query-results table.

        The CascadeQueryTab view supplies the selected Treeview item ID. The
        controller verifies that the system remains present in current session
        state before updating the selected-system variable and overview.
        """

        if system_id not in self.state.cascade_graphs:
            return

        self.selected_system_id_text.set(system_id)
        self._update_selected_system_details()

    def refresh_cascade_system_list(self) -> None:
        """
        Restore the complete set of constructed systems in Part 6.

        The Show All System IDs button deliberately clears query restrictions,
        ensuring the combobox and results table return to their full state.
        """

        if not self.state.cascade_graphs:
            self.cascade_query_tab_view.set_system_ids([])

            self.selected_system_id_text.set("")
            self.cascade_query_status_text.set(
                "No cascade systems are currently available."
            )

            self._display_cascade_query_matches([])

            self.cascade_query_tab_view.display_system_details(
                [
                    (
                        "Status",
                        "No cascades have been constructed under the current criteria.",
                    )
                ]
            )
            return

        self.clear_cascade_system_query()


    def _get_selected_cascade_graph(self) -> tuple[Optional[str], Optional[nx.DiGraph]]:
        """
        Validate the selected system ID and return its NetworkX graph.

        Returns:
            tuple:
                - (system_id, graph) for a valid selected system;
                - (None, None) and a user warning for an invalid selection.
        """

        if not self.state.cascade_graphs:
            messagebox.showwarning(
                APP_NAME,
                "No cascade systems are available. Run Part 5 first.",
            )
            return None, None

        system_id = self.selected_system_id_text.get().strip()

        if not system_id:
            messagebox.showwarning(
                APP_NAME,
                "Select or enter a cascade System ID.",
            )
            return None, None

        cascade_graph = self.state.cascade_graphs.get(system_id)

        if cascade_graph is None:
            messagebox.showwarning(
                APP_NAME,
                f"No constructed cascade system was found for System ID '{system_id}'.",
            )
            return None, None

        return system_id, cascade_graph

    def _display_cascade_query_matches(
        self,
        matching_system_ids: list[str],
    ) -> None:
        """
        Convert matching in-memory cascade graphs into Part 6 view rows.
        """

        match_rows: list[tuple[str, str, int, int]] = []

        for system_id in matching_system_ids:
            cascade_graph = self.state.cascade_graphs[system_id]

            root_dams = system_root_dam_ids(cascade_graph)
            hydroelectric_count = system_hydroelectric_dam_count(
                cascade_graph
            )

            match_rows.append(
                (
                    system_id,
                    "; ".join(root_dams),
                    cascade_graph.number_of_nodes(),
                    hydroelectric_count,
                )
            )

        self.cascade_query_tab_view.display_matches(match_rows)

    def clear_cascade_system_query(self) -> None:
        """
        Disable all filters and display every constructed cascade system.

        This is also useful after a restrictive search, because it restores
        the System ID combobox to the complete constructed-system set.
        """

        self.query_state_enabled_var.set(False)
        self.query_nid_enabled_var.set(False)
        self.query_river_enabled_var.set(False)

        self.query_state_text.set("")
        self.query_nid_text.set("")
        self.query_river_text.set("")

        self.cascade_query_tab_view.update_query_control_states()
        self.run_cascade_system_query()


    def run_cascade_system_query(self) -> None:
        """
        Find cascade systems meeting the enabled Part 6 criteria.

        Enabled criteria combine with AND logic:

        - State: the system contains at least one dam in the requested state.
        - NID ID: the system contains the requested dam ID.
        - River: the system contains at least one dam whose NID river/stream
          name contains the entered search text, case-insensitively.

        The state and river criteria are existential at the system level. That
        means they may be satisfied by the same dam or by different dams in
        the same connected cascade system.
        """

        if not self.state.cascade_graphs:
            messagebox.showwarning(
                APP_NAME,
                "No cascade systems are available. Run Part 5 first.",
            )
            return

        state_filter_enabled = self.query_state_enabled_var.get()
        nid_filter_enabled = self.query_nid_enabled_var.get()
        river_filter_enabled = self.query_river_enabled_var.get()

        requested_state = self.query_state_text.get().strip().upper()
        requested_nid_id = self.query_nid_text.get().strip()
        requested_river = self.query_river_text.get().strip()

        # Validate only active criteria. Disabled query fields have no effect.
        if state_filter_enabled:
            if (
                len(requested_state) != 2
                or not requested_state.isalpha()
            ):
                messagebox.showwarning(
                    APP_NAME,
                    "State search requires a two-letter state abbreviation, "
                    "such as GA, AL, or NY.",
                )
                return

        if nid_filter_enabled and not requested_nid_id:
            messagebox.showwarning(
                APP_NAME,
                "Enter an NID ID or disable the NID ID query criterion.",
            )
            return

        if river_filter_enabled and not requested_river:
            messagebox.showwarning(
                APP_NAME,
                "Enter a river/stream name or disable the river query criterion.",
            )
            return

        try:
            query_result = query_cascade_systems(
                cascade_graphs=self.state.cascade_graphs,
                requested_state=(
                    requested_state
                    if state_filter_enabled
                    else None
                ),
                requested_nid_id=(
                    requested_nid_id
                    if nid_filter_enabled
                    else None
                ),
                requested_river=(
                    requested_river
                    if river_filter_enabled
                    else None
                ),
            )
        except ValueError as error:
            # Input is already validated above, so this is a defensive service
            # boundary for future callers or future UI changes.
            messagebox.showwarning(
                APP_NAME,
                str(error),
            )
            return

        matching_system_ids = query_result.matching_system_ids
        matching_details = query_result.matching_details

        # Preserve the exact system set used for the current query. Part 6's
        # national overview map uses this value rather than independently
        # rerunning or potentially differing from the visible query results.
        self.state.last_cascade_query_system_ids = matching_system_ids.copy()

        # Update both the results table and the selected-system combobox.
        self._display_cascade_query_matches(matching_system_ids)

        self.cascade_query_tab_view.set_system_ids(
            matching_system_ids
        )

        if matching_system_ids:
            current_system_id = self.selected_system_id_text.get().strip()

            if current_system_id not in matching_system_ids:
                self.selected_system_id_text.set(matching_system_ids[0])

            self._update_selected_system_details()

        else:
            self.selected_system_id_text.set("")

            self.cascade_query_tab_view.display_system_details(
                [
                    (
                        "Status",
                        "No cascading systems meet the active query criteria.",
                    )
                ]
            )

        active_criteria_descriptions: list[str] = []

        if state_filter_enabled:
            active_criteria_descriptions.append(
                f"state = {requested_state}"
            )

        if nid_filter_enabled:
            active_criteria_descriptions.append(
                f"NID ID = {requested_nid_id}"
            )

        if river_filter_enabled:
            active_criteria_descriptions.append(
                f"river/stream contains {requested_river!r}"
            )

        criteria_description = (
            "; ".join(active_criteria_descriptions)
            if active_criteria_descriptions
            else "No active filters; all constructed systems shown"
        )

        self.cascade_query_status_text.set(
            f"{len(matching_system_ids):,} of "
            f"{len(self.state.cascade_graphs):,} cascade system(s) match."
        )

        # ------------------------------------------------------------------
        # Shared-log query output
        # ------------------------------------------------------------------
        self._append_log("")
        self._append_log("=" * 60)
        self._append_log("Part 6 — Cascade-System Query Results")
        self._append_log("=" * 60)
        self._append_log(f"Search criteria: {criteria_description}")
        self._append_log(
            f"Matching systems: {len(matching_system_ids):,} of "
            f"{len(self.state.cascade_graphs):,}"
        )

        if not matching_system_ids:
            self._append_log(
                "No cascading systems met all enabled query criteria."
            )
            self._append_log("")
            return

        self._append_log("")

        for system_id in matching_system_ids:
            cascade_graph = self.state.cascade_graphs[system_id]
            root_dam_ids = system_root_dam_ids(cascade_graph)
            hydroelectric_count = system_hydroelectric_dam_count(
                cascade_graph
            )

            self._append_log(
                f"System ID: {system_id}"
            )
            self._append_log(
                f"  Root Dam ID(s): {'; '.join(root_dam_ids)}"
            )
            self._append_log(
                f"  Total Dams: {cascade_graph.number_of_nodes():,}"
            )
            self._append_log(
                f"  Hydroelectric Dams: {hydroelectric_count:,}"
            )

            details = matching_details[system_id]

            if state_filter_enabled:
                self._append_log(
                    f"  State-matching Dam ID(s): "
                    f"{'; '.join(details['state'])}"
                )

            if nid_filter_enabled:
                self._append_log(
                    f"  NID-matching Dam ID(s): "
                    f"{'; '.join(details['nid'])}"
                )

            if river_filter_enabled:
                self._append_log(
                    f"  River-matching Dam(s): "
                    f"{'; '.join(details['river'])}"
                )

            self._append_log("")

        self._append_log(
            "Select a matching system in the results table to inspect its "
            "detailed graph report or generate an interactive map."
        )
        self._append_log("")

    def _update_selected_system_details(self) -> None:
        """
        Populate the Part 6 system overview table for the current selection.

        This produces a compact high-level view. The full dam-node and
        downstream-edge detail is written by print_selected_cascade_graph().
        """

        system_id = self.selected_system_id_text.get().strip()
        cascade_graph = self.state.cascade_graphs.get(system_id)

        if cascade_graph is None:
            self.cascade_query_tab_view.display_system_details(
                [
                    (
                        "Status",
                        "Select a valid System ID to display its overview.",
                    )
                ]
            )
            return

        root_dams = sorted(
            dam_id
            for dam_id in cascade_graph.nodes
            if cascade_graph.in_degree(dam_id) == 0
        )

        terminal_dams = sorted(
            dam_id
            for dam_id in cascade_graph.nodes
            if cascade_graph.out_degree(dam_id) == 0
        )

        hydroelectric_count = sum(
            1
            for _, attributes in cascade_graph.nodes(data=True)
            if bool(attributes.get("is_hydroelectric", False))
        )

        root_descriptions = "; ".join(
            (
                f"{dam_id} "
                f"({display_value(cascade_graph.nodes[dam_id].get('name'))})"
            )
            for dam_id in root_dams
        )

        terminal_descriptions = "; ".join(
            (
                f"{dam_id} "
                f"({display_value(cascade_graph.nodes[dam_id].get('name'))})"
            )
            for dam_id in terminal_dams
        )

        self.cascade_query_tab_view.display_system_details(
            [
                ("System ID", system_id),
                (
                    "Total dams",
                    f"{cascade_graph.number_of_nodes():,}",
                ),
                (
                    "Direct downstream links",
                    f"{cascade_graph.number_of_edges():,}",
                ),
                (
                    "Hydroelectric dams",
                    f"{hydroelectric_count:,}",
                ),
                (
                    "Root dam(s)",
                    root_descriptions or "Unavailable",
                ),
                (
                    "Terminal dam(s)",
                    terminal_descriptions or "Unavailable",
                ),
            ]
        )

        self.cascade_query_status_text.set(
            f"System {system_id}: "
            f"{cascade_graph.number_of_nodes():,} dam(s), "
            f"{cascade_graph.number_of_edges():,} direct link(s)."
        )

    def print_selected_cascade_graph(self) -> None:
        """
        Write a detailed selected-cascade report to the shared Application Log.

        This is the Tkinter equivalent of the original print_cascade_graph()
        function. It explicitly iterates graph edges rather than assuming a
        linear chain, correctly representing merged systems with multiple
        upstream roots converging downstream.
        """

        system_id, cascade_graph = self._get_selected_cascade_graph()

        if system_id is None or cascade_graph is None:
            return

        root_dams = sorted(
            dam_id
            for dam_id in cascade_graph.nodes
            if cascade_graph.in_degree(dam_id) == 0
        )

        terminal_dams = sorted(
            dam_id
            for dam_id in cascade_graph.nodes
            if cascade_graph.out_degree(dam_id) == 0
        )

        hydroelectric_count = sum(
            1
            for _, attributes in cascade_graph.nodes(data=True)
            if bool(attributes.get("is_hydroelectric", False))
        )

        root_descriptions = ", ".join(
            (
                f"{dam_id} "
                f"({display_value(cascade_graph.nodes[dam_id].get('name'))})"
            )
            for dam_id in root_dams
        )

        terminal_descriptions = ", ".join(
            (
                f"{dam_id} "
                f"({display_value(cascade_graph.nodes[dam_id].get('name'))})"
            )
            for dam_id in terminal_dams
        )

        self._append_log("")
        self._append_log("=" * 60)
        self._append_log(f"Cascade System: {system_id}")
        self._append_log("=" * 60)
        self._append_log(
            f"Number of nodes (dams): {cascade_graph.number_of_nodes():,}"
        )
        self._append_log(
            f"Number of edges (downstream links): "
            f"{cascade_graph.number_of_edges():,}"
        )
        self._append_log(
            f"Root dam(s): {root_descriptions or 'Unavailable'}"
        )
        self._append_log(
            f"Terminal dam(s): {terminal_descriptions or 'Unavailable'}"
        )
        self._append_log(
            f"Hydroelectric dams in this system: {hydroelectric_count:,}"
        )

        self._append_log("")
        self._append_log("Nodes:")

        # Sort by NID ID for stable and repeatable research output.
        for dam_id in sorted(cascade_graph.nodes):
            attributes = cascade_graph.nodes[dam_id]

            dam_name = display_value(attributes.get("name"))
            owner = display_value(attributes.get("owner"))
            latitude = display_value(attributes.get("Latitude"))
            longitude = display_value(attributes.get("Longitude"))
            purposes = display_value(attributes.get("purposes"))

            hydroelectric_tag = (
                "[hydroelectric]"
                if bool(attributes.get("is_hydroelectric", False))
                else "[non-hydroelectric]"
            )

            self._append_log(
                f"  {hydroelectric_tag} {dam_id}: {dam_name!r} "
                f"(owner={owner}, lat={latitude}, lon={longitude})"
            )
            self._append_log(f"    Purposes: {purposes}")

        self._append_log("")
        self._append_log("Edges:")

        # Sort source and target IDs to keep log output deterministic.
        for upstream_dam_id, downstream_dam_id, edge_attributes in sorted(
            cascade_graph.edges(data=True),
            key=lambda edge: (str(edge[0]), str(edge[1])),
        ):
            distance = edge_attributes.get("distance_miles")

            if distance is None:
                distance_text = "unavailable"
            else:
                try:
                    distance_text = f"{float(distance):.2f} miles"
                except (TypeError, ValueError):
                    distance_text = "unavailable"

            upstream_name = display_value(
                cascade_graph.nodes[upstream_dam_id].get("name")
            )
            downstream_name = display_value(
                cascade_graph.nodes[downstream_dam_id].get("name")
            )

            self._append_log(
                f"  {upstream_dam_id} ({upstream_name}) -> "
                f"{downstream_dam_id} ({downstream_name}): "
                f"{distance_text}"
            )

        self._append_log("")
        self._append_log(
            f"Printed detailed information for cascade system {system_id}."
        )

    def generate_selected_cascade_map(self) -> None:
        """
        Generate and open an interactive map for the selected cascade system.

        Map rendering and file output are delegated to map_service. This UI
        method retains only selection validation, status/log updates, dialog
        handling, and browser-launch behavior.
        """

        system_id, cascade_graph = self._get_selected_cascade_graph()

        if system_id is None or cascade_graph is None:
            return

        try:
            map_result = build_selected_cascade_map(
                system_id=system_id,
                cascade_graph=cascade_graph,
                maps_directory=self.cascade_maps_directory,
            )

        except ValueError as error:
            messagebox.showerror(
                APP_NAME,
                str(error),
            )
            return

        except Exception as error:
            self._append_log(
                f"Part 6 map generation failed for system "
                f"{system_id}: {error}"
            )

            messagebox.showerror(
                APP_NAME,
                "Could not generate the interactive cascade map.\n\n"
                f"Details: {error}",
            )
            return

        self.cascade_query_status_text.set(
            f"Saved and opened interactive map for system {system_id}."
        )

        self._append_log(
            f"Part 6 map saved for cascade system {system_id}: "
            f"{map_result.output_file}"
        )

        if map_result.skipped_node_count:
            self._append_log(
                f"Map note: {map_result.skipped_node_count:,} dam "
                "marker(s) were not drawn because coordinates were "
                "unavailable or invalid."
            )

        if map_result.skipped_edge_count:
            self._append_log(
                f"Map note: {map_result.skipped_edge_count:,} edge(s) "
                "were not drawn because one or both endpoint dams lacked "
                "valid coordinates."
            )

        try:
            webbrowser.open_new_tab(
                map_result.output_file.resolve().as_uri()
            )

        except Exception as error:
            self._append_log(
                "Map saved, but the browser could not be opened "
                f"automatically: {error}"
            )

            messagebox.showinfo(
                APP_NAME,
                "The interactive map was saved successfully, but could not "
                "be opened automatically.\n\n"
                f"Open this file manually:\n{map_result.output_file}",
            )

    def generate_query_results_conus_map(self) -> None:
        """
        Generate and open a CONUS overview map for the latest Part 6 query.

        The rendering and persistence work is delegated to map_service. This
        controller method maintains query-state validation, UI messaging, logs,
        and browser-launch behavior.
        """

        if not self.state.last_cascade_query_system_ids:
            messagebox.showwarning(
                APP_NAME,
                "No cascade systems are in the current query result. "
                "Run a Part 6 query first, or use Clear Filters / Show All "
                "Systems.",
            )
            return

        try:
            map_result = build_query_results_overview_map(
                cascade_graphs=self.state.cascade_graphs,
                selected_system_ids=self.state.last_cascade_query_system_ids,
                maps_directory=self.cascade_maps_directory,
                show_distance_labels=(
                    self.show_overview_distance_labels_var.get()
                ),
            )

        except ValueError as error:
            messagebox.showerror(
                APP_NAME,
                str(error),
            )
            return

        except Exception as error:
            self._append_log(
                "Part 6 CONUS overview map generation failed: "
                f"{error}"
            )

            messagebox.showerror(
                APP_NAME,
                "Could not generate the CONUS overview map.\n\n"
                f"Details: {error}",
            )
            return

        self.cascade_query_status_text.set(
            f"Saved CONUS overview map for "
            f"{map_result.mapped_system_count:,} "
            "query-matching cascade system(s)."
        )

        self._append_log(
            "Part 6 CONUS overview map saved for "
            f"{map_result.mapped_system_count:,} query-matching system(s): "
            f"{map_result.output_file}"
        )

        if map_result.skipped_node_count:
            self._append_log(
                f"CONUS map note: {map_result.skipped_node_count:,} dam "
                "marker(s) were not drawn because coordinates were "
                "unavailable or invalid."
            )

        if map_result.skipped_edge_count:
            self._append_log(
                f"CONUS map note: {map_result.skipped_edge_count:,} edge(s) "
                "were not drawn because one or both endpoint coordinates "
                "were unavailable."
            )

        try:
            webbrowser.open_new_tab(
                map_result.output_file.resolve().as_uri()
            )

        except Exception as error:
            self._append_log(
                "CONUS map was saved, but could not be opened "
                f"automatically: {error}"
            )

            messagebox.showinfo(
                APP_NAME,
                "The CONUS overview map was saved successfully, but could "
                "not be opened automatically.\n\n"
                f"Open this file manually:\n{map_result.output_file}",
            )
    def _append_log(self, message: str) -> None:
        """Append timestamped text to the UI log from the Tkinter main thread."""

        timestamp = time.strftime("%H:%M:%S")
        self.log_widget.configure(state=tk.NORMAL)
        self.log_widget.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state=tk.DISABLED)

    def start_initialization(self) -> None:
        """Start initialization once, in a worker thread."""

        if self.initialization_thread and self.initialization_thread.is_alive():
            messagebox.showinfo(APP_NAME, "Initialization is already running.")
            return

        self.initialization_tab_view.set_initialize_button_enabled(
            enabled=False
        )
        self.status_text.set("Initializing...")
        self.progress_text.set("Preparing local cache and source-data connections...")
        self._append_log("Starting initialization.")

        self.initialization_thread = threading.Thread(
            target=self._run_initialization_worker,
            daemon=True,
        )
        self.initialization_thread.start()

    def _run_initialization_worker(self) -> None:
        """
        Execute Part 1 initialization without blocking the Tkinter UI thread.
        """ 

        # This import is intentionally delayed. InitializationService imports
        # pynhd and pygeohydro; app.main() configures aiohttp DNS resolution
        # before this worker can begin.
        from cascade_research_tool.services.initialization_service import InitializationService

        def worker_log(message: str) -> None:
            self.ui_message_queue.put(("log", message))
            self.ui_message_queue.put(("progress", message))

        try:
            service = InitializationService(
                self.cache_dir,
                worker_log,
            )
            result = service.initialize()
            self.ui_message_queue.put(("success", result))
        except Exception as error:
            self.ui_message_queue.put(("failure", str(error)))

    def _process_worker_messages(self) -> None:
        """Process worker messages safely on the Tkinter UI thread."""

        try:
            while True:
                message_type, payload = self.ui_message_queue.get_nowait()

                if message_type == "log":
                    self._append_log(str(payload))

                elif message_type == "progress":
                    self.progress_text.set(str(payload))

                elif message_type == "success":
                    # Save the initialized graph, mappings, and matched dam
                    # inventory for use by the later workflow tabs.
                    self.state.initialization_result = payload

                    # The fingerprint is used when saving/loading named case
                    # studies to detect whether the NHD/NID matching context
                    # differs from the context used to create a study.
                    self.state.current_initialization_fingerprint = (
                        compute_initialization_fingerprint(payload)
                    )

                    # A named study can now be saved because Part 1 has
                    # established the baseline NHD/NID context.
                    self.case_study_controls_view.set_save_enabled(
                        enabled=True
                    )

                    # Populate the Part 1 statistics display.
                    self.initialization_tab_view.display_statistics(
                        payload.statistics
                    )

                    # Enable Part 2 only after the dependent network and dam
                    # inventory state exists in memory.
                    self.notebook.tab(self.node_explorer_tab, state="normal")

                    # Part 3 can now operate on the initialized primary-dam
                    # inventory and matched NHD-node dataset.
                    self.notebook.tab(self.filter_tab, state="normal")

                    # Part 4 uses the initialized full matched-dam inventory,
                    # NHD graph, and node mappings.
                    self.notebook.tab(self.downstream_tab, state="normal")

                    self.status_text.set("Initialization complete.")
                    self.progress_text.set(
                        "Data are ready. Use Part 2 to explore nodes, Part 3 "
                        "to filter dams, or Part 4 to build downstream links."
                    )
                    self.initialization_tab_view.set_initialize_button_enabled(enabled=True)

                    self._append_log("Initialization completed successfully.")
                    self._append_log(
                        "Part 2 — Explore Nodes, Part 3 — Filter Dams, and Part 4 — Build Downstream Links are now available."
                    )

                elif message_type == "downstream_progress":
                    processed = payload["processed"]
                    total = payload["total"]
                    elapsed_seconds = payload["elapsed_seconds"]
                    remaining_seconds = payload["remaining_seconds"]

                    progress_message = (
                        f"Part 4 search progress: {processed:,}/{total:,} dams "
                        f"processed; {elapsed_seconds:.1f}s elapsed; "
                        f"approximately {remaining_seconds:.1f}s remaining."
                    )

                    self.downstream_search_status_text.set(
                        f"Processing {processed:,} of {total:,} dams..."
                    )
                    self._append_log(progress_message)

                elif message_type == "downstream_success":
                    self.state.downstream_links = payload["dataframe"]
                    self.state.downstream_links_cache_file = payload["cache_file"]

                    # Downstream links are now available. Part 5 additionally
                    # checks that Part 3 filtering has been run before it
                    # permits cascade construction.
                    self.notebook.tab(
                        self.cascade_builder_tab,
                        state="normal",
                    )

                    self.downstream_tab_view.set_export_enabled(
                        enabled=True
                    )

                    total_dams = payload["total_dams"]
                    linked_dams = payload["linked_dams"]
                    maximum_distance = payload["maximum_distance"]
                    elapsed_seconds = payload["elapsed_seconds"]
                    duplicate_node_count = payload["duplicate_node_count"]

                    match_rate = (
                        (linked_dams / total_dams) * 100
                        if total_dams > 0
                        else 0.0
                    )

                    self.downstream_csv_path_text.set(
                        f"Application cache: {self.state.downstream_links_cache_file}"
                    )

                    self.downstream_search_status_text.set(
                        f"Complete: {linked_dams:,} downstream links found."
                    )

                    self.downstream_tab_view.set_search_enabled(
                        enabled=True
                    )

                    self.downstream_tab_view.display_results(
                        [
                            (
                                "Matched dams searched",
                                f"{total_dams:,}",
                            ),
                            (
                                "Maximum downstream search distance",
                                f"{maximum_distance:g} miles",
                            ),
                            (
                                "Dams with a downstream dam found",
                                f"{linked_dams:,}",
                            ),
                            (
                                "Dams without a downstream dam in range",
                                f"{total_dams - linked_dams:,}",
                            ),
                            (
                                "Downstream-link success rate",
                                f"{match_rate:.1f}%",
                            ),
                            (
                                "Nodes containing multiple matched dams",
                                f"{duplicate_node_count:,}",
                            ),
                            (
                                "Search and CSV export duration",
                                f"{elapsed_seconds:.1f} seconds",
                            ),
                            (
                                "Reference CSV",
                                str(self.state.downstream_links_cache_file),
                            ),
                        ]
                    )

                    self._append_log("")
                    self._append_log("=" * 60)
                    self._append_log("Part 4 — Downstream Dam Search Complete")
                    self._append_log("=" * 60)
                    self._append_log(
                        f"Processed {total_dams:,} matched dam(s) in "
                        f"{elapsed_seconds:.1f} seconds."
                    )
                    self._append_log(
                        f"Found downstream links for {linked_dams:,} of "
                        f"{total_dams:,} dams ({match_rate:.1f}%)."
                    )
                    self._append_log(
                        "Saved application-managed downstream-link artifact: "
                        f"{self.state.downstream_links_cache_file}"
                    )
                    self._append_log(
                        "The downstream-link reference data are now ready for "
                        "Part 5 cascade construction."
                    )
                    self._append_log("")

                elif message_type == "downstream_failure":
                    self.downstream_tab_view.set_search_enabled(
                        enabled=True
                    )
                    self.downstream_search_status_text.set(
                        "Downstream search failed."
                    )

                    self._append_log(
                        f"Part 4 downstream search failed: {payload}"
                    )

                    messagebox.showerror(
                        APP_NAME,
                        "The downstream dam search did not complete.\n\n"
                        f"Details: {payload}",
                    )
                elif message_type == "cascade_construction_success":
                    self.state.cascade_graphs = payload["cascade_graphs"]
                    self.state.cascade_systems_edges = payload["edge_dataframe"]
                    self.state.cascade_systems_summary = payload["summary_dataframe"]
                    self.state.cascade_systems_cache_file = payload["cache_file"]

                    # Part 6 uses the in-memory graphs produced by Part 5.
                    # It remains available even when the current criteria
                    # produce zero systems, so it can report that condition.
                    self.notebook.tab(
                        self.cascade_query_tab,
                        state="normal",
                    )

                    self.refresh_cascade_system_list()

                    self.cascade_builder_tab_view.set_edge_export_enabled(enabled=True)
                    self.cascade_builder_tab_view.set_summary_export_enabled(enabled=True)
                    
                    self.cascade_systems_csv_path_text.set(
                        f"Application cache: {self.state.cascade_systems_cache_file}"
                    )
                    self.cascade_summary_csv_path_text.set(
                        "CSV outputs are optional exports and are not read by the application."
                    )

                    self.cascade_builder_tab_view.set_construct_enabled(enabled=True)

                    system_count = payload["system_count"]
                    edge_count = payload["edge_count"]

                    self.cascade_construction_status_text.set(
                        f"Complete: {system_count:,} cascade system(s) constructed."
                    )

                    self.cascade_builder_tab_view.display_results(
                        [
                            (
                                "Part 3 matched root candidates",
                                f"{payload['root_candidate_count']:,}",
                            ),
                            (
                                "Root candidates covered by another candidate chain",
                                f"{payload['covered_candidate_count']:,}",
                            ),
                            (
                                "True upstream root candidates",
                                f"{payload['true_root_count']:,}",
                            ),
                            (
                                "Initial chains containing at least two dams",
                                f"{payload['two_dam_chain_count']:,}",
                            ),
                            (
                                "Minimum hydroelectric dams per system",
                                f"{payload['minimum_hydroelectric_dams']:,}",
                            ),
                            (
                                "Same-owner continuation required",
                                str(payload["require_same_owner"]),
                            ),
                            (
                                "Initial chains retained for system merging",
                                f"{payload['mergeable_chain_count']:,}",
                            ),
                            (
                                "Final connected cascade systems",
                                f"{system_count:,}",
                            ),
                            (
                                "Systems with multiple converging roots",
                                f"{payload['multi_root_system_count']:,}",
                            ),
                            (
                                "Direct downstream edges exported",
                                f"{edge_count:,}",
                            ),
                            (
                                "Cycle warnings encountered",
                                f"{payload['cycle_count']:,}",
                            ),
                        ]
                    )

                    self._append_log("")
                    self._append_log("=" * 60)
                    self._append_log(
                        "Part 5 — Cascade-System Construction Complete"
                    )
                    self._append_log("=" * 60)
                    self._append_log(
                        f"Part 3 matched root candidates: "
                        f"{payload['root_candidate_count']:,}"
                    )
                    self._append_log(
                        f"True upstream root candidates: "
                        f"{payload['true_root_count']:,}"
                    )
                    self._append_log(
                        f"Initial chains with at least two dams: "
                        f"{payload['two_dam_chain_count']:,}"
                    )
                    self._append_log(
                        f"Minimum hydroelectric dams per system: "
                        f"{payload['minimum_hydroelectric_dams']:,}"
                    )
                    self._append_log(
                        f"Same-owner continuation requirement: "
                        f"{payload['require_same_owner']}"
                    )
                    self._append_log(
                        f"Final cascade systems: {system_count:,}"
                    )
                    self._append_log(
                        f"Direct downstream edges exported: {edge_count:,}"
                    )
                    self._append_log(
                        "Saved application-managed cascade artifact: "
                        f"{self.state.cascade_systems_cache_file}"
                    )

                    if payload["cycle_count"]:
                        self._append_log(
                            "Warning: one or more cyclic downstream-link paths "
                            "were detected and safely terminated."
                        )

                    self._append_log(
                        "Cascade graphs are available in memory for the future "
                        "Part 6 query and visualization tools."
                    )
                    self._append_log("")

                elif message_type == "cascade_construction_failure":
                    self.cascade_builder_tab_view.set_construct_enabled(enabled=True)
                    self.cascade_construction_status_text.set(
                        "Cascade construction failed."
                    )

                    self._append_log(
                        f"Part 5 cascade-system construction failed: {payload}"
                    )

                    messagebox.showerror(
                        APP_NAME,
                        "Cascade-system construction did not complete.\n\n"
                        f"Details: {payload}",
                    )

                elif message_type == "failure":
                    self.status_text.set("Initialization failed.")
                    self.progress_text.set("Review the initialization log and correct the reported issue.")
                    self.initialization_tab_view.set_initialize_button_enabled(enabled=True)
                    self._append_log(f"Initialization failed: {payload}")
                    messagebox.showerror(
                        APP_NAME,
                        "Initialization did not complete.\n\n"
                        f"Details: {payload}",
                    )

        except queue.Empty:
            pass

        # Schedule the next queue check while the window exists.
        if self.winfo_exists():
            self.after(100, self._process_worker_messages)

    def _handle_window_close(self) -> None:
        """
        Close the application.

        The worker is daemonized because NHD/HTTP libraries do not necessarily
        support safe interruption at arbitrary points. Atomic cache writes
        ensure an interrupted download/write is not accepted as a valid cache.
        """

        if self.initialization_thread and self.initialization_thread.is_alive():
            if not messagebox.askyesno(
                APP_NAME,
                "Initialization is still running. Exit the application?",
            ):
                return

        self.destroy()