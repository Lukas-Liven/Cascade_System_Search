"""
Part 1 initialization service.

This module acquires, validates, caches, and maps the NHD, GeoConnex, ResNet,
and National Inventory of Dams datasets. It contains no Tkinter widgets,
dialogs, worker queues, or browser behavior.

The caller supplies a neutral log callback so the Tkinter worker can relay
progress messages safely to the user-interface thread.
"""

from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import Any, Callable

import geopandas as gpd
import pandas as pd
import pygeohydro as gh
import pynhd
from pynhd import GeoConnex

from cascade_research_tool.constants import (
    CONUS_BBOX,
    GEOCONNEX_CACHE_NAME,
    NHD_GRAPH_CACHE_NAME,
    NID_INVENTORY_CACHE_NAME,
    RESNET_CACHE_NAME,
)
from cascade_research_tool.exceptions import DataValidationError
from cascade_research_tool.models import (
    InitializationResult,
    MappingStatistics,
)
from cascade_research_tool.services.cache_service import (
    load_application_artifact,
    save_application_artifact,
    write_cache_metadata,
)
from cascade_research_tool.services.resnet_service import (
    calculate_file_md5,
    download_resnet_with_validation,
    get_resnet_expected_md5,
)
from cascade_research_tool.utilities.identifiers import (
    normalize_identifier,
    parse_comid,
)
from cascade_research_tool.utilities.security import (
    atomic_write_bytes,
    ensure_pickle_cache_is_private,
)

class InitializationService:
    """Owns initial dataset acquisition, caching, validation, and COMID mapping."""

    def __init__(self, cache_dir: Path, log: Callable[[str], None]) -> None:
        self.cache_dir = cache_dir
        self.log = log

        self.nhd_cache = self.cache_dir / NHD_GRAPH_CACHE_NAME
        self.geoconnex_cache = self.cache_dir / GEOCONNEX_CACHE_NAME
        self.resnet_cache = self.cache_dir / RESNET_CACHE_NAME
        # Raw NID inventory saved after a successful live pygeohydro retrieval.
        # If the upstream NID service is unavailable later, this is the
        # application-controlled fallback used to continue initialization.
        self.nid_inventory_cache = (
            self.cache_dir / NID_INVENTORY_CACHE_NAME
        )

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
        """
        Load a checksum-verified ResNet crosswalk.

        Every existing cache file is compared to Zenodo's currently published
        MD5 checksum before use. A mismatched cache is rejected, deleted, and
        replaced only through the HTTPS download-and-validation workflow.
        """

        expected_md5 = get_resnet_expected_md5(self.log)

        if self.resnet_cache.exists():
            self.log("Verifying cached ResNet crosswalk checksum...")

            try:
                actual_md5 = calculate_file_md5(self.resnet_cache)
            except OSError as error:
                raise DataValidationError(
                    "Could not read the cached ResNet crosswalk for checksum "
                    f"verification: {error}"
                ) from error

            if actual_md5 == expected_md5:
                self.log(
                    "Cached ResNet checksum matches the Zenodo-published "
                    "checksum."
                )
            else:
                self.log(
                    "Cached ResNet checksum does not match the "
                    "Zenodo-published checksum. The cache will be replaced."
                )

                try:
                    self.resnet_cache.unlink()
                except OSError as error:
                    raise DataValidationError(
                        "The invalid cached ResNet file could not be removed: "
                        f"{error}"
                    ) from error

                download_resnet_with_validation(
                    self.resnet_cache,
                    self.log,
                )
        else:
            self.log(
                "No cached ResNet crosswalk was found. Downloading a "
                "checksum-validated copy..."
            )

            download_resnet_with_validation(
                self.resnet_cache,
                self.log,
            )

        # Use strings first. COMIDs are parsed explicitly afterward, avoiding
        # loss of identifier values due to pandas numeric type inference.
        resnet_df = pd.read_csv(
            self.resnet_cache,
            dtype=str,
        )

        required_columns = {"NID", "COMID"}
        missing_columns = required_columns - set(resnet_df.columns)

        if missing_columns:
            raise DataValidationError(
                "ResNet file is missing required columns: "
                f"{sorted(missing_columns)}"
            )

        self.log(f"ResNet records ready: {len(resnet_df):,} rows.")

        return resnet_df

    def load_nid_inventory(self) -> pd.DataFrame:
        """
        Acquire the raw National Inventory of Dams and apply baseline filters.

        Normal behavior:
        1. Request current NID data using pygeohydro.
        2. Save the successful raw DataFrame to the application-managed cache.
        3. Apply application filtering and return the resulting inventory.

        Resiliency behavior:
        - If pygeohydro cannot reach the NID service, such as during DNS,
          HTTPS, server, or temporary network failure, load the most recently
          successful application-managed NID cache instead.
        - The cached raw inventory is subjected to the same field validation
          and filtering as a live response.

        Security:
        - The fallback artifact is loaded only from this application's private
          cache directory through load_application_artifact().
        - Do not expose a UI feature that lets users select arbitrary pickle
          files, because pickle is unsafe for untrusted input.
        """

        required_columns = {
            # Primary-dam identification and NID-to-NHD matching.
            "NID ID",
            "Federal ID",

            # Part 3 storage threshold and Part 6 map coordinates.
            "Max Storage (Acre-Ft)",
            "Longitude",
            "Latitude",

            # Part 3 default hydroelectric-purpose filter.
            "Purposes",

            # Part 3 estimated power calculation:
            # (Hydraulic Height × Max Discharge) / POWER_CONVERSION_FACTOR.
            "Hydraulic Height (Ft)",
            "Max Discharge (Cubic Ft/Second)",
        }

        raw_nid_inventory: pd.DataFrame
        source_description: str

        try:
            self.log(
                "Loading National Inventory of Dams through pygeohydro..."
            )

            # pygeohydro may use its own internal cache, but the call can still
            # attempt remote access. A DNS failure here is handled by the
            # application-owned fallback cache below.
            raw_nid_inventory = gh.NID().df.copy()

            if not isinstance(raw_nid_inventory, pd.DataFrame):
                raise DataValidationError(
                    "pygeohydro returned an invalid NID inventory structure."
                )

            missing_columns = required_columns - set(
                raw_nid_inventory.columns
            )

            if missing_columns:
                raise DataValidationError(
                    "Live NID data are missing required columns: "
                    f"{sorted(missing_columns)}"
                )

            # Persist the unfiltered source data. Current and future filtering
            # behavior therefore remains consistent whether data came from a
            # live source or a prior successful cache.
            save_application_artifact(
                artifact_file=self.nid_inventory_cache,
                artifact_type="nid_inventory_raw",
                payload={
                    "nid_inventory": raw_nid_inventory,
                },
            )

            source_description = "live pygeohydro/NID data"

            self.log(
                "NID data loaded successfully and saved to the "
                "application-managed fallback cache."
            )

        except Exception as live_error:
            # Keep the original reason in the log. This makes it clear that
            # initialization continued from cached data rather than silently
            # claiming the NID service was reached.
            self.log(
                "Unable to retrieve live NID data; attempting the "
                f"application-managed NID cache. Reason: {live_error}"
            )

            try:
                cached_payload = load_application_artifact(
                    artifact_file=self.nid_inventory_cache,
                    expected_artifact_type="nid_inventory_raw",
                )

                cached_inventory = cached_payload.get("nid_inventory")

                if not isinstance(cached_inventory, pd.DataFrame):
                    raise DataValidationError(
                        "The cached NID artifact does not contain a valid DataFrame."
                    )

                missing_columns = required_columns - set(
                    cached_inventory.columns
                )

                if missing_columns:
                    raise DataValidationError(
                        "Cached NID data are missing required columns: "
                        f"{sorted(missing_columns)}"
                    )

                # Make a fresh in-memory copy. This ensures the normalization
                # and filtering below never mutate the cached artifact object.
                raw_nid_inventory = cached_inventory.copy()
                source_description = "application-managed cached NID data"

                self.log(
                    "Live NID retrieval was unavailable. Continuing with the "
                    "most recently cached NID inventory."
                )

            except Exception as cache_error:
                raise DataValidationError(
                    "Could not retrieve live NID data and no valid "
                    "application-managed NID cache was available. "
                    f"Live retrieval error: {live_error}. "
                    f"Cache recovery error: {cache_error}."
                ) from cache_error

        # Work from the validated live-or-cache raw dataset. Everything below
        # deliberately applies identically for both acquisition paths.
        dam_inventory = raw_nid_inventory.copy()

        # Normalize identifiers before comparing them. This retains only
        # primary dam records, excluding associated structures:
        #
        #     NID ID == Federal ID
        #
        dam_inventory["NID ID"] = dam_inventory["NID ID"].map(
            normalize_identifier
        )
        dam_inventory["Federal ID"] = dam_inventory["Federal ID"].map(
            normalize_identifier
        )

        dam_inventory["Max Storage (Acre-Ft)"] = pd.to_numeric(
            dam_inventory["Max Storage (Acre-Ft)"],
            errors="coerce",
        )

        # A valid NID identifier, coordinates, and storage value are required
        # for later mapping, filtering, and visualization. User-controlled
        # minimum storage filtering remains in Part 3.
        dam_inventory = dam_inventory.dropna(
            subset=[
                "NID ID",
                "Max Storage (Acre-Ft)",
                "Longitude",
                "Latitude",
            ]
        )

        # Automatic baseline condition from the original cascade algorithm:
        # preserve the main/primary NID record and remove associated structures.
        dam_inventory = dam_inventory[
            dam_inventory["NID ID"] == dam_inventory["Federal ID"]
        ].copy()

        # Stable unique NID IDs are necessary for downstream maps, cascade
        # graphs, and Part 6 dam-ID searches.
        dam_inventory = dam_inventory.drop_duplicates(
            subset=["NID ID"],
            keep="first",
        )

        self.log(
            "NID inventory ready from "
            f"{source_description} after required-field and primary-dam "
            f"filters: {len(dam_inventory):,} dams."
        )

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