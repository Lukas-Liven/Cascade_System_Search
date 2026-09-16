"""
Cascade Research Tool — Initial Data Setup Application

This first-stage Tkinter application:
1. Creates a user-local cache directory.
2. Downloads/caches the enhanced NHD river-network graph.
3. Downloads/caches CONUS GeoConnex dam data.
4. Downloads/caches and checksum-validates the ResNet crosswalk.
5. Loads the National Inventory of Dams through pygeohydro.
6. Maps NID dams to NHD graph nodes through:
   a. GeoConnex provider_id -> COMID mappings
   b. ResNet NID -> COMID mappings
7. Displays initialization and COMID-matching statistics.

Security notes:
- The NetworkX graph is cached as a pickle because it is practical for the
  large, attribute-rich NHD graph. Pickle is NOT safe for untrusted files.
  This program only loads its own cache from a private user cache directory.
  Do NOT replace cache files with files received from another person/system.
- ResNet is downloaded from the Zenodo record API and verified against the
  checksum published in the record metadata when one is available.
- Downloads use HTTPS, explicit timeouts, streaming, size limits, and atomic
  replacement so partially downloaded files are never treated as valid caches.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import queue
import re
import stat
import threading
import time
import math
import heapq
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
import requests
import tkinter as tk
from tkinter import messagebox, ttk
import html
import webbrowser

import geopandas as gpd
import pynhd
from pynhd import GeoConnex
import pygeohydro as gh
import networkx as nx
import folium

# ---------------------------------------------------------------------------
# Application and data-source constants
# ---------------------------------------------------------------------------

APP_NAME = "Cascade Research Tool"
CACHE_SCHEMA_VERSION = 1

# Zenodo record/file currently used by the existing cascade_search workflow.
RESNET_RECORD_ID = "15644268"
RESNET_FILENAME = "ResNet.csv"
ZENODO_RECORD_API = f"https://zenodo.org/api/records/{RESNET_RECORD_ID}"

# The Continental United States bounding box:
# (minimum longitude, minimum latitude, maximum longitude, maximum latitude).
CONUS_BBOX = (-125.0, 25.0, -65.0, 50.0)

# Used for unit conversion in calculating the power capacity of a dam from its storage volume and head height.
POWER_CONVERSION_FACTOR = 11800

# A defensive size ceiling for ResNet. The current file should be far below
# this ceiling; this blocks accidental or malicious unexpectedly-large files.
MAX_RESNET_DOWNLOAD_BYTES = 1_000_000_000

# GeoConnex data will be persisted as GeoJSON rather than pickle. GeoJSON is
# an interoperable, inspectable cache format and does not execute code when
# read. The NHD graph remains pickle-based for practical NetworkX fidelity.
GEOCONNEX_CACHE_NAME = "geoconnex_conus_dams.geojson"
NHD_GRAPH_CACHE_NAME = "nhd_enhd_network.pkl"
CACHE_METADATA_NAME = "cache_metadata.json"
RESNET_CACHE_NAME = RESNET_FILENAME

# ---------------------------------------------------------------------------
# Data structures and exceptions
# ---------------------------------------------------------------------------

class CacheSecurityError(RuntimeError):
    """Raised when a cache file has insecure permissions or is unsafe to load."""


class DataValidationError(RuntimeError):
    """Raised when an upstream data source lacks fields required by the app."""

@dataclass
class MappingStatistics:
    """Summary values shown to the researcher after initialization."""

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
    In-memory state produced by initialization.

    Later screens (dam exploration, filtering, downstream analysis, and
    cascade construction) should receive this object through an application
    state/controller object rather than rereading every data source.
    """

    graph: Any
    node_to_comid: dict[Any, int]
    comid_to_node: dict[int, Any]
    dam_inventory: pd.DataFrame
    dam_inventory_matched: pd.DataFrame
    damid_to_comid: dict[str, int]
    damid_to_node: dict[str, Any]
    statistics: MappingStatistics

# ---------------------------------------------------------------------------
# Cache and safe-download utilities
# ---------------------------------------------------------------------------

def get_application_cache_directory() -> Path:
    """
    Return a per-user application cache directory.

    Respect common OS conventions while retaining a safe fallback. The
    directory is made private where the operating system supports POSIX modes.
    """

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))

    cache_dir = base / "cascade_research_tool"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # On POSIX platforms, 0700 means only the current user can access the
    # cache directory. Windows ACLs should instead be controlled by the user
    # profile; chmod has limited semantics there.
    if os.name != "nt":
        os.chmod(cache_dir, 0o700)

    return cache_dir

def ensure_pickle_cache_is_private(cache_file: Path) -> None:
    """
    Reject pickle caches writable by other users on POSIX systems.

    This is a defense-in-depth control only. Pickle remains unsafe if an
    attacker can modify the current user's cache directory or account.
    """

    if not cache_file.exists() or os.name == "nt":
        return

    mode = stat.S_IMODE(cache_file.stat().st_mode)
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise CacheSecurityError(
            f"Refusing to load insecure pickle cache: {cache_file}. "
            "Delete it and rerun initialization."
        )

def atomic_write_bytes(destination: Path, data: bytes) -> None:
    """Write bytes atomically, preventing partial cache files after a failure."""

    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with open(temporary, "wb") as output_file:
            output_file.write(data)
            output_file.flush()
            os.fsync(output_file.fileno())

        if os.name != "nt":
            os.chmod(temporary, 0o600)

        os.replace(temporary, destination)
    finally:
        # If a failure occurred before os.replace, remove the temporary file.
        if temporary.exists():
            temporary.unlink(missing_ok=True)

def write_cache_metadata(cache_dir: Path) -> None:
    """Record the cache schema version without storing sensitive information."""

    metadata = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "application": APP_NAME,
        "created_or_updated_unix_time": time.time(),
    }
    atomic_write_bytes(
        cache_dir / CACHE_METADATA_NAME,
        json.dumps(metadata, indent=2).encode("utf-8"),
    )

def normalize_identifier(value: Any) -> Optional[str]:
    """
    Normalize NID/provider identifiers to stable strings.

    NID IDs may be loaded as strings, numbers, or missing values depending on
    the source and pandas' type inference. This prevents mismatches caused by
    inconsistent representations such as numeric-looking IDs.
    """

    if value is None or pd.isna(value):
        return None

    text = str(value).strip()
    return text if text else None

def parse_comid(value: Any) -> Optional[int]:
    """
    Extract an integer COMID from a plain value or URL-like GeoConnex value.

    Example accepted GeoConnex value:
    https://geoconnex.us/ref/nhdplusv2/comid/12345678
    """

    if value is None or pd.isna(value):
        return None

    text = str(value).strip().rstrip("/")
    if not text:
        return None

    match = re.search(r"(\d+)$", text)
    if not match:
        return None

    try:
        return int(match.group(1))
    except ValueError:
        return None

def get_zenodo_file_metadata(record: dict[str, Any], filename: str) -> dict[str, Any]:
    """Find a named file in a Zenodo record response."""

    for file_info in record.get("files", []):
        if file_info.get("key") == filename:
            return file_info

    raise DataValidationError(
        f"Zenodo record {RESNET_RECORD_ID} does not contain expected file '{filename}'."
    )

def download_resnet_with_validation(
    destination: Path,
    log: Callable[[str], None],
) -> None:
    """
    Download ResNet over HTTPS and validate the publisher-supplied MD5 checksum.

    The checksum is obtained from Zenodo's record metadata rather than being
    hard-coded, allowing the record publisher to publish an updated revision.
    """

    log("Retrieving ResNet file metadata from Zenodo...")
    response = requests.get(ZENODO_RECORD_API, timeout=(10, 60))
    response.raise_for_status()
    record = response.json()

    file_info = get_zenodo_file_metadata(record, RESNET_FILENAME)
    expected_checksum = str(file_info.get("checksum", ""))
    download_url = file_info.get("links", {}).get("self")

    if not download_url or not str(download_url).lower().startswith("https://"):
        raise DataValidationError("Zenodo did not provide a valid HTTPS download URL.")

    log("Downloading ResNet crosswalk...")
    temporary = destination.with_suffix(destination.suffix + ".download")
    downloaded_bytes = 0
    digest = hashlib.md5()  # Zenodo currently publishes this checksum type.

    try:
        with requests.get(download_url, stream=True, timeout=(10, 120)) as download:
            download.raise_for_status()

            with open(temporary, "wb") as output_file:
                for chunk in download.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue

                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > MAX_RESNET_DOWNLOAD_BYTES:
                        raise DataValidationError(
                            "ResNet download exceeded the configured safety limit."
                        )

                    digest.update(chunk)
                    output_file.write(chunk)

                output_file.flush()
                os.fsync(output_file.fileno())

        # Zenodo commonly expresses the checksum as "md5:<hex-digest>".
        if expected_checksum.lower().startswith("md5:"):
            expected_md5 = expected_checksum.split(":", 1)[1].lower()
            actual_md5 = digest.hexdigest().lower()

            if actual_md5 != expected_md5:
                raise DataValidationError(
                    "ResNet checksum verification failed. The download was discarded."
                )
        else:
            log("Warning: Zenodo did not provide an MD5 checksum; file was not checksum-verified.")

        if os.name != "nt":
            os.chmod(temporary, 0o600)

        os.replace(temporary, destination)
        log(f"ResNet downloaded and validated ({downloaded_bytes:,} bytes).")

    finally:
        temporary.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Part 4: Downstream dam-link search constants and helpers
# ---------------------------------------------------------------------------

# These retain the conventions from the original cascade_search algorithm.
LENGTH_ATTR = "lengthkm"
KM_TO_MILES = 0.621371
DEFAULT_MAX_DISTANCE_MILES = 100.0
DOWNSTREAM_LINKS_CSV_NAME = "downstream_dam_pairs.csv"


def get_edge_length_km(
    graph: Any,
    upstream_node: Any,
    downstream_node: Any,
    length_attr: str = LENGTH_ATTR,
) -> float:
    """
    Retrieve a nonnegative downstream edge length in kilometers.

    The enhanced NHD graph is expected to be a DiGraph. The MultiDiGraph
    branch is included defensively in case a future network source represents
    parallel flowline edges between the same graph nodes.

    Missing, nonnumeric, negative, NaN, and infinite lengths are treated as
    zero, matching the original script's tolerant handling of missing edge
    data while preventing invalid distances from entering the search.
    """

    edge_data = graph.get_edge_data(upstream_node, downstream_node, default={})

    # NetworkX MultiDiGraph structures are generally:
    # {edge_key: {attribute_name: value, ...}, ...}
    if graph.is_multigraph():
        candidate_lengths = []

        for attributes in edge_data.values():
            raw_length = attributes.get(length_attr, 0)
            try:
                length = float(raw_length or 0)
            except (TypeError, ValueError):
                length = 0.0

            if math.isfinite(length) and length >= 0:
                candidate_lengths.append(length)

        return min(candidate_lengths) if candidate_lengths else 0.0

    raw_length = edge_data.get(length_attr, 0)

    try:
        length = float(raw_length or 0)
    except (TypeError, ValueError):
        length = 0.0

    return length if math.isfinite(length) and length >= 0 else 0.0

def find_downstream_dam(
    graph: Any,
    start_node: Any,
    dam_nodes: set[Any],
    length_attr: str = LENGTH_ATTR,
    max_distance: float = DEFAULT_MAX_DISTANCE_MILES,
) -> tuple[Optional[Any], Optional[float]]:
    """
    Find the nearest downstream node containing a dam within max_distance.

    This preserves the intent of the original iterative DFS function:
    begin immediately downstream of start_node and stop at the first dam node
    encountered within the configured river-mile limit.

    The current enhanced NHD graph normally has one downstream successor per
    node, so this behaves like a simple downstream walk. A priority queue is
    used instead of assuming that property, which makes the search safe if a
    graph contains multiple successors: the reachable dam with the shortest
    cumulative river distance is selected.

    Returns:
        tuple[Optional[Any], Optional[float]]:
            - downstream NHD node ID and river distance in miles; or
            - (None, None) if no downstream dam is found within the limit.
    """

    if start_node not in graph:
        return None, None

    # Prevent invalid configuration from causing unbounded or nonsensical
    # traversal. UI validation should catch this first, but service functions
    # also validate their own security and correctness boundaries.
    if not math.isfinite(max_distance) or max_distance < 0:
        raise ValueError("Maximum downstream distance must be a nonnegative finite number.")

    # Heap items include a monotonic sequence number so Python never needs to
    # compare potentially heterogeneous NHD node identifiers when two paths
    # have identical distance values.
    sequence = itertools.count()
    search_queue: list[tuple[float, int, Any]] = [(0.0, next(sequence), start_node)]
    best_distance_by_node: dict[Any, float] = {start_node: 0.0}

    while search_queue:
        distance_so_far, _, current_node = heapq.heappop(search_queue)

        # Ignore stale queue entries after a shorter route to the same node
        # has already been discovered.
        if distance_so_far > best_distance_by_node.get(current_node, float("inf")):
            continue

        for downstream_node in graph.successors(current_node):
            segment_km = get_edge_length_km(
                graph,
                current_node,
                downstream_node,
                length_attr,
            )
            downstream_distance = distance_so_far + (segment_km * KM_TO_MILES)

            if downstream_distance > max_distance:
                continue

            # The start node itself is intentionally not checked. A dam's
            # "downstream dam" must be another dam location, consistent with
            # the original cascade_search algorithm.
            if downstream_node in dam_nodes:
                return downstream_node, downstream_distance

            previous_best = best_distance_by_node.get(downstream_node)

            if previous_best is None or downstream_distance < previous_best:
                best_distance_by_node[downstream_node] = downstream_distance

                heapq.heappush(
                    search_queue,
                    (
                        downstream_distance,
                        next(sequence),
                        downstream_node,
                    ),
                )

    return None, None

def sanitize_csv_cell(value: Any) -> Any:
    """
    Prevent spreadsheet formula injection in exported CSV values.

    CSV files are often opened in Excel or similar spreadsheet tools. If an
    externally sourced text value begins with =, +, -, or @, those tools can
    interpret it as a formula. Prefixing a single apostrophe forces text
    interpretation while preserving the displayed value for researchers.
    """

    if value is None or pd.isna(value):
        return None

    if isinstance(value, str):
        trimmed_value = value.lstrip()

        if trimmed_value.startswith(("=", "+", "-", "@")):
            return "'" + value

    return value

def write_downstream_links_csv(
    dataframe: pd.DataFrame,
    output_file: Path,
) -> None:
    """
    Safely write downstream-link results using atomic replacement.

    Atomic replacement prevents a partial CSV from being treated as a valid
    reference dataset if the application is closed or an error occurs during
    export.
    """

    temporary_file = output_file.with_suffix(output_file.suffix + ".tmp")

    # Sanitize a copy so analysis data held in application memory remains
    # unmodified while the CSV remains safe for spreadsheet opening.
    export_dataframe = dataframe.copy()

    for column_name in export_dataframe.columns:
        export_dataframe[column_name] = export_dataframe[column_name].map(
            sanitize_csv_cell
        )

    try:
        export_dataframe.to_csv(
            temporary_file,
            index=False,
            encoding="utf-8",
        )

        if os.name != "nt":
            os.chmod(temporary_file, 0o600)

        os.replace(temporary_file, output_file)

    finally:
        temporary_file.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Part 5: Cascade-system construction constants
# ---------------------------------------------------------------------------

CASCADE_SYSTEMS_CSV_NAME = "cascading_systems.csv"
CASCADE_SYSTEMS_SUMMARY_CSV_NAME = "cascading_systems_summary.csv"

DEFAULT_MIN_HYDROELECTRIC_DAMS_PER_CASCADE = 2
DEFAULT_REQUIRE_SAME_OWNER = False

# ---------------------------------------------------------------------------
# Part 6: Cascade inspection and map-output constants
# ---------------------------------------------------------------------------

CASCADE_MAPS_DIRECTORY_NAME = "cascade_maps"

def bearing_degrees(
    latitude_1: float,
    longitude_1: float,
    latitude_2: float,
    longitude_2: float,
) -> float:
    """
    Calculate the compass bearing from the upstream point to the downstream point.

    Returns a bearing in degrees where:
    - 0 degrees points north;
    - 90 degrees points east;
    - 180 degrees points south;
    - 270 degrees points west.

    The Part 6 map rotates an upward-pointing triangle by this value, making
    the arrow point from the upstream dam toward its direct downstream dam.
    """

    latitude_1_radians = math.radians(latitude_1)
    latitude_2_radians = math.radians(latitude_2)
    longitude_delta_radians = math.radians(longitude_2 - longitude_1)

    x_value = math.sin(longitude_delta_radians) * math.cos(
        latitude_2_radians
    )
    y_value = (
        math.cos(latitude_1_radians) * math.sin(latitude_2_radians)
        - math.sin(latitude_1_radians)
        * math.cos(latitude_2_radians)
        * math.cos(longitude_delta_radians)
    )

    return (
        math.degrees(math.atan2(x_value, y_value)) + 360
    ) % 360

# Part 6 overview-map output name. The file is overwritten safely whenever a
# researcher generates a new map for a different Part 6 query result.
CONUS_CASCADE_MAP_FILENAME = "conus_cascade_query_map.html"

# Each cascading system receives a separate line color. The flow direction is
# indicated independently by the midpoint arrow, so color distinguishes systems
# rather than direction.
CASCADE_LINE_COLORS = [
    "blue",
    "darkred",
    "darkgreen",
    "purple",
    "orange",
    "darkblue",
    "cadetblue",
    "deeppink",
    "black",
    "darkorange",
]

# ---------------------------------------------------------------------------
# Initialization service: datasets, cache, and COMID/node matching
# ---------------------------------------------------------------------------

class InitializationService:
    """Owns initial dataset acquisition, caching, validation, and COMID mapping."""

    def __init__(self, cache_dir: Path, log: Callable[[str], None]) -> None:
        self.cache_dir = cache_dir
        self.log = log

        self.nhd_cache = self.cache_dir / NHD_GRAPH_CACHE_NAME
        self.geoconnex_cache = self.cache_dir / GEOCONNEX_CACHE_NAME
        self.resnet_cache = self.cache_dir / RESNET_CACHE_NAME

    def load_or_download_nhd_network(self) -> tuple[Any, dict[Any, int]]:
        """
        Load the enhanced NHD graph from local cache or obtain it through pynhd.

        Important: only application-created, local pickle files may be loaded.
        Never copy an NHD pickle cache from an untrusted location.
        """

        if self.nhd_cache.exists():
            ensure_pickle_cache_is_private(self.nhd_cache)
            self.log("Loading cached enhanced NHD river network...")
            with open(self.nhd_cache, "rb") as input_file:
                graph, node_to_comid = pickle.load(input_file)
        else:
            self.log("Downloading/building enhanced NHD river network; this may take several minutes...")
            graph, node_to_comid, _ = pynhd.enhd_flowlines_nx()

            # Serialize only data created by the trusted pynhd process in this
            # application session. Atomic replacement prevents partial caches.
            payload = pickle.dumps((graph, node_to_comid), protocol=pickle.HIGHEST_PROTOCOL)
            atomic_write_bytes(self.nhd_cache, payload)
            self.log(f"Saved NHD network cache: {self.nhd_cache}")

        if not hasattr(graph, "number_of_nodes") or not isinstance(node_to_comid, dict):
            raise DataValidationError("Cached/downloaded NHD network has an unexpected structure.")

        normalized_node_to_comid: dict[Any, int] = {}
        for node_id, comid_value in node_to_comid.items():
            comid = parse_comid(comid_value)
            if comid is not None:
                normalized_node_to_comid[node_id] = comid

        if not normalized_node_to_comid:
            raise DataValidationError("No usable COMIDs were found in the NHD node mapping.")

        self.log(
            f"NHD network ready: {graph.number_of_nodes():,} nodes; "
            f"{graph.number_of_edges():,} edges."
        )
        return graph, normalized_node_to_comid

    def load_or_download_geoconnex_dams(self) -> gpd.GeoDataFrame:
        """Load cached GeoConnex dams or download CONUS records and cache GeoJSON."""

        if self.geoconnex_cache.exists():
            self.log("Loading cached GeoConnex CONUS dam records...")
            geo_dams = gpd.read_file(self.geoconnex_cache)
        else:
            self.log("Retrieving GeoConnex CONUS dam records...")
            geo_dams = GeoConnex("dams").bybox(CONUS_BBOX)

            required_columns = {"provider_id", "nhdpv2_comid"}
            missing_columns = required_columns - set(geo_dams.columns)
            if missing_columns:
                raise DataValidationError(
                    f"GeoConnex response is missing required columns: {sorted(missing_columns)}"
                )

            # Write to a temporary file then replace to avoid accepting an
            # incomplete GeoJSON cache if the process is interrupted.
            temporary = self.geoconnex_cache.with_suffix(".geojson.tmp")
            geo_dams.to_file(temporary, driver="GeoJSON")

            if os.name != "nt":
                os.chmod(temporary, 0o600)

            os.replace(temporary, self.geoconnex_cache)
            self.log(f"Saved GeoConnex cache: {self.geoconnex_cache}")

        required_columns = {"provider_id", "nhdpv2_comid"}
        missing_columns = required_columns - set(geo_dams.columns)
        if missing_columns:
            raise DataValidationError(
                f"Cached GeoConnex data is missing required columns: {sorted(missing_columns)}"
            )

        self.log(f"GeoConnex records ready: {len(geo_dams):,} dams.")
        return geo_dams

    def load_or_download_resnet(self) -> pd.DataFrame:
        """Load a validated cached ResNet CSV or download and validate it."""

        if not self.resnet_cache.exists():
            download_resnet_with_validation(self.resnet_cache, self.log)
        else:
            self.log("Loading cached ResNet crosswalk...")

        # Use string types first. COMIDs are parsed explicitly afterward,
        # avoiding accidental loss of IDs because of pandas numeric inference.
        resnet_df = pd.read_csv(self.resnet_cache, dtype=str)

        required_columns = {"NID", "COMID"}
        missing_columns = required_columns - set(resnet_df.columns)
        if missing_columns:
            raise DataValidationError(
                f"ResNet file is missing required columns: {sorted(missing_columns)}"
            )

        self.log(f"ResNet records ready: {len(resnet_df):,} rows.")
        return resnet_df

    def load_nid_inventory(self) -> pd.DataFrame:
        """
        Acquire NID through pygeohydro and apply only the basic current filters.

        pygeohydro manages its own NID download/cache. Subsequent application
        stages will expose additional filtering through the user interface.
        """

        self.log("Loading National Inventory of Dams through pygeohydro...")
        dam_inventory = gh.NID().df.copy()

        required_columns = {
            "NID ID",
            "Federal ID",
            "Max Storage (Acre-Ft)",
            "Longitude",
            "Latitude",
        }
        missing_columns = required_columns - set(dam_inventory.columns)
        if missing_columns:
            raise DataValidationError(
                f"NID data is missing required columns: {sorted(missing_columns)}"
            )

        # Normalize identifiers before comparing NID ID and Federal ID. This
        # preserves the original workflow's primary-structure preference.
        dam_inventory["NID ID"] = dam_inventory["NID ID"].map(normalize_identifier)
        dam_inventory["Federal ID"] = dam_inventory["Federal ID"].map(normalize_identifier)

        dam_inventory["Max Storage (Acre-Ft)"] = pd.to_numeric(
            dam_inventory["Max Storage (Acre-Ft)"],
            errors="coerce",
        )

        dam_inventory = dam_inventory.dropna(
            subset=["NID ID", "Max Storage (Acre-Ft)", "Longitude", "Latitude"]
        )
        dam_inventory = dam_inventory[
            dam_inventory["NID ID"] == dam_inventory["Federal ID"]
        ]

        self.log(f"NID inventory ready after basic filters: {len(dam_inventory):,} dams.")
        return dam_inventory

    @staticmethod
    def build_geoconnex_damid_to_comid(geo_dams: gpd.GeoDataFrame) -> dict[str, int]:
        """Create normalized NID/provider ID -> COMID mappings from GeoConnex."""

        mapping: dict[str, int] = {}
        for _, dam in geo_dams.iterrows():
            dam_id = normalize_identifier(dam.get("provider_id"))
            comid = parse_comid(dam.get("nhdpv2_comid"))

            if dam_id is not None and comid is not None:
                mapping[dam_id] = comid

        return mapping

    @staticmethod
    def build_resnet_damid_to_comid(resnet_df: pd.DataFrame) -> dict[str, int]:
        """Create normalized NID ID -> COMID mappings from the ResNet crosswalk."""

        mapping: dict[str, int] = {}
        for _, row in resnet_df.iterrows():
            dam_id = normalize_identifier(row.get("NID"))
            comid = parse_comid(row.get("COMID"))

            if dam_id is not None and comid is not None:
                mapping[dam_id] = comid

        return mapping

    def initialize(self) -> InitializationResult:
        """Run the full data initialization and matching pipeline."""

        graph, node_to_comid = self.load_or_download_nhd_network()
        geo_dams = self.load_or_download_geoconnex_dams()
        resnet_df = self.load_or_download_resnet()
        dam_inventory = self.load_nid_inventory()

        # A COMID normally identifies exactly one NHD reach/node in this
        # mapping. setdefault retains the first item if source data includes a
        # duplicate COMID, providing deterministic behavior.
        comid_to_node: dict[int, Any] = {}
        for node_id, comid in node_to_comid.items():
            comid_to_node.setdefault(comid, node_id)

        geoconnex_map = self.build_geoconnex_damid_to_comid(geo_dams)
        resnet_map = self.build_resnet_damid_to_comid(resnet_df)

        self.log("Mapping NID dams to NHD nodes using GeoConnex COMIDs...")
        dam_inventory["geo_node_id"] = dam_inventory["NID ID"].map(
            lambda dam_id: comid_to_node.get(geoconnex_map.get(dam_id))
        )

        geo_matches = int(dam_inventory["geo_node_id"].notna().sum())

        self.log("Applying ResNet COMID mapping to remaining unmatched dams...")
        dam_inventory["resnet_node_id"] = dam_inventory["NID ID"].map(
            lambda dam_id: comid_to_node.get(resnet_map.get(dam_id))
        )

        # GeoConnex is the preferred mapping. ResNet fills only missing values.
        dam_inventory["node_id"] = dam_inventory["geo_node_id"].combine_first(
            dam_inventory["resnet_node_id"]
        )

        total_matches = int(dam_inventory["node_id"].notna().sum())
        resnet_recovered = total_matches - geo_matches
        unmatched = len(dam_inventory) - total_matches
        match_rate = (100.0 * total_matches / len(dam_inventory)) if len(dam_inventory) else 0.0

        dam_inventory_matched = dam_inventory.dropna(subset=["node_id"]).copy()

        # node IDs originate from the graph and may not always be integers in
        # future NHD releases, so retain the graph's native node-ID type.
        damid_to_node = dict(
            zip(dam_inventory_matched["NID ID"], dam_inventory_matched["node_id"])
        )

        combined_damid_to_comid = dict(resnet_map)
        combined_damid_to_comid.update(geoconnex_map)  # GeoConnex remains preferred.

        statistics = MappingStatistics(
            total_nid_dams_after_basic_filter=len(dam_inventory),
            geo_connex_comid_mappings=len(geoconnex_map),
            resnet_comid_mappings=len(resnet_map),
            dams_matched_by_geoconnex=geo_matches,
            dams_recovered_by_resnet=resnet_recovered,
            total_dams_matched_to_network=total_matches,
            unmatched_dams=unmatched,
            match_rate_percent=match_rate,
            graph_nodes=graph.number_of_nodes(),
            graph_edges=graph.number_of_edges(),
        )

        write_cache_metadata(self.cache_dir)
        self.log("Initialization and COMID-to-node mapping completed successfully.")

        return InitializationResult(
            graph=graph,
            node_to_comid=node_to_comid,
            comid_to_node=comid_to_node,
            dam_inventory=dam_inventory,
            dam_inventory_matched=dam_inventory_matched,
            damid_to_comid=combined_damid_to_comid,
            damid_to_node=damid_to_node,
            statistics=statistics,
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
        self.initialization_result: Optional[InitializationResult] = None

        # Part 3 creates researcher-selected subsets from the initialized
        # baseline inventory. Later stages, such as downstream searching and
        # cascade construction, can use these datasets rather than repeating
        # the filtering operation.
        self.filtered_dam_inventory: Optional[pd.DataFrame] = None
        self.filtered_dam_inventory_matched: Optional[pd.DataFrame] = None

        # Part 4 stores the full matched primary-dam downstream-link reference
        # table. Parts 5 and 6 will later use this table to construct and
        # inspect cascade systems.
        self.downstream_links: Optional[pd.DataFrame] = None
        self.downstream_links_csv_path: Optional[Path] = None

        # The downstream search has its own worker thread because it can
        # process thousands of matched dams and must not block Tkinter.
        self.downstream_search_thread: Optional[threading.Thread] = None

        # Part 5 output. Each key is a stable cascade system ID and each value
        # is a NetworkX DiGraph representing one potentially branching system.
        self.cascade_graphs: dict[str, nx.DiGraph] = {}
        self.cascade_systems_edges: Optional[pd.DataFrame] = None
        self.cascade_systems_summary: Optional[pd.DataFrame] = None
        self.cascade_systems_csv_path: Optional[Path] = None
        self.cascade_systems_summary_csv_path: Optional[Path] = None

        # Cascade construction can involve many chains, graph merges, and CSV
        # exports. Run it separately from the Tkinter event thread.
        self.cascade_construction_thread: Optional[threading.Thread] = None

        # Part 6 stores generated HTML maps in the private user-local
        # application cache directory. Each selected system receives its own
        # independently referenceable interactive HTML map file.
        self.cascade_maps_directory = (
            self.cache_dir / CASCADE_MAPS_DIRECTORY_NAME
        )

        # The latest Part 6 query result is retained so the researcher can
        # generate a national overview map from exactly the systems displayed
        # in the query-results table. An empty list means that no successful
        # query has run yet.
        self.last_cascade_query_system_ids: list[str] = []

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
        self.initialization_tab = ttk.Frame(self.notebook, padding=12)
        self.node_explorer_tab = ttk.Frame(self.notebook, padding=12)
        self.filter_tab = ttk.Frame(self.notebook, padding=12)
        self.downstream_tab = ttk.Frame(self.notebook, padding=12)
        self.cascade_builder_tab = ttk.Frame(self.notebook, padding=12)
        self.cascade_query_tab = ttk.Frame(self.notebook, padding=12)

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

        # Build the implemented tabs.
        self._build_initialization_tab()
        self._build_node_explorer_tab()
        self._build_filter_tab()
        self._build_downstream_search_tab()
        self._build_cascade_builder_tab()
        self._build_cascade_query_tab()

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

    def _build_initialization_tab(self) -> None:
        """
        Build the initialization controls and mapping
        statistics table into the first notebook tab.
        """

        ttk.Label(
            self.initialization_tab,
            text="Part 1: Data Initialization and COMID Mapping",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.initialization_tab,
            text=(
                "Download or load cached NHD, GeoConnex, ResNet, and NID data. "
                "Then map NID dam records to nodes in the NHD river network."
            ),
            wraplength=950,
        ).pack(anchor=tk.W, pady=(4, 12))

        cache_frame = ttk.LabelFrame(
            self.initialization_tab,
            text="Local Cache",
            padding=10,
        )
        cache_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(cache_frame, text="Cache directory:").grid(
            row=0,
            column=0,
            sticky=tk.NW,
            padx=(0, 8),
        )

        ttk.Label(
            cache_frame,
            textvariable=self.cache_path_text,
            wraplength=780,
        ).grid(
            row=0,
            column=1,
            sticky=tk.W,
        )

        controls = ttk.Frame(self.initialization_tab)
        controls.pack(fill=tk.X, pady=(0, 10))

        self.initialize_button = ttk.Button(
            controls,
            text="Initialize / Refresh Required Data",
            command=self.start_initialization,
        )
        self.initialize_button.pack(side=tk.LEFT)

        ttk.Label(
            controls,
            textvariable=self.status_text,
            foreground="#1f4e79",
        ).pack(side=tk.LEFT, padx=(16, 0))

        progress_frame = ttk.LabelFrame(
            self.initialization_tab,
            text="Current Activity",
            padding=10,
        )
        progress_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(
            progress_frame,
            textvariable=self.progress_text,
            wraplength=950,
        ).pack(anchor=tk.W)

        stats_frame = ttk.LabelFrame(
            self.initialization_tab,
            text="NHD / COMID Mapping Statistics",
            padding=10,
        )
        stats_frame.pack(fill=tk.BOTH, expand=True)

        self.stats_tree = ttk.Treeview(
            stats_frame,
            columns=("metric", "value"),
            show="headings",
            height=10,
        )
        self.stats_tree.heading("metric", text="Metric")
        self.stats_tree.heading("value", text="Value")
        self.stats_tree.column("metric", width=550, anchor=tk.W)
        self.stats_tree.column("value", width=250, anchor=tk.E)
        self.stats_tree.pack(fill=tk.BOTH, expand=True)


    def _build_node_explorer_tab(self) -> None:
        """
        Build Part 2: an NHD node lookup interface.

        A user provides a river-network node ID. The application searches the
        initialized matched NID inventory for dams assigned to that node and
        writes the same formatted information as the original
        explore_node_dams() function into the shared application log.
        """

        ttk.Label(
            self.node_explorer_tab,
            text="Part 2: Explore Dams at an NHD Network Node",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.node_explorer_tab,
            text=(
                "Search by NHD network node ID to list matched dams at that "
                "location, or search by NID ID to identify a dam's mapped "
                "network node and purposes. Results are written to the "
                "shared Application Log below."
            ),
            wraplength=950,
        ).pack(anchor=tk.W, pady=(4, 16))

        lookup_frame = ttk.LabelFrame(
            self.node_explorer_tab,
            text="Node Lookup",
            padding=14,
        )
        lookup_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(
            lookup_frame,
            text="NHD Node ID:",
        ).grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 8),
        )

        # Keep the node ID as text until validation occurs. This avoids
        # Tkinter silently coercing values and allows a useful user message
        # when a non-integer value is entered.
        self.node_id_text = tk.StringVar()

        self.node_id_entry = ttk.Entry(
            lookup_frame,
            textvariable=self.node_id_text,
            width=30,
        )
        self.node_id_entry.grid(
            row=0,
            column=1,
            sticky=tk.W,
        )

        self.explore_node_button = ttk.Button(
            lookup_frame,
            text="Explore Node",
            command=self.explore_selected_node,
        )
        self.explore_node_button.grid(
            row=0,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
        )

        # Support keyboard-first research workflows: pressing Enter while the
        # entry has focus executes the same lookup as clicking the button.
        self.node_id_entry.bind(
            "<Return>",
            lambda _event: self.explore_selected_node(),
        )

        # Add a second lookup option for researchers who know a dam's NID ID
        # but do not yet know which NHD network node it maps to.
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

        # NID IDs must remain strings. Many IDs contain a state prefix or
        # leading zeros, either of which would be corrupted by numeric casting.
        self.nid_id_text = tk.StringVar()

        self.nid_id_entry = ttk.Entry(
            lookup_frame,
            textvariable=self.nid_id_text,
            width=30,
        )
        self.nid_id_entry.grid(
            row=1,
            column=1,
            sticky=tk.W,
            pady=(12, 0),
        )

        self.explore_nid_button = ttk.Button(
            lookup_frame,
            text="Find Dam by NID ID",
            command=self.explore_selected_nid,
        )
        self.explore_nid_button.grid(
            row=1,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
            pady=(12, 0),
        )

        # Permit Enter to run the NID lookup when the NID field has focus.
        self.nid_id_entry.bind(
            "<Return>",
            lambda _event: self.explore_selected_nid(),
        )

        lookup_frame.columnconfigure(3, weight=1)

        guidance_frame = ttk.LabelFrame(
            self.node_explorer_tab,
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

    def _build_filter_tab(self) -> None:
        """
        Build Part 3: researcher-controlled dam filtering.

        The primary-dam rule from the original algorithm is always applied
        during initialization:

            NID ID == Federal ID

        This tab exposes the remaining requested filter controls:
        - minimum maximum storage;
        - optional hydroelectric-purpose filtering;
        - optional lower power-capacity threshold for hydroelectric dams.
        """

        ttk.Label(
            self.filter_tab,
            text="Part 3: Filter Dam Inventory",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.filter_tab,
            text=(
                "The inventory already contains only primary dam records, "
                "where NID ID equals Federal ID. Set a lower storage limit, "
                "optionally retain dams whose purposes include Hydroelectric, "
                "and optionally apply the original estimated power-capacity "
                "threshold to that hydroelectric subset."
            ),
            wraplength=950,
        ).pack(anchor=tk.W, pady=(4, 14))

        baseline_frame = ttk.LabelFrame(
            self.filter_tab,
            text="Automatic Baseline Rule",
            padding=10,
        )
        baseline_frame.pack(fill=tk.X, pady=(0, 10))

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
            self.filter_tab,
            text="Researcher-Selected Filters",
            padding=12,
        )
        criteria_frame.pack(fill=tk.X, pady=(0, 10))

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

        # Default to the original script's storage threshold, while allowing
        # the researcher to clear or replace it.
        self.minimum_storage_text = tk.StringVar(value="100")

        self.minimum_storage_entry = ttk.Entry(
            criteria_frame,
            textvariable=self.minimum_storage_text,
            width=18,
        )
        self.minimum_storage_entry.grid(
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

        # This checkbox enables both the hydroelectric purpose filter and the
        # associated power-capacity threshold controls.
        self.hydroelectric_only_var = tk.BooleanVar(value=True)

        self.hydroelectric_checkbox = ttk.Checkbutton(
            criteria_frame,
            text="Filter for hydroelectric dams",
            variable=self.hydroelectric_only_var,
            command=self._update_power_filter_state,
        )
        self.hydroelectric_checkbox.grid(
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

        # The original code used 10 as the default power threshold.
        self.power_threshold_text = tk.StringVar(value="10")

        self.power_threshold_entry = ttk.Entry(
            criteria_frame,
            textvariable=self.power_threshold_text,
            width=18,
        )
        self.power_threshold_entry.grid(
            row=2,
            column=1,
            sticky=tk.W,
        )

        self.power_threshold_units_label = ttk.Label(
            criteria_frame,
            text=(
                "Applies: (Hydraulic Height × Max Discharge) / "
                "POWER_CONVERSION_FACTOR > threshold."
            ),
        )
        self.power_threshold_units_label.grid(
            row=2,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
        )

        criteria_frame.columnconfigure(2, weight=1)

        action_frame = ttk.Frame(self.filter_tab)
        action_frame.pack(fill=tk.X, pady=(4, 10))

        self.apply_filters_button = ttk.Button(
            action_frame,
            text="Apply Filters",
            command=self.apply_dam_filters,
        )
        self.apply_filters_button.pack(side=tk.LEFT)

        self.reset_filters_button = ttk.Button(
            action_frame,
            text="Restore Original Defaults",
            command=self.restore_default_filters,
        )
        self.reset_filters_button.pack(side=tk.LEFT, padx=(10, 0))

        self.filter_status_text = tk.StringVar(
            value="Configure Initial Filters."
        )

        ttk.Label(
            action_frame,
            textvariable=self.filter_status_text,
            foreground="#1f4e79",
        ).pack(side=tk.LEFT, padx=(16, 0))

        results_frame = ttk.LabelFrame(
            self.filter_tab,
            text="Current Filter Results",
            padding=10,
        )
        results_frame.pack(fill=tk.BOTH, expand=True)

        self.filter_results_tree = ttk.Treeview(
            results_frame,
            columns=("metric", "value"),
            show="headings",
            height=9,
        )
        self.filter_results_tree.heading("metric", text="Metric")
        self.filter_results_tree.heading("value", text="Value")
        self.filter_results_tree.column("metric", width=600, anchor=tk.W)
        self.filter_results_tree.column("value", width=250, anchor=tk.E)
        self.filter_results_tree.pack(fill=tk.BOTH, expand=True)

        # The default checkbox state is hydroelectric-only, so leave the power
        # threshold entry enabled when the tab is initially constructed.
        self._update_power_filter_state()

    def _build_downstream_search_tab(self) -> None:
        """
        Build Part 4: national downstream dam-link reference generation.

        This stage intentionally searches every initialized dam that has an
        NHD node ID, rather than only the Part 3 filtered dams. Doing so
        creates a reusable reference table from which later cascade criteria
        can be applied without rerunning all downstream graph searches.
        """

        ttk.Label(
            self.downstream_tab,
            text="Part 4: Search for Downstream Dams",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.downstream_tab,
            text=(
                "Search the enhanced NHD river network downstream from every "
                "matched primary dam. The first reachable dam within the "
                "selected river-distance limit is recorded as that dam's "
                "direct downstream link. Results are saved as a reusable CSV "
                "reference dataset in the application cache."
            ),
            wraplength=950,
        ).pack(anchor=tk.W, pady=(4, 14))

        search_settings_frame = ttk.LabelFrame(
            self.downstream_tab,
            text="Search Settings",
            padding=12,
        )
        search_settings_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(
            search_settings_frame,
            text="Maximum downstream search distance (miles):",
        ).grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
        )

        # Retain the original algorithm's 100-mile search distance as the default.
        self.maximum_downstream_distance_text = tk.StringVar(
            value=str(DEFAULT_MAX_DISTANCE_MILES)
        )

        self.maximum_downstream_distance_entry = ttk.Entry(
            search_settings_frame,
            textvariable=self.maximum_downstream_distance_text,
            width=15,
        )
        self.maximum_downstream_distance_entry.grid(
            row=0,
            column=1,
            sticky=tk.W,
        )

        ttk.Label(
            search_settings_frame,
            text=(
                "Only downstream dam nodes reached within this limit are recorded."
            ),
            wraplength=520,
        ).grid(
            row=0,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
        )

        search_settings_frame.columnconfigure(2, weight=1)

        action_frame = ttk.Frame(self.downstream_tab)
        action_frame.pack(fill=tk.X, pady=(0, 10))

        self.run_downstream_search_button = ttk.Button(
            action_frame,
            text="Build Downstream Dam Reference CSV",
            command=self.start_downstream_search,
        )
        self.run_downstream_search_button.pack(side=tk.LEFT)

        self.downstream_search_status_text = tk.StringVar(
            value="Click to start downstream search."
        )

        ttk.Label(
            action_frame,
            textvariable=self.downstream_search_status_text,
            foreground="#1f4e79",
        ).pack(side=tk.LEFT, padx=(16, 0))

        output_frame = ttk.LabelFrame(
            self.downstream_tab,
            text="Reference Dataset",
            padding=12,
        )
        output_frame.pack(fill=tk.X, pady=(0, 10))

        self.downstream_csv_path_text = tk.StringVar(
            value=(
                "The CSV path will be shown after the downstream search completes."
            )
        )

        ttk.Label(
            output_frame,
            text="Output CSV:",
        ).grid(
            row=0,
            column=0,
            sticky=tk.NW,
            padx=(0, 8),
        )

        ttk.Label(
            output_frame,
            textvariable=self.downstream_csv_path_text,
            wraplength=780,
        ).grid(
            row=0,
            column=1,
            sticky=tk.W,
        )

        results_frame = ttk.LabelFrame(
            self.downstream_tab,
            text="Latest Search Summary",
            padding=10,
        )
        results_frame.pack(fill=tk.BOTH, expand=True)

        self.downstream_results_tree = ttk.Treeview(
            results_frame,
            columns=("metric", "value"),
            show="headings",
            height=9,
        )
        self.downstream_results_tree.heading("metric", text="Metric")
        self.downstream_results_tree.heading("value", text="Value")
        self.downstream_results_tree.column("metric", width=600, anchor=tk.W)
        self.downstream_results_tree.column("value", width=260, anchor=tk.E)
        self.downstream_results_tree.pack(fill=tk.BOTH, expand=True)

    def _build_cascade_builder_tab(self) -> None:
        """
        Build Part 5: downstream-link-based cascade-system construction.

        Part 5 consumes:
        - the researcher-selected/matched candidate dams from Part 3; and
        - the direct downstream dam links created in Part 4.

        It does not rerun NHD river-network traversal. That separation keeps
        downstream relationship generation reusable and cascade criteria
        reproducible.
        """

        ttk.Label(
            self.cascade_builder_tab,
            text="Part 5: Construct Cascading Systems",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.cascade_builder_tab,
            text=(
                "Build directed cascading systems from the Part 4 downstream "
                "dam-pair reference dataset. Part 3 selected and matched dams "
                "are used as cascade root candidates. Each system must contain "
                "at least two dams, regardless of the hydroelectric criterion."
            ),
            wraplength=950,
        ).pack(anchor=tk.W, pady=(4, 14))

        settings_frame = ttk.LabelFrame(
            self.cascade_builder_tab,
            text="Cascade Construction Criteria",
            padding=12,
        )
        settings_frame.pack(fill=tk.X, pady=(0, 10))

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

        self.minimum_hydroelectric_dams_text = tk.StringVar(
            value=str(DEFAULT_MIN_HYDROELECTRIC_DAMS_PER_CASCADE)
        )

        self.minimum_hydroelectric_dams_entry = ttk.Entry(
            settings_frame,
            textvariable=self.minimum_hydroelectric_dams_text,
            width=12,
        )
        self.minimum_hydroelectric_dams_entry.grid(
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

        self.require_same_owner_var = tk.BooleanVar(
            value=DEFAULT_REQUIRE_SAME_OWNER
        )

        self.require_same_owner_checkbox = ttk.Checkbutton(
            settings_frame,
            text=(
                "Require every dam in each cascade chain to have the same owner "
                "as its root dam"
            ),
            variable=self.require_same_owner_var,
        )
        self.require_same_owner_checkbox.grid(
            row=1,
            column=0,
            columnspan=3,
            sticky=tk.W,
        )

        settings_frame.columnconfigure(2, weight=1)

        action_frame = ttk.Frame(self.cascade_builder_tab)
        action_frame.pack(fill=tk.X, pady=(0, 10))

        self.construct_cascades_button = ttk.Button(
            action_frame,
            text="Construct Cascade Systems",
            command=self.start_cascade_construction,
        )
        self.construct_cascades_button.pack(side=tk.LEFT)

        self.restore_cascade_defaults_button = ttk.Button(
            action_frame,
            text="Restore Defaults",
            command=self.restore_default_cascade_settings,
        )
        self.restore_cascade_defaults_button.pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        self.cascade_construction_status_text = tk.StringVar(
            value=(
                "Apply Part 3 filters and build Part 4 downstream links before "
                "constructing cascade systems."
            )
        )

        ttk.Label(
            action_frame,
            textvariable=self.cascade_construction_status_text,
            foreground="#1f4e79",
        ).pack(
            side=tk.LEFT,
            padx=(16, 0),
        )

        output_frame = ttk.LabelFrame(
            self.cascade_builder_tab,
            text="Cascade Reference Datasets",
            padding=12,
        )
        output_frame.pack(fill=tk.X, pady=(0, 10))

        self.cascade_systems_csv_path_text = tk.StringVar(
            value="The cascade edge-list CSV will be shown after construction."
        )

        self.cascade_summary_csv_path_text = tk.StringVar(
            value="The cascade summary CSV will be shown after construction."
        )

        ttk.Label(
            output_frame,
            text="System edge-list CSV:",
        ).grid(
            row=0,
            column=0,
            sticky=tk.NW,
            padx=(0, 8),
            pady=(0, 8),
        )

        ttk.Label(
            output_frame,
            textvariable=self.cascade_systems_csv_path_text,
            wraplength=760,
        ).grid(
            row=0,
            column=1,
            sticky=tk.W,
            pady=(0, 8),
        )

        ttk.Label(
            output_frame,
            text="System summary CSV:",
        ).grid(
            row=1,
            column=0,
            sticky=tk.NW,
            padx=(0, 8),
        )

        ttk.Label(
            output_frame,
            textvariable=self.cascade_summary_csv_path_text,
            wraplength=760,
        ).grid(
            row=1,
            column=1,
            sticky=tk.W,
        )

        summary_frame = ttk.LabelFrame(
            self.cascade_builder_tab,
            text="Latest Construction Summary",
            padding=10,
        )
        summary_frame.pack(fill=tk.BOTH, expand=True)

        self.cascade_results_tree = ttk.Treeview(
            summary_frame,
            columns=("metric", "value"),
            show="headings",
            height=9,
        )
        self.cascade_results_tree.heading("metric", text="Metric")
        self.cascade_results_tree.heading("value", text="Value")
        self.cascade_results_tree.column("metric", width=630, anchor=tk.W)
        self.cascade_results_tree.column("value", width=240, anchor=tk.E)
        self.cascade_results_tree.pack(fill=tk.BOTH, expand=True)

    def _build_cascade_query_tab(self) -> None:
        """
        Build Part 6: cascade-system querying, reporting, and visualization.

        Query criteria are evaluated at the SYSTEM level. A system matches an
        enabled criterion when at least one dam node inside that system matches
        that criterion. When multiple criteria are enabled, all enabled
        criteria must be satisfied.
        """

        ttk.Label(
            self.cascade_query_tab,
            text="Part 6: Query and Visualize Cascade Systems",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            self.cascade_query_tab,
            text=(
                "Filter constructed cascading systems by state, dam NID ID, "
                "and river/stream name. Enable one or more criteria; enabled "
                "criteria are combined using AND logic. Query results are "
                "written to the Application Log and can be inspected or mapped."
            ),
            wraplength=950,
        ).pack(anchor=tk.W, pady=(4, 12))

        # ------------------------------------------------------------------
        # Query filters
        # ------------------------------------------------------------------
        query_frame = ttk.LabelFrame(
            self.cascade_query_tab,
            text="Cascade-System Filters",
            padding=12,
        )
        query_frame.pack(fill=tk.X, pady=(0, 10))

        # State query controls.
        self.query_state_enabled_var = tk.BooleanVar(value=False)
        self.query_state_text = tk.StringVar()

        self.query_state_checkbox = ttk.Checkbutton(
            query_frame,
            text="At least one dam in state:",
            variable=self.query_state_enabled_var,
            command=self._update_cascade_query_control_states,
        )
        self.query_state_checkbox.grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
            pady=(0, 8),
        )

        self.query_state_entry = ttk.Entry(
            query_frame,
            textvariable=self.query_state_text,
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

        # NID ID query controls.
        self.query_nid_enabled_var = tk.BooleanVar(value=False)
        self.query_nid_text = tk.StringVar()

        self.query_nid_checkbox = ttk.Checkbutton(
            query_frame,
            text="System contains dam with NID ID:",
            variable=self.query_nid_enabled_var,
            command=self._update_cascade_query_control_states,
        )
        self.query_nid_checkbox.grid(
            row=1,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
            pady=(0, 8),
        )

        self.query_nid_entry = ttk.Entry(
            query_frame,
            textvariable=self.query_nid_text,
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

        # River/stream query controls.
        self.query_river_enabled_var = tk.BooleanVar(value=False)
        self.query_river_text = tk.StringVar()

        self.query_river_checkbox = ttk.Checkbutton(
            query_frame,
            text="At least one dam on river/stream:",
            variable=self.query_river_enabled_var,
            command=self._update_cascade_query_control_states,
        )
        self.query_river_checkbox.grid(
            row=2,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
        )

        self.query_river_entry = ttk.Entry(
            query_frame,
            textvariable=self.query_river_text,
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

        # Allow keyboard users to start a query by pressing Enter in any
        # active query field.
        self.query_state_entry.bind(
            "<Return>",
            lambda _event: self.run_cascade_system_query(),
        )
        self.query_nid_entry.bind(
            "<Return>",
            lambda _event: self.run_cascade_system_query(),
        )
        self.query_river_entry.bind(
            "<Return>",
            lambda _event: self.run_cascade_system_query(),
        )

        query_actions_frame = ttk.Frame(self.cascade_query_tab)
        query_actions_frame.pack(fill=tk.X, pady=(0, 10))

        self.run_cascade_query_button = ttk.Button(
            query_actions_frame,
            text="Search Cascading Systems",
            command=self.run_cascade_system_query,
        )
        self.run_cascade_query_button.pack(side=tk.LEFT)

        self.clear_cascade_query_button = ttk.Button(
            query_actions_frame,
            text="Clear Filters / Show All Systems",
            command=self.clear_cascade_system_query,
        )
        self.clear_cascade_query_button.pack(side=tk.LEFT, padx=(10, 0))

        # The overview map uses the systems returned by the current Part 6
        # query. With no active query filters, that result is every system.
        self.generate_conus_query_map_button = ttk.Button(
            query_actions_frame,
            text="Generate Map of Query Results",
            command=self.generate_query_results_conus_map,
        )
        self.generate_conus_query_map_button.pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        # Distance labels can clutter a large national map, so they are
        # optional. Tooltips always continue to show each edge distance.
        self.show_overview_distance_labels_var = tk.BooleanVar(value=False)

        self.show_overview_distance_labels_checkbox = ttk.Checkbutton(
            query_actions_frame,
            text="Show distance labels",
            variable=self.show_overview_distance_labels_var,
        )
        self.show_overview_distance_labels_checkbox.pack(
            side=tk.LEFT,
            padx=(10, 0),
        )

        self.cascade_query_status_text = tk.StringVar(
            value=(
                "Construct cascade systems in Part 5 before querying or "
                "visualizing them."
            )
        )

        ttk.Label(
            query_actions_frame,
            textvariable=self.cascade_query_status_text,
            foreground="#1f4e79",
        ).pack(side=tk.LEFT, padx=(16, 0))

        # ------------------------------------------------------------------
        # Query result systems
        # ------------------------------------------------------------------
        results_frame = ttk.LabelFrame(
            self.cascade_query_tab,
            text="Matching Cascade Systems",
            padding=10,
        )
        results_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.cascade_query_matches_tree = ttk.Treeview(
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

        self.cascade_query_matches_tree.heading(
            "system_id",
            text="System ID",
        )
        self.cascade_query_matches_tree.heading(
            "root_dams",
            text="Root Dam ID(s)",
        )
        self.cascade_query_matches_tree.heading(
            "total_dams",
            text="Total Dams",
        )
        self.cascade_query_matches_tree.heading(
            "hydroelectric_dams",
            text="Hydroelectric Dams",
        )

        self.cascade_query_matches_tree.column(
            "system_id",
            width=150,
            anchor=tk.W,
        )
        self.cascade_query_matches_tree.column(
            "root_dams",
            width=350,
            anchor=tk.W,
        )
        self.cascade_query_matches_tree.column(
            "total_dams",
            width=120,
            anchor=tk.E,
        )
        self.cascade_query_matches_tree.column(
            "hydroelectric_dams",
            width=160,
            anchor=tk.E,
        )

        matches_scrollbar = ttk.Scrollbar(
            results_frame,
            orient=tk.VERTICAL,
            command=self.cascade_query_matches_tree.yview,
        )
        self.cascade_query_matches_tree.configure(
            yscrollcommand=matches_scrollbar.set
        )

        self.cascade_query_matches_tree.pack(
            side=tk.LEFT,
            fill=tk.BOTH,
            expand=True,
        )
        matches_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Selecting a system in the results table makes it the active system
        # for the reporting and mapping controls below.
        self.cascade_query_matches_tree.bind(
            "<<TreeviewSelect>>",
            self._select_cascade_system_from_query_result,
        )

        # ------------------------------------------------------------------
        # Existing selected-system tools
        # ------------------------------------------------------------------
        selection_frame = ttk.LabelFrame(
            self.cascade_query_tab,
            text="Selected Cascade System",
            padding=12,
        )
        selection_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(
            selection_frame,
            text="System ID:",
        ).grid(
            row=0,
            column=0,
            sticky=tk.W,
            padx=(0, 10),
        )

        self.selected_system_id_text = tk.StringVar()

        self.cascade_system_combobox = ttk.Combobox(
            selection_frame,
            textvariable=self.selected_system_id_text,
            width=34,
            state="normal",
        )
        self.cascade_system_combobox.grid(
            row=0,
            column=1,
            sticky=tk.W,
        )

        self.cascade_system_combobox.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._update_selected_system_details(),
        )
        self.cascade_system_combobox.bind(
            "<Return>",
            lambda _event: self._update_selected_system_details(),
        )

        self.refresh_system_list_button = ttk.Button(
            selection_frame,
            text="Show All System IDs",
            command=self.refresh_cascade_system_list,
        )
        self.refresh_system_list_button.grid(
            row=0,
            column=2,
            sticky=tk.W,
            padx=(10, 0),
        )

        selection_frame.columnconfigure(3, weight=1)

        action_frame = ttk.Frame(self.cascade_query_tab)
        action_frame.pack(fill=tk.X, pady=(0, 10))

        self.print_cascade_button = ttk.Button(
            action_frame,
            text="Print System Information to Log",
            command=self.print_selected_cascade_graph,
        )
        self.print_cascade_button.pack(side=tk.LEFT)

        self.generate_map_button = ttk.Button(
            action_frame,
            text="Generate and Open Interactive Map",
            command=self.generate_selected_cascade_map,
        )
        self.generate_map_button.pack(side=tk.LEFT, padx=(10, 0))

        details_frame = ttk.LabelFrame(
            self.cascade_query_tab,
            text="Selected System Overview",
            padding=10,
        )
        details_frame.pack(fill=tk.BOTH, expand=True)

        self.cascade_query_results_tree = ttk.Treeview(
            details_frame,
            columns=("metric", "value"),
            show="headings",
            height=7,
        )
        self.cascade_query_results_tree.heading("metric", text="Metric")
        self.cascade_query_results_tree.heading("value", text="Value")
        self.cascade_query_results_tree.column(
            "metric",
            width=360,
            anchor=tk.W,
        )
        self.cascade_query_results_tree.column(
            "value",
            width=540,
            anchor=tk.W,
        )
        self.cascade_query_results_tree.pack(fill=tk.BOTH, expand=True)

        # Disabled controls cannot accidentally participate in a query until
        # their associated checkbox is selected.
        self._update_cascade_query_control_states()

    @staticmethod
    def _node_id_matches(stored_node_id: object, requested_node_id: int) -> bool:
        """
        Compare a pandas-stored node ID with a user-entered integer node ID.

        The DataFrame may contain integer node IDs stored as floats because
        pandas uses NaN during the initialization/matching process. For
        example, NHD node 123 may appear as 123.0 in a mixed-null column.
        This helper safely treats 123 and 123.0 as equivalent while rejecting
        invalid or fractional values.
        """

        if pd.isna(stored_node_id):
            return False

        try:
            numeric_node_id = float(stored_node_id)

            # Prevent an unexpected fractional value, such as 123.5, from
            # being mistakenly converted and matched to node 123.
            if not numeric_node_id.is_integer():
                return False

            return int(numeric_node_id) == requested_node_id

        except (TypeError, ValueError):
            return False


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

        if self.initialization_result is None:
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

        dam_inventory_matched = self.initialization_result.dam_inventory_matched

        # Use the helper instead of direct equality because pandas may have
        # represented integer graph node IDs as float values in the DataFrame.
        dams_at_node = dam_inventory_matched[
            dam_inventory_matched["node_id"].map(
                lambda stored_node_id: self._node_id_matches(
                    stored_node_id,
                    requested_node_id,
                )
            )
        ]

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
        if self.initialization_result is None:
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

        # NID ID values were normalized to strings during initialization.
        # Explicit string conversion makes the comparison robust if a future
        # data-source version causes pandas to infer another column type.
        dam_inventory_matched = self.initialization_result.dam_inventory_matched
        matching_dams = dam_inventory_matched[
            dam_inventory_matched["NID ID"].astype(str).str.strip() == requested_nid_id
        ]

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

    def _update_power_filter_state(self) -> None:
        """
        Enable the power threshold only when hydroelectric filtering is active.

        The original power-capacity calculation applies to matched
        hydroelectric dams. Disabling this field when the hydroelectric
        checkbox is not selected prevents ambiguity about whether a power
        filter should implicitly remove non-hydroelectric dams.
        """

        if self.hydroelectric_only_var.get():
            self.power_threshold_entry.configure(state="normal")
        else:
            self.power_threshold_entry.configure(state="disabled")


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
        self._update_power_filter_state()

        self._append_log(
            "Part 3 filters restored to the original algorithm defaults: "
            "storage > 100 acre-feet, hydroelectric only, and power threshold > 10 MW."
        )


    @staticmethod
    def _parse_nonnegative_float(
        raw_value: str,
        field_label: str,
    ) -> float:
        """
        Parse a nonnegative finite float from a UI input field.

        Rejecting NaN and infinity prevents misleading or unstable filtering
        behavior. A lower limit may validly be zero, but not negative.
        """

        try:
            value = float(raw_value.strip())
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{field_label} must be a valid number."
            ) from error

        if not math.isfinite(value):
            raise ValueError(
                f"{field_label} must be a finite number."
            )

        if value < 0:
            raise ValueError(
                f"{field_label} cannot be negative."
            )

        return value


    @staticmethod
    def _has_hydroelectric_purpose(purposes: object) -> bool:
        """
        Implement the original purpose test safely and case-insensitively.

        This is equivalent in intent to:

            purposes.str.contains('Hydroelectric', case=False, na=False)

        but can be used on individual values and remains safe for None/NaN.
        """

        return bool(
            pd.notna(purposes)
            and "hydroelectric" in str(purposes).casefold()
        )


    def _display_filter_results(
        self,
        result_rows: list[tuple[str, str]],
    ) -> None:
        """Replace the Part 3 result-summary table with current filter metrics."""

        for item_id in self.filter_results_tree.get_children():
            self.filter_results_tree.delete(item_id)

        for metric, value in result_rows:
            self.filter_results_tree.insert(
                "",
                tk.END,
                values=(metric, value),
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

        if self.initialization_result is None:
            messagebox.showwarning(
                APP_NAME,
                "Initialize the datasets in Part 1 before applying filters.",
            )
            return

        try:
            minimum_storage = self._parse_nonnegative_float(
                self.minimum_storage_text.get(),
                "Maximum storage lower limit",
            )

            hydroelectric_only = self.hydroelectric_only_var.get()

            power_threshold: Optional[float] = None
            if hydroelectric_only:
                power_threshold = self._parse_nonnegative_float(
                    self.power_threshold_text.get(),
                    "Estimated power threshold lower limit",
                )

        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error))
            return

        # Use a copy to preserve the Part 1 dataset unchanged. This is
        # necessary so researchers can revise criteria repeatedly without
        # reinitializing or cumulatively narrowing their source data.
        filtered_inventory = self.initialization_result.dam_inventory.copy()

        # Same strict "greater than" behavior as the original algorithm.
        filtered_inventory = filtered_inventory[
            filtered_inventory["Max Storage (Acre-Ft)"] > minimum_storage
        ].copy()

        after_storage_count = len(filtered_inventory)

        # This mirrors the original .str.contains('Hydroelectric',
        # case=False, na=False) operation.
        if hydroelectric_only:
            filtered_inventory = filtered_inventory[
                filtered_inventory["Purposes"].map(
                    self._has_hydroelectric_purpose
                )
            ].copy()

        after_purpose_count = len(filtered_inventory)

        # Only matched dams can participate in network-based analysis. A
        # merge is preferable to positional filtering because it preserves the
        # selected inventory record as the authoritative row.
        matched_columns = ["NID ID", "node_id"]

        matched_node_data = (
            self.initialization_result.dam_inventory_matched[matched_columns]
            .drop_duplicates(subset=["NID ID"], keep="first")
            .copy()
        )

        filtered_matched = filtered_inventory.merge(
            matched_node_data,
            on="NID ID",
            how="inner",
            suffixes=("", "_matched"),
        )

        # If the initialized inventory already contained node_id from its
        # original mapping stage, pandas may create node_id_matched. Keep one
        # unambiguous node_id field for future parts of the application.
        if "node_id_matched" in filtered_matched.columns:
            filtered_matched["node_id"] = filtered_matched["node_id_matched"]
            filtered_matched = filtered_matched.drop(columns=["node_id_matched"])

        before_power_count = len(filtered_matched)

        # Preserve original algorithm behavior:
        #
        # hydroelectric_dams_matched_filtered =
        #     hydroelectric_dams_matched[
        #         Hydraulic Height * Max Discharge / POWER_CONVERSION_FACTOR > 10
        #     ]
        #
        # Missing or nonnumeric values produce NaN and therefore do not pass
        # the threshold comparison.
        if hydroelectric_only and power_threshold is not None:
            hydraulic_height = pd.to_numeric(
                filtered_matched["Hydraulic Height (Ft)"],
                errors="coerce",
            )
            maximum_discharge = pd.to_numeric(
                filtered_matched["Max Discharge (Cubic Ft/Second)"],
                errors="coerce",
            )

            filtered_matched["estimated_power_capacity"] = (
                hydraulic_height
                * maximum_discharge
                / POWER_CONVERSION_FACTOR
            )

            filtered_matched = filtered_matched[
                filtered_matched["estimated_power_capacity"] > power_threshold
            ].copy()

            # Keep the inventory-level filtered dataset consistent with the
            # power-qualified, matched hydroelectric root candidates.
            qualified_ids = set(filtered_matched["NID ID"])
            filtered_inventory = filtered_inventory[
                filtered_inventory["NID ID"].isin(qualified_ids)
            ].copy()

        # Store copies as application state for Parts 4–6.
        self.filtered_dam_inventory = filtered_inventory
        self.filtered_dam_inventory_matched = filtered_matched

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

        self._display_filter_results(result_rows)

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

    def _display_downstream_search_results(
        self,
        result_rows: list[tuple[str, str]],
    ) -> None:
        """Replace the Part 4 summary table with current search statistics."""

        for item_id in self.downstream_results_tree.get_children():
            self.downstream_results_tree.delete(item_id)

        for metric, value in result_rows:
            self.downstream_results_tree.insert(
                "",
                tk.END,
                values=(metric, value),
            )


    def start_downstream_search(self) -> None:
        """
        Validate Part 4 settings and start the graph search in a worker thread.

        The search always uses the complete initialized matched inventory:
        self.initialization_result.dam_inventory_matched

        It does not use Part 3's filtered set. This is intentional because
        Part 4 produces a broad reusable dam-to-dam connectivity reference.
        """

        if self.initialization_result is None:
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
            maximum_distance = self._parse_nonnegative_float(
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

        matched_dams = self.initialization_result.dam_inventory_matched

        if matched_dams.empty:
            messagebox.showwarning(
                APP_NAME,
                "No matched dams are available for downstream searching.",
            )
            return

        output_file = self.cache_dir / DOWNSTREAM_LINKS_CSV_NAME

        self.run_downstream_search_button.configure(state=tk.DISABLED)
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
        self._append_log(f"Reference CSV destination: {output_file}")

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
        Run the full downstream-link search without blocking the Tkinter UI.

        The worker does not modify Tkinter widgets. It reports progress and
        sends results to the main thread through ui_message_queue.
        """

        try:
            if self.initialization_result is None:
                raise RuntimeError(
                    "Initialization state was unavailable when the search began."
                )

            graph = self.initialization_result.graph
            matched_dams = self.initialization_result.dam_inventory_matched.copy()

            # The node_id column can contain float representations of integer
            # graph node IDs because pandas columns that previously contained
            # missing values may use floating-point storage. Python considers
            # integer 123 and float 123.0 equivalent as dictionary/set keys,
            # but this conversion gives cleaner, consistent graph lookups.
            def normalize_graph_node_id(node_id: Any) -> Any:
                if pd.isna(node_id):
                    return None

                if isinstance(node_id, float) and node_id.is_integer():
                    return int(node_id)

                return node_id

            matched_dams["node_id"] = matched_dams["node_id"].map(
                normalize_graph_node_id
            )
            matched_dams = matched_dams.dropna(subset=["node_id"]).copy()

            # Map each NHD node to all dams located on that node. The original
            # algorithm used one NID ID per node; this keeps the behavior
            # deterministic while guarding against occasional many-dams-to-one-
            # node records in future NID releases.
            node_to_dam_ids: dict[Any, list[str]] = {}

            for _, dam_row in matched_dams.iterrows():
                node_id = dam_row["node_id"]
                dam_id = normalize_identifier(dam_row.get("NID ID"))

                if node_id is not None and dam_id is not None:
                    node_to_dam_ids.setdefault(node_id, []).append(dam_id)

            for dam_ids in node_to_dam_ids.values():
                dam_ids.sort()

            dam_nodes = set(node_to_dam_ids)

            # Use NID IDs as stable keys for source-record lookup.
            matched_by_nid = (
                matched_dams
                .drop_duplicates(subset=["NID ID"], keep="first")
                .set_index("NID ID", drop=False)
            )

            duplicate_node_count = sum(
                1
                for dam_ids in node_to_dam_ids.values()
                if len(dam_ids) > 1
            )

            if duplicate_node_count:
                self.ui_message_queue.put(
                    (
                        "log",
                        (
                            "Part 4 notice: "
                            f"{duplicate_node_count:,} NHD node(s) contain multiple "
                            "matched NID dams. The alphanumerically first NID ID at "
                            "each downstream target node is used as the direct "
                            "downstream reference."
                        ),
                    )
                )

            total = len(matched_dams)
            start_time = time.time()
            result_rows: list[dict[str, Any]] = []

            for index, (_, dam_row) in enumerate(matched_dams.iterrows(), start=1):
                upstream_node = dam_row["node_id"]
                upstream_dam_id = normalize_identifier(dam_row.get("NID ID"))

                downstream_node, distance_miles = find_downstream_dam(
                    graph=graph,
                    start_node=upstream_node,
                    dam_nodes=dam_nodes,
                    max_distance=maximum_distance,
                )

                downstream_dam_id: Optional[str] = None
                downstream_dam_name: Optional[str] = None
                downstream_dam_purposes: Optional[str] = None

                if downstream_node is not None:
                    target_dam_ids = node_to_dam_ids.get(downstream_node, [])

                    if target_dam_ids:
                        downstream_dam_id = target_dam_ids[0]

                        if downstream_dam_id in matched_by_nid.index:
                            downstream_record = matched_by_nid.loc[
                                downstream_dam_id
                            ]
                            downstream_dam_name = downstream_record.get(
                                "Dam Name"
                            )
                            downstream_dam_purposes = downstream_record.get(
                                "Purposes"
                            )

                # Preserve the original CSV fields, plus river distance for
                # direct use in later system construction and visualization.
                result_rows.append(
                    {
                        "Dam": upstream_dam_id,
                        "Dam Name": dam_row.get("Dam Name"),
                        "Purposes": dam_row.get("Purposes"),
                        "Downstream Dam": downstream_dam_id,
                        "Downstream Dam Name": downstream_dam_name,
                        "Downstream Dam Purposes": downstream_dam_purposes,
                        "Distance (Miles)": (
                            round(distance_miles, 2)
                            if distance_miles is not None
                            else None
                        ),
                    }
                )

                # Report periodic progress without overwhelming the UI queue.
                if index % 500 == 0 or index == total:
                    elapsed_seconds = time.time() - start_time
                    processing_rate = (
                        index / elapsed_seconds if elapsed_seconds > 0 else 0.0
                    )
                    remaining_seconds = (
                        (total - index) / processing_rate
                        if processing_rate > 0
                        else 0.0
                    )

                    self.ui_message_queue.put(
                        (
                            "downstream_progress",
                            {
                                "processed": index,
                                "total": total,
                                "elapsed_seconds": elapsed_seconds,
                                "remaining_seconds": remaining_seconds,
                            },
                        )
                    )

            results_dataframe = pd.DataFrame(
                result_rows,
                columns=[
                    "Dam",
                    "Dam Name",
                    "Purposes",
                    "Downstream Dam",
                    "Downstream Dam Name",
                    "Downstream Dam Purposes",
                    "Distance (Miles)",
                ],
            )

            results_dataframe = results_dataframe.sort_values(
                "Dam",
                kind="stable",
            ).reset_index(drop=True)

            write_downstream_links_csv(results_dataframe, output_file)

            linked_dam_count = int(
                results_dataframe["Downstream Dam"].notna().sum()
            )
            elapsed_total_seconds = time.time() - start_time

            self.ui_message_queue.put(
                (
                    "downstream_success",
                    {
                        "dataframe": results_dataframe,
                        "output_file": output_file,
                        "total_dams": total,
                        "linked_dams": linked_dam_count,
                        "duplicate_node_count": duplicate_node_count,
                        "maximum_distance": maximum_distance,
                        "elapsed_seconds": elapsed_total_seconds,
                    },
                )
            )

        except Exception as error:
            self.ui_message_queue.put(
                ("downstream_failure", str(error))
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


    @staticmethod
    def _parse_nonnegative_integer(
        raw_value: str,
        field_label: str,
    ) -> int:
        """
        Parse a nonnegative integer without accepting floats such as 2.5.

        The minimum hydroelectric-dam criterion is a count, not a continuous
        measurement, so only whole numbers are valid.
        """

        cleaned_value = raw_value.strip()

        if not cleaned_value:
            raise ValueError(f"{field_label} is required.")

        try:
            value = int(cleaned_value)
        except ValueError as error:
            raise ValueError(
                f"{field_label} must be a whole number greater than or equal to 0."
            ) from error

        if value < 0:
            raise ValueError(
                f"{field_label} must be greater than or equal to 0."
            )

        return value


    def _display_cascade_results(
        self,
        result_rows: list[tuple[str, str]],
    ) -> None:
        """Replace the Part 5 construction-summary table contents."""

        for item_id in self.cascade_results_tree.get_children():
            self.cascade_results_tree.delete(item_id)

        for metric, value in result_rows:
            self.cascade_results_tree.insert(
                "",
                tk.END,
                values=(metric, value),
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

        if self.initialization_result is None:
            messagebox.showwarning(
                APP_NAME,
                "Initialize data in Part 1 before constructing cascades.",
            )
            return

        if self.filtered_dam_inventory_matched is None:
            messagebox.showwarning(
                APP_NAME,
                "Apply Part 3 filters before constructing cascade systems.",
            )
            return

        if self.downstream_links is None:
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
            minimum_hydroelectric_dams = self._parse_nonnegative_integer(
                self.minimum_hydroelectric_dams_text.get(),
                "Minimum hydroelectric dams per cascading system",
            )
        except ValueError as error:
            messagebox.showwarning(APP_NAME, str(error))
            return

        require_same_owner = self.require_same_owner_var.get()

        edge_csv_path = self.cache_dir / CASCADE_SYSTEMS_CSV_NAME
        summary_csv_path = self.cache_dir / CASCADE_SYSTEMS_SUMMARY_CSV_NAME

        self.construct_cascades_button.configure(state=tk.DISABLED)
        self.cascade_construction_status_text.set(
            "Constructing cascade-system graphs..."
        )

        self._append_log("")
        self._append_log("=" * 60)
        self._append_log("Part 5 — Cascade-System Construction Started")
        self._append_log("=" * 60)
        self._append_log(
            f"Part 3 matched root candidates: "
            f"{len(self.filtered_dam_inventory_matched):,}"
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
                edge_csv_path,
                summary_csv_path,
            ),
            daemon=True,
        )
        self.cascade_construction_thread.start()


    def _run_cascade_construction_worker(
        self,
        minimum_hydroelectric_dams: int,
        require_same_owner: bool,
        edge_csv_path: Path,
        summary_csv_path: Path,
    ) -> None:
        """
        Construct branching cascade systems from Part 4 downstream links.

        A system is built in these stages:
        1. Build a direct dam-to-dam downstream mapping from Part 4.
        2. Build one downstream chain for each Part 3 root candidate.
        3. Exclude a candidate if it is already downstream of another
           candidate chain; remaining candidates are true roots.
        4. Require each initial chain to have at least two dams.
        5. Require the configured minimum count of hydroelectric dams.
        6. Merge qualifying chains that share dam nodes into connected,
           potentially branching cascade systems.
        7. Export a system edge list and a companion system summary.

        Tkinter widgets are never updated by this worker directly.
        """

        try:
            if self.initialization_result is None:
                raise RuntimeError("Initialization data are unavailable.")

            if self.filtered_dam_inventory_matched is None:
                raise RuntimeError("Part 3 filtered candidates are unavailable.")

            if self.downstream_links is None:
                raise RuntimeError("Part 4 downstream links are unavailable.")

            matched_inventory = (
                self.initialization_result.dam_inventory_matched
                .copy()
                .drop_duplicates(subset=["NID ID"], keep="first")
            )

            # Normalize all NID IDs as strings, preserving leading zeros and
            # state prefixes. This ensures alignment across NID, Part 3, and
            # Part 4 CSV-derived relationship data.
            matched_inventory["NID ID"] = matched_inventory["NID ID"].map(
                normalize_identifier
            )
            matched_inventory = matched_inventory.dropna(
                subset=["NID ID"]
            ).copy()

            dam_records_by_id = (
                matched_inventory
                .set_index("NID ID", drop=False)
                .to_dict(orient="index")
            )

            # Build a stable direct mapping from the Part 4 output. Invalid or
            # missing downstream identifiers represent a terminal dam.
            downstream_map: dict[str, str] = {}
            distance_by_edge: dict[tuple[str, str], Optional[float]] = {}

            for _, link_row in self.downstream_links.iterrows():
                upstream_dam_id = normalize_identifier(link_row.get("Dam"))
                downstream_dam_id = normalize_identifier(
                    link_row.get("Downstream Dam")
                )

                if upstream_dam_id is None:
                    continue

                # Only preserve links whose endpoints exist in the initialized
                # matched inventory. This rejects stale/incompatible external
                # CSV data and prevents invalid graph nodes from being created.
                if (
                    downstream_dam_id is not None
                    and upstream_dam_id in dam_records_by_id
                    and downstream_dam_id in dam_records_by_id
                ):
                    downstream_map[upstream_dam_id] = downstream_dam_id

                    raw_distance = link_row.get("Distance (Miles)")
                    try:
                        distance = float(raw_distance)
                        if not math.isfinite(distance) or distance < 0:
                            distance = None
                    except (TypeError, ValueError):
                        distance = None

                    distance_by_edge[
                        (upstream_dam_id, downstream_dam_id)
                    ] = distance

            # Part 3 creates the cascade starting-candidate set. Duplicates
            # cannot produce duplicate systems, so normalize to a set.
            root_candidate_ids = {
                normalize_identifier(dam_id)
                for dam_id in self.filtered_dam_inventory_matched["NID ID"]
                if normalize_identifier(dam_id) is not None
                and normalize_identifier(dam_id) in dam_records_by_id
            }

            if not root_candidate_ids:
                raise RuntimeError(
                    "Part 3 produced no matched root candidates for cascade construction."
                )

            def get_owner(dam_id: str) -> Optional[str]:
                """Return a safely normalized owner value for comparison."""

                owner = dam_records_by_id[dam_id].get("Owner Names")

                if owner is None or pd.isna(owner):
                    return None

                normalized_owner = str(owner).strip()
                return normalized_owner if normalized_owner else None

            def have_same_owner(
                root_dam_id: str,
                candidate_dam_id: str,
            ) -> bool:
                """
                Require known, equal owner strings.

                Missing owner values do not satisfy same-owner mode because
                ownership cannot be affirmatively verified.
                """

                root_owner = get_owner(root_dam_id)
                candidate_owner = get_owner(candidate_dam_id)

                return (
                    root_owner is not None
                    and candidate_owner is not None
                    and root_owner == candidate_owner
                )

            def is_hydroelectric_dam(dam_id: str) -> bool:
                """Use the same purpose semantics as Part 3."""

                return self._has_hydroelectric_purpose(
                    dam_records_by_id[dam_id].get("Purposes")
                )

            def build_full_chain(start_dam_id: str) -> tuple[list[str], bool]:
                """
                Follow direct Part 4 links until a terminal, cycle, or owner
                boundary is encountered.

                Returns:
                    chain: ordered dam IDs from upstream root to terminus.
                    cycle_detected: True if malformed/stale source links form
                    a cycle; the chain is safely stopped before repetition.
                """

                chain = [start_dam_id]
                visited = {start_dam_id}
                current_dam_id = start_dam_id
                cycle_detected = False

                while True:
                    downstream_dam_id = downstream_map.get(current_dam_id)

                    if downstream_dam_id is None:
                        break

                    if downstream_dam_id in visited:
                        cycle_detected = True
                        break

                    # Match the original algorithm's behavior: every
                    # downstream dam must match the original root's owner,
                    # rather than only matching its immediate predecessor.
                    if (
                        require_same_owner
                        and not have_same_owner(
                            start_dam_id,
                            downstream_dam_id,
                        )
                    ):
                        break

                    chain.append(downstream_dam_id)
                    visited.add(downstream_dam_id)
                    current_dam_id = downstream_dam_id

                return chain, cycle_detected

            # Construct a downstream chain from every selected root candidate.
            candidate_chains: dict[str, list[str]] = {}
            cycle_count = 0

            for candidate_dam_id in sorted(root_candidate_ids):
                chain, cycle_detected = build_full_chain(candidate_dam_id)
                candidate_chains[candidate_dam_id] = chain

                if cycle_detected:
                    cycle_count += 1

            # A candidate located downstream in another candidate's chain is
            # not a top-level cascade root. This preserves your original root
            # de-duplication logic.
            covered_candidate_ids: set[str] = set()

            for candidate_dam_id, chain in candidate_chains.items():
                del candidate_dam_id

                for downstream_dam_id in chain[1:]:
                    if downstream_dam_id in root_candidate_ids:
                        covered_candidate_ids.add(downstream_dam_id)

            true_root_ids = sorted(
                root_candidate_ids - covered_candidate_ids
            )

            # A qualifying initial cascade always requires >= 2 total dams,
            # independently of whether min hydro is 0, 1, 2, or greater.
            initial_chains = [
                candidate_chains[root_dam_id]
                for root_dam_id in true_root_ids
                if len(candidate_chains[root_dam_id]) >= 2
            ]

            total_dam_filter_count = len(initial_chains)

            def count_hydroelectric_dams(chain: list[str]) -> int:
                return sum(
                    1
                    for dam_id in chain
                    if is_hydroelectric_dam(dam_id)
                )

            qualifying_chains = [
                chain
                for chain in initial_chains
                if count_hydroelectric_dams(chain)
                >= minimum_hydroelectric_dams
            ]

            # Union-find groups chains sharing at least one dam node. That
            # turns convergent linear chains into one connected system.
            parent: dict[str, str] = {}

            def dsu_find(dam_id: str) -> str:
                parent.setdefault(dam_id, dam_id)

                while parent[dam_id] != dam_id:
                    parent[dam_id] = parent[parent[dam_id]]
                    dam_id = parent[dam_id]

                return dam_id

            def dsu_union(dam_id_a: str, dam_id_b: str) -> None:
                root_a = dsu_find(dam_id_a)
                root_b = dsu_find(dam_id_b)

                if root_a != root_b:
                    parent[root_a] = root_b

            for chain in qualifying_chains:
                for dam_id in chain:
                    dsu_find(dam_id)

                for upstream_dam_id, downstream_dam_id in zip(
                    chain[:-1],
                    chain[1:],
                ):
                    dsu_union(upstream_dam_id, downstream_dam_id)

            group_nodes: dict[str, set[str]] = {}
            group_edges: dict[str, set[tuple[str, str]]] = {}

            for chain in qualifying_chains:
                group_key = dsu_find(chain[0])

                group_nodes.setdefault(group_key, set()).update(chain)
                group_edges.setdefault(group_key, set()).update(
                    zip(chain[:-1], chain[1:])
                )

            edge_rows: list[dict[str, Any]] = []
            summary_rows: list[dict[str, Any]] = []
            cascade_graphs: dict[str, nx.DiGraph] = {}

            for group_key in sorted(group_nodes):
                system_nodes = group_nodes[group_key]
                system_edges = group_edges[group_key]

                # A defensive check preserves the universal two-dam minimum
                # even if future merge logic is altered.
                if len(system_nodes) < 2:
                    continue

                incoming_edge_count = {
                    dam_id: 0
                    for dam_id in system_nodes
                }

                for _, downstream_dam_id in system_edges:
                    incoming_edge_count[downstream_dam_id] += 1

                root_dams_in_system = sorted(
                    dam_id
                    for dam_id, count in incoming_edge_count.items()
                    if count == 0
                )

                # Every valid directed acyclic system should have a root. If
                # malformed data produced otherwise, skip safely and report it.
                if not root_dams_in_system:
                    continue

                system_id = min(root_dams_in_system)

                hydroelectric_count = sum(
                    1
                    for dam_id in system_nodes
                    if is_hydroelectric_dam(dam_id)
                )

                # A merged graph could theoretically change system-level
                # composition. Reapply both requirements as an explicit,
                # auditable final validation.
                if (
                    len(system_nodes) < 2
                    or hydroelectric_count < minimum_hydroelectric_dams
                ):
                    continue

                cascade_graph = nx.DiGraph()

                for dam_id in sorted(system_nodes):
                    dam_record = dam_records_by_id[dam_id]

                    # Normalize pandas missing values before saving graph
                    # attributes. Do not use pickle on untrusted sources.
                    node_attributes = {
                        key: (
                            None
                            if pd.isna(value)
                            else value
                        )
                        for key, value in dam_record.items()
                    }

                    node_attributes["NID ID"] = dam_id
                    node_attributes["name"] = dam_record.get("Dam Name")
                    node_attributes["purposes"] = dam_record.get("Purposes")
                    node_attributes["owner"] = dam_record.get("Owner Names")
                    node_attributes["is_hydroelectric"] = (
                        is_hydroelectric_dam(dam_id)
                    )

                    cascade_graph.add_node(
                        dam_id,
                        **node_attributes,
                    )

                for upstream_dam_id, downstream_dam_id in sorted(system_edges):
                    distance = distance_by_edge.get(
                        (upstream_dam_id, downstream_dam_id)
                    )

                    cascade_graph.add_edge(
                        upstream_dam_id,
                        downstream_dam_id,
                        distance_miles=distance,
                    )

                    upstream_record = dam_records_by_id[upstream_dam_id]
                    downstream_record = dam_records_by_id[downstream_dam_id]

                    edge_rows.append(
                        {
                            "System ID": system_id,
                            "Upstream Dam ID": upstream_dam_id,
                            "Upstream Dam Name": upstream_record.get(
                                "Dam Name"
                            ),
                            "Upstream Dam Purposes": upstream_record.get(
                                "Purposes"
                            ),
                            "Upstream Dam Owner": upstream_record.get(
                                "Owner Names"
                            ),
                            "Downstream Dam ID": downstream_dam_id,
                            "Downstream Dam Name": downstream_record.get(
                                "Dam Name"
                            ),
                            "Downstream Dam Purposes": downstream_record.get(
                                "Purposes"
                            ),
                            "Downstream Dam Owner": downstream_record.get(
                                "Owner Names"
                            ),
                            "Distance (Miles)": (
                                round(distance, 2)
                                if distance is not None
                                else None
                            ),
                        }
                    )

                summary_rows.append(
                    {
                        "System ID": system_id,
                        "Root Dam IDs": "; ".join(root_dams_in_system),
                        "Root Dam Names": "; ".join(
                            str(
                                dam_records_by_id[dam_id].get(
                                    "Dam Name",
                                    dam_id,
                                )
                            )
                            for dam_id in root_dams_in_system
                        ),
                        "Total Dams": len(system_nodes),
                        "Hydroelectric Dams": hydroelectric_count,
                    }
                )

                cascade_graphs[system_id] = cascade_graph

            edge_dataframe = pd.DataFrame(
                edge_rows,
                columns=[
                    "System ID",
                    "Upstream Dam ID",
                    "Upstream Dam Name",
                    "Upstream Dam Purposes",
                    "Upstream Dam Owner",
                    "Downstream Dam ID",
                    "Downstream Dam Name",
                    "Downstream Dam Purposes",
                    "Downstream Dam Owner",
                    "Distance (Miles)",
                ],
            )

            summary_dataframe = pd.DataFrame(
                summary_rows,
                columns=[
                    "System ID",
                    "Root Dam IDs",
                    "Root Dam Names",
                    "Total Dams",
                    "Hydroelectric Dams",
                ],
            )

            if not edge_dataframe.empty:
                edge_dataframe = edge_dataframe.sort_values(
                    ["System ID", "Upstream Dam ID", "Downstream Dam ID"],
                    kind="stable",
                ).reset_index(drop=True)

            if not summary_dataframe.empty:
                summary_dataframe = summary_dataframe.sort_values(
                    "System ID",
                    kind="stable",
                ).reset_index(drop=True)

            # The existing atomic CSV writer is generic despite its original
            # Part 4 name. It also protects spreadsheet users from formula
            # injection in externally sourced dam-name/owner text.
            write_downstream_links_csv(
                edge_dataframe,
                edge_csv_path,
            )
            write_downstream_links_csv(
                summary_dataframe,
                summary_csv_path,
            )

            multi_root_system_count = sum(
                1
                for graph in cascade_graphs.values()
                if sum(
                    1
                    for dam_id in graph.nodes
                    if graph.in_degree(dam_id) == 0
                ) > 1
            )

            self.ui_message_queue.put(
                (
                    "cascade_construction_success",
                    {
                        "cascade_graphs": cascade_graphs,
                        "edge_dataframe": edge_dataframe,
                        "summary_dataframe": summary_dataframe,
                        "edge_csv_path": edge_csv_path,
                        "summary_csv_path": summary_csv_path,
                        "root_candidate_count": len(root_candidate_ids),
                        "covered_candidate_count": len(covered_candidate_ids),
                        "true_root_count": len(true_root_ids),
                        "two_dam_chain_count": total_dam_filter_count,
                        "qualifying_chain_count": len(qualifying_chains),
                        "system_count": len(cascade_graphs),
                        "edge_count": len(edge_dataframe),
                        "multi_root_system_count": multi_root_system_count,
                        "cycle_count": cycle_count,
                        "minimum_hydroelectric_dams": (
                            minimum_hydroelectric_dams
                        ),
                        "require_same_owner": require_same_owner,
                    },
                )
            )

        except Exception as error:
            self.ui_message_queue.put(
                ("cascade_construction_failure", str(error))
            )
    def refresh_cascade_system_list(self) -> None:
        """
        Restore the complete set of constructed systems in Part 6.

        The Show All System IDs button deliberately clears query restrictions,
        ensuring the combobox and results table return to their full state.
        """

        if not self.cascade_graphs:
            self.cascade_system_combobox.configure(values=[])

            self.selected_system_id_text.set("")
            self.cascade_query_status_text.set(
                "No cascade systems are currently available."
            )

            self._display_cascade_query_matches([])

            self._display_cascade_query_results(
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

        if not self.cascade_graphs:
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

        cascade_graph = self.cascade_graphs.get(system_id)

        if cascade_graph is None:
            messagebox.showwarning(
                APP_NAME,
                f"No constructed cascade system was found for System ID '{system_id}'.",
            )
            return None, None

        return system_id, cascade_graph

    def _display_cascade_query_results(
        self,
        result_rows: list[tuple[str, str]],
    ) -> None:
        """Replace the Part 6 selected-system overview table."""

        for item_id in self.cascade_query_results_tree.get_children():
            self.cascade_query_results_tree.delete(item_id)

        for metric, value in result_rows:
            self.cascade_query_results_tree.insert(
                "",
                tk.END,
                values=(metric, value),
            )

    def _update_cascade_query_control_states(self) -> None:
        """
        Enable a query input only when its checkbox is selected.

        This makes each query type independently optional and communicates
        clearly which criteria will apply during the next search.
        """

        self.query_state_entry.configure(
            state=(
                "normal"
                if self.query_state_enabled_var.get()
                else "disabled"
            )
        )

        self.query_nid_entry.configure(
            state=(
                "normal"
                if self.query_nid_enabled_var.get()
                else "disabled"
            )
        )

        self.query_river_entry.configure(
            state=(
                "normal"
                if self.query_river_enabled_var.get()
                else "disabled"
            )
        )


    @staticmethod
    def _get_node_attribute_case_insensitive(
        attributes: dict[str, Any],
        candidate_names: tuple[str, ...],
    ) -> Any:
        """
        Retrieve a graph node attribute using case-insensitive field matching.

        NID field naming can vary by pygeohydro/NID release. For example, a
        state field may appear as State, STATE, or state. This helper avoids
        making Part 6 dependent on one exact source-column spelling.
        """

        normalized_attributes = {
            str(key).casefold(): value
            for key, value in attributes.items()
        }

        for candidate_name in candidate_names:
            value = normalized_attributes.get(candidate_name.casefold())

            if value is not None:
                return value

        return None


    def _get_dam_state_code(
        self,
        dam_id: str,
        attributes: dict[str, Any],
    ) -> Optional[str]:
        """
        Resolve a two-letter state code for a dam node.

        The NID State field is preferred. If it is absent or unusable, use the
        first two characters of the NID ID as a compatibility fallback. Your
        original cascade-map workflow used this NID prefix convention for
        state-based output organization.
        """

        raw_state = self._get_node_attribute_case_insensitive(
            attributes,
            (
                "State",
            ),
        )

        if raw_state is not None:
            state_text = str(raw_state).strip().upper()

            if len(state_text) == 2 and state_text.isalpha():
                return state_text

        normalized_dam_id = normalize_identifier(dam_id)

        if normalized_dam_id is not None:
            prefix = normalized_dam_id[:2].upper()

            if len(prefix) == 2 and prefix.isalpha():
                return prefix

        return None


    def _get_dam_river_name(
        self,
        attributes: dict[str, Any],
    ) -> Optional[str]:
        """
        Return a dam's NID river/stream name.

        Part 1 preserves the full NID record as cascade graph node attributes,
        and the authoritative NID field is 'River or Stream Name'.
        """

        raw_river_name = attributes.get("River or Stream Name")

        if raw_river_name is None:
            return None

        try:
            if pd.isna(raw_river_name):
                return None
        except (TypeError, ValueError):
            # Preserve normal conversion for a non-scalar future value.
            pass

        river_name = str(raw_river_name).strip()

        return river_name if river_name else None


    @staticmethod
    def _system_root_dam_ids(
        cascade_graph: nx.DiGraph,
    ) -> list[str]:
        """Return stable, sorted root dam IDs for one cascade system."""

        return sorted(
            str(dam_id)
            for dam_id in cascade_graph.nodes
            if cascade_graph.in_degree(dam_id) == 0
        )


    @staticmethod
    def _system_hydroelectric_dam_count(
        cascade_graph: nx.DiGraph,
    ) -> int:
        """Count hydroelectric dam nodes in a constructed cascade system."""

        return sum(
            1
            for _, attributes in cascade_graph.nodes(data=True)
            if bool(attributes.get("is_hydroelectric", False))
        )


    def _display_cascade_query_matches(
        self,
        matching_system_ids: list[str],
    ) -> None:
        """
        Populate the Part 6 results table with matching cascade systems.

        The Treeview item ID is the system ID. That lets a row selection
        directly activate the selected system for reporting and mapping.
        """

        for item_id in self.cascade_query_matches_tree.get_children():
            self.cascade_query_matches_tree.delete(item_id)

        for system_id in matching_system_ids:
            cascade_graph = self.cascade_graphs[system_id]
            root_dam_ids = self._system_root_dam_ids(cascade_graph)

            self.cascade_query_matches_tree.insert(
                "",
                tk.END,
                iid=system_id,
                values=(
                    system_id,
                    "; ".join(root_dam_ids),
                    cascade_graph.number_of_nodes(),
                    self._system_hydroelectric_dam_count(cascade_graph),
                ),
            )


    def _select_cascade_system_from_query_result(
        self,
        _event: Any = None,
    ) -> None:
        """
        Make a selected query-results row the active Part 6 system.

        This connects the filter-result list directly to the existing detailed
        log/report/map functions.
        """

        selected_items = self.cascade_query_matches_tree.selection()

        if not selected_items:
            return

        system_id = selected_items[0]

        if system_id not in self.cascade_graphs:
            return

        self.selected_system_id_text.set(system_id)
        self._update_selected_system_details()


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

        self._update_cascade_query_control_states()
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

        if not self.cascade_graphs:
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

        requested_nid_normalized = (
            requested_nid_id.casefold()
            if nid_filter_enabled
            else ""
        )

        requested_river_normalized = (
            requested_river.casefold()
            if river_filter_enabled
            else ""
        )

        matching_system_ids: list[str] = []
        matching_details: dict[str, dict[str, list[str]]] = {}

        for system_id in sorted(self.cascade_graphs):
            cascade_graph = self.cascade_graphs[system_id]

            state_matching_dams: list[str] = []
            nid_matching_dams: list[str] = []
            river_matching_dams: list[str] = []

            for dam_id, attributes in cascade_graph.nodes(data=True):
                normalized_dam_id = normalize_identifier(dam_id)

                if (
                    state_filter_enabled
                    and self._get_dam_state_code(
                        str(dam_id),
                        attributes,
                    ) == requested_state
                ):
                    state_matching_dams.append(str(dam_id))

                if (
                    nid_filter_enabled
                    and normalized_dam_id is not None
                    and normalized_dam_id.casefold()
                    == requested_nid_normalized
                ):
                    nid_matching_dams.append(str(dam_id))

                if river_filter_enabled:
                    river_name = self._get_dam_river_name(attributes)

                    if (
                        river_name is not None
                        and requested_river_normalized
                        in river_name.casefold()
                    ):
                        river_matching_dams.append(
                            f"{dam_id} ({river_name})"
                        )

            # No enabled condition means "show all systems." Otherwise, each
            # enabled condition must have at least one matching dam node.
            matches_state = (
                not state_filter_enabled
                or bool(state_matching_dams)
            )
            matches_nid = (
                not nid_filter_enabled
                or bool(nid_matching_dams)
            )
            matches_river = (
                not river_filter_enabled
                or bool(river_matching_dams)
            )

            if matches_state and matches_nid and matches_river:
                matching_system_ids.append(system_id)
                matching_details[system_id] = {
                    "state": sorted(state_matching_dams),
                    "nid": sorted(nid_matching_dams),
                    "river": sorted(river_matching_dams),
                }

        # Preserve the exact system set used for the current query. Part 6's
        # national overview map uses this value rather than independently
        # rerunning or potentially differing from the visible query results.
        self.last_cascade_query_system_ids = matching_system_ids.copy()

        # Update both the results table and the selected-system combobox.
        self._display_cascade_query_matches(matching_system_ids)

        self.cascade_system_combobox.configure(
            values=matching_system_ids
        )

        if matching_system_ids:
            current_system_id = self.selected_system_id_text.get().strip()

            if current_system_id not in matching_system_ids:
                self.selected_system_id_text.set(matching_system_ids[0])

            self._update_selected_system_details()

        else:
            self.selected_system_id_text.set("")

            self._display_cascade_query_results(
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
            f"{len(self.cascade_graphs):,} cascade system(s) match."
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
            f"{len(self.cascade_graphs):,}"
        )

        if not matching_system_ids:
            self._append_log(
                "No cascading systems met all enabled query criteria."
            )
            self._append_log("")
            return

        self._append_log("")

        for system_id in matching_system_ids:
            cascade_graph = self.cascade_graphs[system_id]
            root_dam_ids = self._system_root_dam_ids(cascade_graph)
            hydroelectric_count = self._system_hydroelectric_dam_count(
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

    @staticmethod
    def _display_value(value: Any, fallback: str = "Unavailable") -> str:
        """
        Convert a potentially missing NID attribute to safe readable text.

        The application uses None for normalized missing graph attributes, but
        this method also tolerates NaN values if a future dataset contributes
        one.
        """

        if value is None:
            return fallback

        try:
            if pd.isna(value):
                return fallback
        except (TypeError, ValueError):
            # Some non-scalar values cannot be evaluated by pd.isna in a
            # simple boolean context. Convert those values normally.
            pass

        text = str(value).strip()
        return text if text else fallback

    def _update_selected_system_details(self) -> None:
        """
        Populate the Part 6 system overview table for the current selection.

        This produces a compact high-level view. The full dam-node and
        downstream-edge detail is written by print_selected_cascade_graph().
        """

        system_id = self.selected_system_id_text.get().strip()
        cascade_graph = self.cascade_graphs.get(system_id)

        if cascade_graph is None:
            self._display_cascade_query_results(
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
                f"({self._display_value(cascade_graph.nodes[dam_id].get('name'))})"
            )
            for dam_id in root_dams
        )

        terminal_descriptions = "; ".join(
            (
                f"{dam_id} "
                f"({self._display_value(cascade_graph.nodes[dam_id].get('name'))})"
            )
            for dam_id in terminal_dams
        )

        self._display_cascade_query_results(
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
                f"({self._display_value(cascade_graph.nodes[dam_id].get('name'))})"
            )
            for dam_id in root_dams
        )

        terminal_descriptions = ", ".join(
            (
                f"{dam_id} "
                f"({self._display_value(cascade_graph.nodes[dam_id].get('name'))})"
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

            dam_name = self._display_value(attributes.get("name"))
            owner = self._display_value(attributes.get("owner"))
            latitude = self._display_value(attributes.get("Latitude"))
            longitude = self._display_value(attributes.get("Longitude"))
            purposes = self._display_value(attributes.get("purposes"))

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

            upstream_name = self._display_value(
                cascade_graph.nodes[upstream_dam_id].get("name")
            )
            downstream_name = self._display_value(
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

    @staticmethod
    def _get_valid_map_coordinate(
        value: Any,
        minimum: float,
        maximum: float,
    ) -> Optional[float]:
        """
        Validate and normalize a geographic coordinate for map rendering.

        Invalid NID coordinate fields are excluded from the map rather than
        allowing a malformed record to prevent visualization of an otherwise
        valid cascade system.
        """

        try:
            coordinate = float(value)
        except (TypeError, ValueError):
            return None

        if not math.isfinite(coordinate):
            return None

        if coordinate < minimum or coordinate > maximum:
            return None

        return coordinate

    def generate_selected_cascade_map(self) -> None:
        """
        Build, save, and open an interactive Folium map for one cascade system.

        The generated HTML is displayed in the user's external browser because
        the standard Tkinter library does not provide a maintained, secure,
        full-featured embedded web renderer.
        """

        system_id, cascade_graph = self._get_selected_cascade_graph()

        if system_id is None or cascade_graph is None:
            return

        # Build a coordinate map first. This ensures only nodes with valid
        # coordinates participate in marker and line generation.
        coordinates_by_dam: dict[str, tuple[float, float]] = {}

        for dam_id, attributes in cascade_graph.nodes(data=True):
            latitude = self._get_valid_map_coordinate(
                attributes.get("Latitude"),
                -90.0,
                90.0,
            )
            longitude = self._get_valid_map_coordinate(
                attributes.get("Longitude"),
                -180.0,
                180.0,
            )

            if latitude is not None and longitude is not None:
                coordinates_by_dam[dam_id] = (latitude, longitude)

        if not coordinates_by_dam:
            messagebox.showerror(
                APP_NAME,
                f"System {system_id} has no valid dam coordinates available for mapping.",
            )
            return

        root_dams = {
            dam_id
            for dam_id in cascade_graph.nodes
            if cascade_graph.in_degree(dam_id) == 0
        }

        terminal_dams = {
            dam_id
            for dam_id in cascade_graph.nodes
            if cascade_graph.out_degree(dam_id) == 0
        }

        latitude_values = [
            latitude
            for latitude, _ in coordinates_by_dam.values()
        ]
        longitude_values = [
            longitude
            for _, longitude in coordinates_by_dam.values()
        ]

        map_center = [
            sum(latitude_values) / len(latitude_values),
            sum(longitude_values) / len(longitude_values),
        ]

        cascade_map = folium.Map(
            location=map_center,
            zoom_start=8,
            tiles="CartoDB positron",
            control_scale=True,
        )

        # Add visually distinct dam markers by graph role.
        for dam_id, attributes in cascade_graph.nodes(data=True):
            coordinate = coordinates_by_dam.get(dam_id)

            if coordinate is None:
                continue

            if dam_id in root_dams:
                marker_color = "green"
                role_label = "Root dam"
            elif dam_id in terminal_dams:
                marker_color = "red"
                role_label = "Terminal dam"
            elif bool(attributes.get("is_hydroelectric", False)):
                marker_color = "blue"
                role_label = "Intermediate hydroelectric dam"
            else:
                marker_color = "gray"
                role_label = "Intermediate non-hydroelectric dam"

            dam_name = self._display_value(attributes.get("name"))
            owner = self._display_value(attributes.get("owner"))
            purposes = self._display_value(attributes.get("purposes"))
            hydroelectric_text = (
                "Hydroelectric"
                if bool(attributes.get("is_hydroelectric", False))
                else "Non-hydroelectric"
            )

            # Escape all data-source values before HTML interpolation. NID
            # text is external data and must never be trusted as safe markup.
            popup_html = (
                "<div style='min-width:260px;'>"
                f"<strong>{html.escape(dam_name)}</strong><br>"
                f"<strong>NID ID:</strong> {html.escape(str(dam_id))}<br>"
                f"<strong>Role:</strong> {html.escape(role_label)}<br>"
                f"<strong>Type:</strong> {html.escape(hydroelectric_text)}<br>"
                f"<strong>Owner:</strong> {html.escape(owner)}<br>"
                f"<strong>Purposes:</strong> {html.escape(purposes)}"
                "</div>"
            )

            folium.Marker(
                location=coordinate,
                popup=folium.Popup(
                    popup_html,
                    max_width=350,
                ),
                tooltip=(
                    f"{dam_name} "
                    f"({hydroelectric_text}; {role_label})"
                ),
                icon=folium.Icon(
                    color=marker_color,
                    icon="tint",
                    prefix="fa",
                ),
            ).add_to(cascade_map)

        # Draw each known direct downstream edge. A straight geographic line is
        # a visualization aid only; the calculated river route distance remains
        # available in the edge tooltip and labels.
        skipped_edge_count = 0

        for upstream_dam_id, downstream_dam_id, edge_attributes in cascade_graph.edges(
            data=True
        ):
            upstream_coordinate = coordinates_by_dam.get(upstream_dam_id)
            downstream_coordinate = coordinates_by_dam.get(downstream_dam_id)

            if upstream_coordinate is None or downstream_coordinate is None:
                skipped_edge_count += 1
                continue

            distance = edge_attributes.get("distance_miles")

            try:
                distance_text = (
                    f"{float(distance):.2f} river miles"
                    if distance is not None
                    else "distance unavailable"
                )
            except (TypeError, ValueError):
                distance_text = "distance unavailable"

            upstream_name = self._display_value(
                cascade_graph.nodes[upstream_dam_id].get("name")
            )
            downstream_name = self._display_value(
                cascade_graph.nodes[downstream_dam_id].get("name")
            )

            edge_tooltip = (
                f"{upstream_name} → {downstream_name}: {distance_text}"
            )

            folium.PolyLine(
                locations=[
                    upstream_coordinate,
                    downstream_coordinate,
                ],
                color="#1f5aa6",
                weight=3,
                opacity=0.8,
                tooltip=edge_tooltip,
            ).add_to(cascade_map)

            # Draw one directional arrow at the midpoint of the direct edge.
            # The line is ordered upstream -> downstream, so the bearing is
            # calculated from upstream_coordinate to downstream_coordinate.
            midpoint = [
                (upstream_coordinate[0] + downstream_coordinate[0]) / 2,
                (upstream_coordinate[1] + downstream_coordinate[1]) / 2,
            ]

            flow_bearing = bearing_degrees(
                upstream_coordinate[0],
                upstream_coordinate[1],
                downstream_coordinate[0],
                downstream_coordinate[1],
            )

            # &#9650; is a triangle that points north/up by default. Rotating
            # by a compass bearing therefore makes it point toward the
            # downstream dam:
            #
            # north = 0°, east = 90°, south = 180°, west = 270°.
            arrow_html = (
                '<div style="width:20px; height:20px; display:flex; '
                'align-items:center; justify-content:center; '
                f'transform: rotate({flow_bearing}deg); '
                'transform-origin: center center;">'
                '<span style="font-size:16px; color:#1f5aa6;">&#9650;</span>'
                '</div>'
            )

            folium.Marker(
                location=midpoint,
                icon=folium.DivIcon(
                    html=arrow_html,
                    icon_size=(20, 20),
                    icon_anchor=(10, 10),
                ),
                tooltip=edge_tooltip,
            ).add_to(cascade_map)

        # Add a compact legend to explain the graph-role colors.
        legend_html = (
            "<div style='position: fixed; bottom: 28px; left: 28px; "
            "z-index: 9999; background-color: white; border: 1px solid gray; "
            "border-radius: 4px; padding: 8px; font-size: 12px;'>"
            "<strong>Cascade System Legend</strong><br>"
            "<span style='color:green;'>&#9679;</span> Root dam<br>"
            "<span style='color:red;'>&#9679;</span> Terminal dam<br>"
            "<span style='color:blue;'>&#9679;</span> Intermediate hydroelectric dam<br>"
            "<span style='color:gray;'>&#9679;</span> Intermediate non-hydroelectric dam"
            "</div>"
        )

        cascade_map.get_root().html.add_child(
            folium.Element(legend_html)
        )

        self.cascade_maps_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        if os.name != "nt":
            os.chmod(self.cascade_maps_directory, 0o700)

        # System IDs originate from normalized NID IDs. Replace unexpected
        # filename characters defensively before constructing the output path.
        safe_system_id = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            system_id,
        )

        output_file = (
            self.cascade_maps_directory
            / f"cascade_{safe_system_id}.html"
        )

        # Folium writes the full HTML document. The file is local application
        # output, not downloaded/rendered inside Tkinter.
        cascade_map.save(str(output_file))

        self.cascade_query_status_text.set(
            f"Saved and opened interactive map for system {system_id}."
        )

        self._append_log(
            f"Part 6 map saved for cascade system {system_id}: {output_file}"
        )

        if skipped_edge_count:
            self._append_log(
                f"Map note: {skipped_edge_count:,} edge(s) were not drawn "
                "because one or both endpoint dams lacked valid coordinates."
            )

        try:
            # as_uri() creates a valid escaped file:// URL and avoids browser
            # issues with spaces or non-ASCII characters in the cache path.
            webbrowser.open_new_tab(output_file.resolve().as_uri())
        except Exception as error:
            # The map is still successfully saved even if the operating system
            # cannot launch a configured browser.
            self._append_log(
                f"Map saved, but the browser could not be opened automatically: {error}"
            )

            messagebox.showinfo(
                APP_NAME,
                "The interactive map was saved successfully, but could not be "
                "opened automatically.\n\n"
                f"Open this file manually:\n{output_file}",
            )

    def generate_query_results_conus_map(self) -> None:
        """
        Generate a single interactive CONUS overview map for the cascade
        systems returned by the latest Part 6 query.

        Each cascade system is represented as an independently toggleable
        Folium FeatureGroup. Flow lines use system-specific colors, while dam
        marker colors communicate a dam's role within its own cascade graph:

        - green: root dam;
        - red: terminal dam;
        - blue: intermediate hydroelectric dam;
        - gray: intermediate non-hydroelectric dam.

        The map uses straight geographic lines as a connectivity visualization;
        the Part 4 river-mile value remains the authoritative route distance.
        """

        # A query must run at least once so the map precisely reflects the
        # filters and matching systems currently presented to the researcher.
        if not self.last_cascade_query_system_ids:
            messagebox.showwarning(
                APP_NAME,
                "No cascade systems are in the current query result. "
                "Run a Part 6 query first, or use Clear Filters / Show All Systems.",
            )
            return

        # Retrieve only graphs that still exist. This defensive check handles
        # a future Part 5 rebuild that replaces the in-memory graph set after
        # a query list was previously generated.
        selected_graphs = {
            system_id: self.cascade_graphs[system_id]
            for system_id in self.last_cascade_query_system_ids
            if system_id in self.cascade_graphs
        }

        if not selected_graphs:
            messagebox.showwarning(
                APP_NAME,
                "The systems from the prior query are no longer available. "
                "Run the query again after rebuilding cascade systems.",
            )
            return

        # Validate coordinates once before determining map center or drawing
        # content. Invalid records do not prevent valid systems from mapping.
        coordinates_by_system: dict[
            str,
            dict[str, tuple[float, float]],
        ] = {}

        all_latitudes: list[float] = []
        all_longitudes: list[float] = []

        for system_id, cascade_graph in selected_graphs.items():
            system_coordinates: dict[str, tuple[float, float]] = {}

            for dam_id, attributes in cascade_graph.nodes(data=True):
                latitude = self._get_valid_map_coordinate(
                    attributes.get("Latitude"),
                    -90.0,
                    90.0,
                )
                longitude = self._get_valid_map_coordinate(
                    attributes.get("Longitude"),
                    -180.0,
                    180.0,
                )

                if latitude is None or longitude is None:
                    continue

                system_coordinates[str(dam_id)] = (
                    latitude,
                    longitude,
                )
                all_latitudes.append(latitude)
                all_longitudes.append(longitude)

            coordinates_by_system[system_id] = system_coordinates

        if not all_latitudes or not all_longitudes:
            messagebox.showerror(
                APP_NAME,
                "No valid dam coordinates were available in the query results.",
            )
            return

        # Preserve the original CONUS-map centering logic.
        map_center = [
            (min(all_latitudes) + max(all_latitudes)) / 2,
            (min(all_longitudes) + max(all_longitudes)) / 2,
        ]

        cascade_map = folium.Map(
            location=map_center,
            zoom_start=5,
            tiles="CartoDB positron",
            control_scale=True,
        )

        line_color_cycle = itertools.cycle(CASCADE_LINE_COLORS)
        skipped_edge_count = 0
        skipped_node_count = 0

        for system_id in sorted(selected_graphs):
            cascade_graph = selected_graphs[system_id]
            line_color = next(line_color_cycle)
            coordinates = coordinates_by_system[system_id]

            root_dams = {
                str(dam_id)
                for dam_id in cascade_graph.nodes
                if cascade_graph.in_degree(dam_id) == 0
            }
            terminal_dams = {
                str(dam_id)
                for dam_id in cascade_graph.nodes
                if cascade_graph.out_degree(dam_id) == 0
            }

            root_names = ", ".join(
                self._display_value(
                    cascade_graph.nodes[dam_id].get("name")
                )
                for dam_id in sorted(root_dams)
                if dam_id in cascade_graph.nodes
            )

            # Each cascade is a separate toggleable map layer.
            feature_group = folium.FeatureGroup(
                name=f"{root_names} ({system_id})",
                show=True,
            )

            # --------------------------------------------------------------
            # Dam markers
            # --------------------------------------------------------------
            for dam_id, attributes in cascade_graph.nodes(data=True):
                normalized_dam_id = str(dam_id)
                coordinate = coordinates.get(normalized_dam_id)

                if coordinate is None:
                    skipped_node_count += 1
                    continue

                if normalized_dam_id in root_dams:
                    marker_color = "green"
                    role_label = "Root dam"
                elif normalized_dam_id in terminal_dams:
                    marker_color = "red"
                    role_label = "Terminal dam"
                elif bool(attributes.get("is_hydroelectric", False)):
                    marker_color = "blue"
                    role_label = "Intermediate hydroelectric dam"
                else:
                    marker_color = "gray"
                    role_label = "Intermediate non-hydroelectric dam"

                dam_name = self._display_value(attributes.get("name"))
                dam_owner = self._display_value(attributes.get("owner"))
                dam_purposes = self._display_value(
                    attributes.get("purposes")
                )
                hydroelectric_label = (
                    "Hydroelectric"
                    if bool(attributes.get("is_hydroelectric", False))
                    else "Non-hydroelectric"
                )

                # Escape every externally sourced NID field before inserting it
                # into popup HTML. This prevents stored dam text from becoming
                # executable HTML/JavaScript in the exported Folium page.
                popup_html = (
                    "<div style='min-width:260px;'>"
                    f"<strong>{html.escape(dam_name)}</strong><br>"
                    f"<strong>NID ID:</strong> "
                    f"{html.escape(normalized_dam_id)}<br>"
                    f"<strong>System:</strong> "
                    f"{html.escape(system_id)}<br>"
                    f"<strong>Role:</strong> "
                    f"{html.escape(role_label)}<br>"
                    f"<strong>Type:</strong> "
                    f"{html.escape(hydroelectric_label)}<br>"
                    f"<strong>Owner:</strong> "
                    f"{html.escape(dam_owner)}<br>"
                    f"<strong>Purposes:</strong> "
                    f"{html.escape(dam_purposes)}"
                    "</div>"
                )

                # CircleMarker is intentionally lightweight for national-scale
                # maps that may contain many cascade systems and dam markers.
                folium.CircleMarker(
                    location=coordinate,
                    radius=5,
                    color=marker_color,
                    fill=True,
                    fill_color=marker_color,
                    fill_opacity=0.9,
                    weight=1,
                    popup=folium.Popup(
                        popup_html,
                        max_width=350,
                    ),
                    tooltip=(
                        f"{dam_name} "
                        f"({hydroelectric_label}; {role_label})"
                    ),
                ).add_to(feature_group)

            # --------------------------------------------------------------
            # Direct downstream edges and midpoint directional arrows
            # --------------------------------------------------------------
            for upstream_dam_id, downstream_dam_id, edge_attributes in (
                cascade_graph.edges(data=True)
            ):
                upstream_id = str(upstream_dam_id)
                downstream_id = str(downstream_dam_id)

                upstream_coordinate = coordinates.get(upstream_id)
                downstream_coordinate = coordinates.get(downstream_id)

                if (
                    upstream_coordinate is None
                    or downstream_coordinate is None
                ):
                    skipped_edge_count += 1
                    continue

                raw_distance = edge_attributes.get("distance_miles")

                try:
                    distance_label = (
                        f"{float(raw_distance):.2f} river miles"
                        if raw_distance is not None
                        else "distance unavailable"
                    )
                except (TypeError, ValueError):
                    distance_label = "distance unavailable"

                upstream_name = self._display_value(
                    cascade_graph.nodes[upstream_dam_id].get("name")
                )
                downstream_name = self._display_value(
                    cascade_graph.nodes[downstream_dam_id].get("name")
                )

                edge_tooltip = (
                    f"{upstream_name} → {downstream_name}: {distance_label}"
                )

                folium.PolyLine(
                    locations=[
                        upstream_coordinate,
                        downstream_coordinate,
                    ],
                    color=line_color,
                    weight=2,
                    opacity=0.7,
                    tooltip=edge_tooltip,
                ).add_to(feature_group)

                # Calculate the geographic midpoint and rotate an upward
                # triangle according to the upstream-to-downstream bearing.
                # This is the same valid arrow implementation used in the
                # corrected selected-system map.
                midpoint = [
                    (
                        upstream_coordinate[0]
                        + downstream_coordinate[0]
                    ) / 2,
                    (
                        upstream_coordinate[1]
                        + downstream_coordinate[1]
                    ) / 2,
                ]

                flow_bearing = bearing_degrees(
                    upstream_coordinate[0],
                    upstream_coordinate[1],
                    downstream_coordinate[0],
                    downstream_coordinate[1],
                )

                arrow_html = (
                    '<div style="width:20px; height:20px; display:flex; '
                    'align-items:center; justify-content:center; '
                    f'transform: rotate({flow_bearing}deg); '
                    'transform-origin: center center;">'
                    f'<span style="font-size:16px; color:{line_color};">'
                    '&#9650;</span>'
                    '</div>'
                )

                folium.Marker(
                    location=midpoint,
                    icon=folium.DivIcon(
                        html=arrow_html,
                        icon_size=(20, 20),
                        icon_anchor=(10, 10),
                    ),
                    tooltip=edge_tooltip,
                ).add_to(feature_group)

                if self.show_overview_distance_labels_var.get():
                    folium.Marker(
                        location=midpoint,
                        icon=folium.DivIcon(
                            html=(
                                '<div style="font-size:9pt; color:black; '
                                "background-color:white; padding:1px; "
                                "border:1px solid gray; border-radius:3px; "
                                f'margin-top:14px;">'
                                f"{html.escape(distance_label)}"
                                "</div>"
                            ),
                            icon_size=(0, 0),
                        ),
                    ).add_to(feature_group)

            feature_group.add_to(cascade_map)

        # Give the user a layer selector to isolate any cascade system.
        folium.LayerControl(collapsed=True).add_to(cascade_map)

        legend_html = (
            "<div style='position:fixed; bottom:28px; left:28px; "
            "z-index:9999; background-color:white; border:1px solid gray; "
            "border-radius:4px; padding:8px; font-size:12px;'>"
            "<strong>Cascade System Legend</strong><br>"
            "<span style='color:green;'>&#9679;</span> Root dam<br>"
            "<span style='color:red;'>&#9679;</span> Terminal dam<br>"
            "<span style='color:blue;'>&#9679;</span> "
            "Intermediate hydroelectric dam<br>"
            "<span style='color:gray;'>&#9679;</span> "
            "Intermediate non-hydroelectric dam"
            "</div>"
        )

        cascade_map.get_root().html.add_child(
            folium.Element(legend_html)
        )

        self.cascade_maps_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        if os.name != "nt":
            os.chmod(self.cascade_maps_directory, 0o700)

        output_file = (
            self.cascade_maps_directory
            / CONUS_CASCADE_MAP_FILENAME
        )

        # This is trusted, locally generated output. The source values within
        # popups/labels are escaped above before they enter the Folium document.
        cascade_map.save(str(output_file))

        self.cascade_query_status_text.set(
            f"Saved CONUS overview map for {len(selected_graphs):,} "
            "query-matching cascade system(s)."
        )

        self._append_log(
            "Part 6 CONUS overview map saved for "
            f"{len(selected_graphs):,} query-matching system(s): {output_file}"
        )

        if skipped_node_count:
            self._append_log(
                f"CONUS map note: {skipped_node_count:,} dam marker(s) were "
                "not drawn because coordinates were unavailable or invalid."
            )

        if skipped_edge_count:
            self._append_log(
                f"CONUS map note: {skipped_edge_count:,} edge(s) were not "
                "drawn because one or both endpoint coordinates were unavailable."
            )

        try:
            # Use a valid file URI so paths with spaces or non-ASCII characters
            # open reliably in the system-configured browser.
            webbrowser.open_new_tab(output_file.resolve().as_uri())
        except Exception as error:
            self._append_log(
                "CONUS map was saved, but could not be opened automatically: "
                f"{error}"
            )

            messagebox.showinfo(
                APP_NAME,
                "The CONUS overview map was saved successfully, but could "
                "not be opened automatically.\n\n"
                f"Open this file manually:\n{output_file}",
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

        self.initialize_button.configure(state=tk.DISABLED)
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
        Execute slow data operations without blocking Tkinter.

        Worker code must never modify Tk widgets directly. It sends structured
        messages to the main thread through ui_message_queue instead.
        """

        def worker_log(message: str) -> None:
            self.ui_message_queue.put(("log", message))
            self.ui_message_queue.put(("progress", message))

        try:
            service = InitializationService(self.cache_dir, worker_log)
            result = service.initialize()
            self.ui_message_queue.put(("success", result))
        except Exception as error:
            # The traceback is intentionally not shown in the standard UI
            # message. Production releases should also write it to a protected
            # local diagnostic log with appropriate privacy controls.
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
                    self.initialization_result = payload

                    # Populate the Part 1 statistics display.
                    self._display_statistics(payload.statistics)

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
                    self.initialize_button.configure(state=tk.NORMAL)

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
                    self.downstream_links = payload["dataframe"]
                    self.downstream_links_csv_path = payload["output_file"]

                    # Downstream links are now available. Part 5 additionally
                    # checks that Part 3 filtering has been run before it
                    # permits cascade construction.
                    self.notebook.tab(
                        self.cascade_builder_tab,
                        state="normal",
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
                        str(self.downstream_links_csv_path)
                    )

                    self.downstream_search_status_text.set(
                        f"Complete: {linked_dams:,} downstream links found."
                    )

                    self.run_downstream_search_button.configure(
                        state=tk.NORMAL
                    )

                    self._display_downstream_search_results(
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
                                str(self.downstream_links_csv_path),
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
                        f"Saved reference CSV: {self.downstream_links_csv_path}"
                    )
                    self._append_log(
                        "The downstream-link reference data are now ready for "
                        "Part 5 cascade construction."
                    )
                    self._append_log("")

                elif message_type == "downstream_failure":
                    self.run_downstream_search_button.configure(
                        state=tk.NORMAL
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
                    self.cascade_graphs = payload["cascade_graphs"]
                    self.cascade_systems_edges = payload["edge_dataframe"]
                    self.cascade_systems_summary = payload["summary_dataframe"]
                    self.cascade_systems_csv_path = payload["edge_csv_path"]
                    self.cascade_systems_summary_csv_path = payload[
                        "summary_csv_path"
                    ]

                    # Part 6 uses the in-memory graphs produced by Part 5.
                    # It remains available even when the current criteria
                    # produce zero systems, so it can report that condition.
                    self.notebook.tab(
                        self.cascade_query_tab,
                        state="normal",
                    )

                    self.refresh_cascade_system_list()
                    
                    self.cascade_systems_csv_path_text.set(
                        str(self.cascade_systems_csv_path)
                    )
                    self.cascade_summary_csv_path_text.set(
                        str(self.cascade_systems_summary_csv_path)
                    )

                    self.construct_cascades_button.configure(state=tk.NORMAL)

                    system_count = payload["system_count"]
                    edge_count = payload["edge_count"]

                    self.cascade_construction_status_text.set(
                        f"Complete: {system_count:,} cascade system(s) constructed."
                    )

                    self._display_cascade_results(
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
                                "Qualifying initial cascade chains",
                                f"{payload['qualifying_chain_count']:,}",
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
                        f"System edge-list CSV: {self.cascade_systems_csv_path}"
                    )
                    self._append_log(
                        f"System summary CSV: "
                        f"{self.cascade_systems_summary_csv_path}"
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
                    self.construct_cascades_button.configure(state=tk.NORMAL)
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
                    self.initialize_button.configure(state=tk.NORMAL)
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

    def _display_statistics(self, stats: MappingStatistics) -> None:
        """Populate the statistics table after a successful mapping run."""

        for item_id in self.stats_tree.get_children():
            self.stats_tree.delete(item_id)

        metric_rows = [
            ("NHD river-network nodes", f"{stats.graph_nodes:,}"),
            ("NHD river-network edges", f"{stats.graph_edges:,}"),
            ("NID dams after required-field and primary-dam filters",
             f"{stats.total_nid_dams_after_basic_filter:,}"),
            ("GeoConnex provider ID → COMID mappings", f"{stats.geo_connex_comid_mappings:,}"),
            ("ResNet NID ID → COMID mappings", f"{stats.resnet_comid_mappings:,}"),
            ("Dams matched to NHD through GeoConnex", f"{stats.dams_matched_by_geoconnex:,}"),
            ("Additional dams recovered through ResNet", f"{stats.dams_recovered_by_resnet:,}"),
            ("Total NID dams matched to an NHD node",
             f"{stats.total_dams_matched_to_network:,}"),
            ("Unmatched NID dams", f"{stats.unmatched_dams:,}"),
            ("Final NID-to-network match rate", f"{stats.match_rate_percent:.1f}%"),
        ]

        for metric, value in metric_rows:
            self.stats_tree.insert("", tk.END, values=(metric, value))

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

def main() -> None:
    """Start the application."""

    application = CascadeResearchApp()
    application.mainloop()

if __name__ == "__main__":
    main()