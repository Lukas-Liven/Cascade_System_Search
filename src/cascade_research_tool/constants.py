"""
Centralized application constants.

This module must contain values only: no network calls, file-system writes,
Tkinter initialization, imports of HyRiver libraries, or executable startup
logic. Centralizing constants makes configuration changes auditable and avoids
duplicate literal values across services and UI modules.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Application and cache-schema identity
# ---------------------------------------------------------------------------

APP_NAME = "Cascade Research Tool"
CACHE_SCHEMA_VERSION = 1
WORKFLOW_ARTIFACT_SCHEMA_VERSION = 1
CASE_STUDY_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# External-source identifiers and geographic scope
# ---------------------------------------------------------------------------

RESNET_RECORD_ID = "15644268"
RESNET_FILENAME = "ResNet.csv"
ZENODO_RECORD_API = f"https://zenodo.org/api/records/{RESNET_RECORD_ID}"

# Continental United States bounding box:
# (minimum longitude, minimum latitude, maximum longitude, maximum latitude).
CONUS_BBOX = (-125.0, 25.0, -65.0, 50.0)


# ---------------------------------------------------------------------------
# Application calculations and download limits
# ---------------------------------------------------------------------------

POWER_CONVERSION_FACTOR = 11800
MAX_RESNET_DOWNLOAD_BYTES = 1_000_000_000


# ---------------------------------------------------------------------------
# Application-managed cache artifact names
# ---------------------------------------------------------------------------

GEOCONNEX_CACHE_NAME = "geoconnex_conus_dams.geojson"
NHD_GRAPH_CACHE_NAME = "nhd_enhd_network.pkl"
NID_INVENTORY_CACHE_NAME = "nid_inventory_raw.pkl"
CACHE_METADATA_NAME = "cache_metadata.json"
RESNET_CACHE_NAME = RESNET_FILENAME

DOWNSTREAM_LINKS_CACHE_NAME = "downstream_dam_pairs.pkl"
CASCADE_SYSTEMS_CACHE_NAME = "cascading_systems.pkl"

DEFAULT_DOWNSTREAM_LINKS_EXPORT_NAME = "downstream_dam_pairs.csv"
DEFAULT_CASCADE_SYSTEMS_EXPORT_NAME = "cascading_systems.csv"
DEFAULT_CASCADE_SUMMARY_EXPORT_NAME = "cascading_systems_summary.csv"


# ---------------------------------------------------------------------------
# Named case-study persistence
# ---------------------------------------------------------------------------

CASE_STUDIES_DIRECTORY_NAME = "case_studies"
CASE_STUDY_FILE_SUFFIX = ".study.pkl"
CASE_STUDY_ARTIFACT_TYPE = "cascade_case_study"


# ---------------------------------------------------------------------------
# Part 4: downstream dam search settings
# ---------------------------------------------------------------------------

LENGTH_ATTR = "lengthkm"
KM_TO_MILES = 0.621371
DEFAULT_MAX_DISTANCE_MILES = 100.0


# ---------------------------------------------------------------------------
# Part 5: cascade construction settings
# ---------------------------------------------------------------------------

DEFAULT_MIN_HYDROELECTRIC_DAMS_PER_CASCADE = 2
DEFAULT_REQUIRE_SAME_OWNER = False


# ---------------------------------------------------------------------------
# Part 6: maps and presentation
# ---------------------------------------------------------------------------

CASCADE_MAPS_DIRECTORY_NAME = "cascade_maps"
CONUS_CASCADE_MAP_FILENAME = "conus_cascade_query_map.html"

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

# USGS National Map topographic tile service used for generated Folium maps.
# The service is better aligned with the application's CONUS research focus
# than public OSM tiles for local file://-opened map documents.
USGS_TOPO_TILE_URL = (
    "https://basemap.nationalmap.gov/arcgis/rest/services/"
    "USGSTopo/MapServer/tile/{z}/{y}/{x}"
)

USGS_TOPO_ATTRIBUTION = (
    "Tiles courtesy of the U.S. Geological Survey"
)