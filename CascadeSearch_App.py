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

import geopandas as gpd
import pynhd
from pynhd import GeoConnex
import pygeohydro as gh


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

        self._build_placeholder_tab(
            self.cascade_builder_tab,
            "Part 5 — Construct Cascades",
            "Cascade construction and map-export controls will be implemented later.",
        )
        self._build_placeholder_tab(
            self.cascade_query_tab,
            "Part 6 — Query Cascades",
            "Cascade inspection, reports, and visualization tools will be implemented later.",
        )

        # Build the currently implemented tabs.
        self._build_initialization_tab()
        self._build_node_explorer_tab()
        self._build_filter_tab()
        self._build_downstream_search_tab()

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


    def _build_placeholder_tab(
        self,
        tab: ttk.Frame,
        title: str,
        description: str,
    ) -> None:
        """
        Add a consistent placeholder to an application stage not yet built.

        Keeping placeholders avoids creating a misleading blank tab and makes
        the staged development plan visible to the user.
        """

        ttk.Label(
            tab,
            text=title,
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            tab,
            text=description,
            wraplength=950,
        ).pack(anchor=tk.W, pady=(8, 0))

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