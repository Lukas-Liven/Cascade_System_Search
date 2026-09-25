Cascade Research Tool
This repository contains a modular desktop research application for identifying, constructing, querying, and visualizing cascading dam systems across the Continental United States.

The application uses the enhanced National Hydrography Dataset river network, National Inventory of Dams records, GeoConnex dam mappings, and the ResNet crosswalk to associate dams with river-network nodes. It then supports downstream dam-link analysis, cascade-system construction, case-study persistence, CSV export, and interactive map generation.

Repository Structure
=
cascade-research-tool/
├── Script/
│   └── run_application.py          # Primary application launcher
│
├── src/
│   └── cascade_research_tool/
│       ├── app.py                  # Package-level application startup
│       ├── runtime.py              # aiohttp DNS resolver configuration
│       ├── constants.py            # Application constants and source settings
│       ├── exceptions.py           # Application-specific exception types
│       ├── models.py               # Shared data/result models
│       ├── state.py                # Current application session state
│       │
│       ├── services/
│       │   ├── initialization_service.py      # NHD, NID, GeoConnex, ResNet initialization
│       │   ├── resnet_service.py              # Zenodo download and checksum validation
│       │   ├── downstream_service.py          # River-network traversal helpers
│       │   ├── node_lookup_service.py         # Part 2 dam/node lookups
│       │   ├── filter_service.py              # Part 3 dam filtering
│       │   ├── downstream_link_service.py     # Part 4 downstream-link construction
│       │   ├── cascade_service.py             # Part 5 cascade construction
│       │   ├── cascade_query_service.py       # Part 6 query logic
│       │   ├── map_service.py                 # Folium/USGS map generation
│       │   ├── cache_service.py               # Controlled artifact persistence
│       │   └── case_study_service.py          # Named case-study persistence
│       │
│       ├── ui/
│       │   ├── main_window.py                 # Main Tkinter application controller
│       │   ├── initialization_tab.py          # Part 1 interface
│       │   ├── node_explorer_tab.py           # Part 2 interface
│       │   ├── filter_tab.py                  # Part 3 interface
│       │   ├── downstream_tab.py              # Part 4 interface
│       │   ├── cascade_builder_tab.py         # Part 5 interface
│       │   ├── cascade_query_tab.py           # Part 6 interface
│       │   └── case_study_controls.py         # Case-study controls
│       │
│       └── utilities/
│           ├── identifiers.py                 # NID ID, COMID, and study-name handling
│           ├── security.py                    # Atomic writes and pickle-cache safeguards
│           ├── csv_export.py                  # Spreadsheet-safe CSV exports
│           ├── map_helpers.py                 # Coordinate and map display helpers
│           └── validation.py                  # Numeric input validation
│
├── pyproject.toml                  # Project metadata and direct dependencies
├── uv.lock                         # Locked dependency resolution
├── requirements.txt                # Generated pip-compatible requirements
├── .gitignore
└── README.md

Setup and Installation
This project uses uv for dependency and environment management.
Clone the repository and navigate to its root directory.

git clone https://github.com/Lukas-Liven/Cascade_System_Search
cd cascade-research-tool

Create the local virtual environment and install the locked dependencies.
uv sync --locked

Python Requirement
The project currently declares Python 3.10 or newer. Use the Python-version range documented in pyproject.toml.

Execution
Launch the application from the repository root.

uv run python Script/run_application.py

OR For an activated pip-managed virtual environment, run:

python Script/run_application.py

First-Time Use
The first initialization may take several minutes because the application retrieves or builds the required national datasets and creates a private user-local cache.

Application Workflow
Part 1 — Initialize Data
Downloads or loads cached NHD, GeoConnex, ResNet, and NID data, then maps NID dams to NHD network nodes.

Part 2 — Explore Nodes
Search by:

NHD network node ID, to list matched dam records at that node;
NID ID, to identify a dam’s mapped NHD node and purposes.
Results are written to the shared application log.

Part 3 — Filter Dams
Filter primary dam records using:

minimum maximum storage;
optional hydroelectric-purpose selection;
optional estimated power-capacity threshold.
The estimated power calculation is:

$$ \text{Estimated Power Capacity} = 
\frac{ \text{Hydraulic Height} \times \text{Maximum Discharge} }{ 11800 } $$

The automatic baseline rule retains primary NID dam records where:
NID ID = Federal ID
Part 4 — Search Downstream Dams
Searches downstream from every matched primary dam and records the nearest reachable downstream dam within a user-selected river-distance limit.

The resulting direct downstream-dam reference table is stored as an application-managed artifact and may optionally be exported as CSV.

Part 5 — Construct Cascading Systems
Builds directed cascade systems from:

Part 3 selected and matched candidate dams;
Part 4 direct downstream dam relationships.
Users can configure:

minimum hydroelectric dams per final cascade system;
optional same-owner continuity from each root dam.
Converging dam chains are merged into connected systems before the minimum hydroelectric-dam requirement is evaluated.

Part 6 — Query and Visualize Cascades
Query constructed systems by:

state;
exact NID ID;
partial river or stream name.
Query criteria use system-level AND logic. The application can:

print selected system details to the shared log;
generate an interactive map for one selected cascade;
generate a CONUS map of the latest query results;
show optional river-distance labels on overview maps.
Generated interactive maps use the USGS National Map USGS Topo basemap service.

Case Studies
The application supports named case studies for saving researcher-specific workflow output.

A case study can preserve:

Part 3 filter choices and filtered inventories;
Part 4 downstream links;
Part 5 cascade graphs, edge lists, summaries, and settings;
Part 6 query settings and latest query-result system IDs.
Case studies do not duplicate the full NHD graph or baseline NID inventory. Those common datasets are restored through Part 1 initialization.

If a requested study name already exists after filename normalization, the application asks whether the existing study should be overwritten.

Local Data and Cache Behavior
The application stores downloaded data and generated artifacts in a user-local cache directory rather than inside the repository. Follow the file locations displayed in the application to locate files for use outside of runtime.
