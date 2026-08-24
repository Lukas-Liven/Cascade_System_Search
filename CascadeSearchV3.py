import pandas as pd
import os
import requests
import pickle
import time
import itertools
import math

import folium
from folium.plugins import PolyLineTextPath
from folium.plugins import AntPath
import pynhd
import networkx as nx
from pynhd import GeoConnex
import pygeohydro as gh
from StoragePlots import POWER_CONVERSION_FACTOR #, analyze_dam_storage, analyze_power_capacity


# --- Load River Network and Node Mapping ---
print("Loading US River Network data...")
if not os.path.exists('US_River_Network.pkl'): # First run download and cache the river network data. This is a large file and may take a few minutes to download.
    graph, node_to_comid, _ = pynhd.enhd_flowlines_nx()
    with open('US_River_Network.pkl', 'wb') as file:
        pickle.dump((graph, node_to_comid), file)
else:
    with open('US_River_Network.pkl', 'rb') as file: # Load the cached river network data.
        graph, node_to_comid = pickle.load(file)

print("Data loaded successfully!")
print(f"Graph has {graph.number_of_nodes()} nodes and {graph.number_of_edges()} edges")

nid = gh.NID() # NID data is downloaded and cached automatically by pygeohydro. The first run may take a few minutes to download the data.
dam_inventory = nid.df

# --- Filter dam inventory ---
dam_inventory = dam_inventory.dropna(subset=['Max Storage (Acre-Ft)', 'Longitude', 'Latitude']) # Remove dams with missing storage or coordinates
dam_inventory = dam_inventory[dam_inventory['NID ID'] == dam_inventory['Federal ID']] # This removes associated structures so the primary dam is kept and prioritized instead of being missed in favor of a component. 
dam_inventory = dam_inventory[dam_inventory['Max Storage (Acre-Ft)'] > 100] # Remove dams with storage less than 100 acre-feet, as they are unlikely to be significant for hydroelectric power generation and cascades.

df_hydroelectric = dam_inventory[dam_inventory['Purposes'].str.contains('Hydroelectric', case=False, na=False)] # Any dam that contains hydroelectric as one of its purposes

print(f"Total dams with coordinates: {len(dam_inventory)}")
print(f"Hydroelectric dams: {len(df_hydroelectric)}")

# --- Get dam data from GeoConnex ---
GEOCONNEX_CACHE_FILE = 'geoconnex_dams.pkl'
if not os.path.exists(GEOCONNEX_CACHE_FILE): # First run download and cache the GeoConnex dam data.
    print("Fetching dam data from GeoConnex...")
    global_geo_dams = GeoConnex("dams")
    us_geo_dams = global_geo_dams.bybox((-125, 25, -65, 50))
    print(f"Retrieved {len(us_geo_dams)} dams from GeoConnex")

    print(f"Saving to cache: {GEOCONNEX_CACHE_FILE}")
    with open(GEOCONNEX_CACHE_FILE, 'wb') as file:
        pickle.dump(us_geo_dams, file)
else:
    print(f"Loading GeoConnex dam data from cache: {GEOCONNEX_CACHE_FILE}")
    with open(GEOCONNEX_CACHE_FILE, 'rb') as file: # Load the cached GeoConnex dam data.
        us_geo_dams = pickle.load(file)
    print(f"Loaded {len(us_geo_dams)} dams from GeoConnex")

# --- Map NID ID to COMID ---
damid_to_comid = { # Map the NID ID to COMID. In GeoConnex, the provider id is the NID ID and the COMID is stored in the nhdpv2_comid field, which is a URL. The COMID is the last part of the URL after the last slash.
    dam['provider_id']: int(float(str(dam['nhdpv2_comid']).split('/')[-1]))
    if pd.notna(dam['nhdpv2_comid']) else None
    for k, dam in us_geo_dams.iterrows()
}
print(f"Created NID ID to COMID mapping for {len(damid_to_comid)} dams")

# --- Create reverse mapping from COMID to node ---
comid_to_node = {v: k for k, v in node_to_comid.items()}
print(f"Created COMID to node mapping for {len(comid_to_node)} nodes")

# --- Find nodes that contain dams ---
# This is a set of all nodes in the river network that have a dam from the dam inventory. It is used to speed up the search for downstream dams, since we only need to check nodes that have dams.
nodes_of_dams = set([
    comid_to_node[damid_to_comid[damid]]
    for damid in dam_inventory['NID ID']
    if (damid in damid_to_comid)
    and (damid_to_comid[damid] in comid_to_node)
])

print(f"\nResults:")
print(f"Total dams in dam_inventory: {len(dam_inventory)}")
print(f"Total number of nodes with matching dams: {len(nodes_of_dams)}")
print(f"Match rate: {len(nodes_of_dams)/len(dam_inventory)*100:.1f}%")

# --- Create a dictionary mapping NID ID to node ID ---
# This dictionary is used to quickly look up the node ID for a given dam's NID ID so that we can get from an ID to a point in the river network. 
# It only includes dams that have a valid mapping to a COMID and a corresponding node in the river network.
damid_to_node = {
    damid: comid_to_node[damid_to_comid[damid]]
    for damid in dam_inventory['NID ID']
    if (damid in damid_to_comid)
    and (damid_to_comid[damid] in comid_to_node)
}

dam_inventory['node_id'] = dam_inventory['NID ID'].map(damid_to_node) # Joins the dam inventory with the node ID for each dam, allowing us to quickly find the corresponding node in the river network for any given dam.

# --- Get dam data from ResNet ---
record_id = "15644268"
resnet_file = "ResNet.csv"

if not os.path.exists(resnet_file): # Download the ResNet crosswalk file from Zenodo if it doesn't already exist locally. This file is used to map NID IDs to COMIDs for dams that are not already matched in the dam inventory.
    url = f"https://zenodo.org/api/records/{record_id}/files/{resnet_file}/content"
    response = requests.get(url, stream=True, timeout=30)

    if response.status_code == 200:
        with open(resnet_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Successfully downloaded {resnet_file}")
    else:
        print(f"Failed to download. Status code: {response.status_code}")
else: # If the ResNet crosswalk file already exists locally, skip the download and notify the user.
    print(f"{resnet_file} already exists locally, skipping download.")

resnet_df = pd.read_csv('ResNet.csv')

resnet_map = resnet_df.dropna(subset=['COMID']).set_index('NID')['COMID'].astype(int).to_dict()

resnet_node_map = { # Map NID ID to node ID using the ResNet crosswalk. This is used to fill in missing node IDs for dams that were not matched in the initial dam inventory mapping.
    nid_id: comid_to_node[comid]
    for nid_id, comid in resnet_map.items()
    if comid in comid_to_node
}

still_missing_before = dam_inventory['node_id'].isna().sum()
dam_inventory['node_id'] = dam_inventory['node_id'].fillna(
    dam_inventory['NID ID'].map(resnet_node_map)
)
still_missing_after = dam_inventory['node_id'].isna().sum()

print(f"Dams recovered via ResNet crosswalk: {still_missing_before - still_missing_after}")
print(f"Updated match rate: "
        f"{(len(dam_inventory) - still_missing_after) / len(dam_inventory) * 100:.1f}%")

# --- Filtering to dams with nodes ---
dam_inventory_matched = dam_inventory.dropna(subset=['node_id']).copy()
dam_inventory_matched['node_id'] = dam_inventory_matched['node_id'].astype(int)
dam_inventory_matched = dam_inventory_matched.drop_duplicates(subset='NID ID', keep='first')

hydroelectric_dams_matched = dam_inventory_matched[dam_inventory_matched['Purposes'].str.contains('Hydroelectric', case=False, na=False)]

# We're looking for hydroelectric dams that surpass a certain power capacity threshold, which is calculated as Hydraulic Height (Ft) * Max Discharge (Cubic Ft/Second) / POWER_CONVERSION_FACTOR. 
# Dams that do not meet this threshold are filtered out as potential roots, but still included in the cascade search if they are downstream of a qualifying root.
hydroelectric_dams_matched_filtered = hydroelectric_dams_matched[hydroelectric_dams_matched['Hydraulic Height (Ft)'] * hydroelectric_dams_matched['Max Discharge (Cubic Ft/Second)'] / POWER_CONVERSION_FACTOR > 10]

print(f"Dams with matched nodes: {len(dam_inventory_matched)}")
print(f"Hydroelectric dams that surpass the power capacity threshold with matched nodes: {len(hydroelectric_dams_matched_filtered)}")

ROOT_CANDIDATES_CSV = 'hydroelectric_root_candidates.csv'
df_root_candidates = hydroelectric_dams_matched_filtered[['NID ID', 'Dam Name', 'Purposes']].copy()
df_root_candidates = df_root_candidates.sort_values('NID ID').reset_index(drop=True)
df_root_candidates.to_csv(ROOT_CANDIDATES_CSV, index=False)
print(f"Saved {len(df_root_candidates)} qualifying root-candidate dams to {ROOT_CANDIDATES_CSV}")

dam_nodes = set(dam_inventory_matched['node_id'])

# This mapping is going to be used in our downstream search to quickly look up the NID ID of a dam given its node ID.
node_to_damid = (
    dam_inventory_matched
    .set_index('node_id')['NID ID']
    .to_dict()
)

def explore_node_dams(node_id: int) -> None:
    """
    Display all dams at a specific node
    """
    dams_at_node = dam_inventory_matched[dam_inventory_matched['node_id'] == node_id]

    if len(dams_at_node) == 0:
        print(f"No dams found at node {node_id}")
        return

    print(f"\n{'='*60}")
    print(f"Node ID: {node_id}")
    print(f"Number of dams: {len(dams_at_node)}")
    print(f"Location: ({dams_at_node.iloc[0]['Latitude']}, {dams_at_node.iloc[0]['Longitude']})")
    print(f"{'='*60}\n")

    for idx, dam in dams_at_node.iterrows():
        print(f"Dam: {dam['Dam Name']}")
        print(f"  NID ID: {dam['NID ID']}")
        print(f"  Purposes: {dam['Purposes']}")
        print(f"  Operational Status: {dam['Operational Status']}")
        print()

LENGTH_ATTR = 'lengthkm' # This is the attribute name in the river network graph that stores the length of each edge in kilometers.
KM_TO_MILES = 0.621371 # Conversion factor from kilometers to miles, which we chose to use for distance calculations in the downstream search function.
MAX_DISTANCE_MILES = 100.0 # Maximum distance to search downstream of a node in miles.

def find_downstream_dam(graph, start_node: int, dam_nodes: set,
                                    length_attr: str = LENGTH_ATTR, max_distance: float = MAX_DISTANCE_MILES, return_path: bool = False) -> tuple|None:
        """
        Iterative DFS following the river network downstream (via graph.successors)
        starting from `start_node`, searching for the first node containing a dam.

        Returns:
        If return_path is False: downstream_node, or None if no dam was found.
        If return_path is True: (downstream_node, path, distance_miles), with
        downstream_node/distance_miles set to None if no dam was found.
        """
        visited = {start_node}
        stack = [(start_node, 0.0, iter(graph.successors(start_node)))]
        path = [start_node]

        while stack:
            node, distance_so_far, successors_iter = stack[-1]
            advanced = False

            for neighbor in successors_iter:
                seg_length_km = graph[node][neighbor].get(length_attr, 0) or 0
                new_distance = distance_so_far + (seg_length_km * KM_TO_MILES)

                if new_distance > max_distance:
                    continue

                if neighbor in dam_nodes:
                    path.append(neighbor)
                    if return_path:
                        return neighbor, path, new_distance
                    else:
                        return neighbor

                if neighbor in visited:
                    continue

                visited.add(neighbor)
                stack.append((neighbor, new_distance, iter(graph.successors(neighbor))))
                path.append(neighbor)
                advanced = True
                break

            if not advanced:
                visited.discard(node)
                stack.pop()
                if path and path[-1] == node:
                    path.pop()

        if return_path:
            return None, path, None
        else:
            return None

damid_to_name = (
    dam_inventory_matched
    .set_index('NID ID')['Dam Name']
    .to_dict()
)

damid_to_purposes = (
    dam_inventory_matched
    .set_index('NID ID')['Purposes']
    .to_dict()
)

# For functional purposes, we wanted to filter cascades to only include dams that are owned by the same entity. 
# This is the most relevant information for operation, as dams owned by different entities may not be operated in a coordinated manner.
damid_to_owner = (
    dam_inventory_matched
    .set_index('NID ID')['Owner Names']
    .to_dict()
)

def is_hydroelectric_purpose(purposes) -> bool:
    """
    Shared helper: determine whether a dam's Purposes string indicates
    it is hydroelectric, tolerant of NaN/None input.
    """
    return bool(pd.notna(purposes) and 'Hydroelectric' in str(purposes))

def get_segment_distance_miles(graph, start_node: int, end_node: int) -> float|None:
    """
    Compute the downstream river distance (in miles) between two specific
    graph nodes, by reusing find_downstream_dam with a target set
    containing ONLY end_node. Moved above the cascade-building stage
    (rather than defined alongside get_cascade_chain as in earlier
    versions), since the new merge/export step below now needs it to
    compute each edge's distance at CSV-build time, rather than only
    on-demand later when visualizing a specific cascade.
    """
    downstream_node, _path, distance = find_downstream_dam(
        graph, start_node, {end_node}, return_path=True
    )
    return distance

DOWNSTREAM_LINKS_CSV = 'downstream_dam_pairs.csv'
# For the first run, we want to find the downstream dam for each dam in the inventory. 
# We cache the results in a CSV file to avoid the few seconds of computation for each dam on subsequent runs.
if not os.path.exists(DOWNSTREAM_LINKS_CSV):
    print(f"No existing {DOWNSTREAM_LINKS_CSV} found, starting batch search for downstream dams...")

    results = []
    total = len(dam_inventory_matched)
    start_time = time.time()

    for i, (idx, dam_row) in enumerate(dam_inventory_matched.iterrows()):
        start_node = dam_row['node_id']
        start_nid = dam_row['NID ID']
        start_name = dam_row['Dam Name']
        start_purposes = dam_row['Purposes']

        downstream_node = find_downstream_dam(graph, start_node, dam_nodes)

        if downstream_node is not None:
            downstream_nid = node_to_damid[downstream_node]
            downstream_name = damid_to_name.get(downstream_nid)
            downstream_purposes = damid_to_purposes.get(downstream_nid)
        else:
            downstream_nid = None
            downstream_name = None
            downstream_purposes = None

        results.append({
            'Dam': start_nid,
            'Dam Name': start_name,
            'Purposes': start_purposes,
            'Downstream Dam': downstream_nid,
            'Downstream Dam Name': downstream_name,
            'Downstream Dam Purposes': downstream_purposes
        })

        if (i + 1) % 500 == 0 or (i + 1) == total:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed
            remaining = (total - (i + 1)) / rate if rate > 0 else 0
            print(f"Processed {i + 1}/{total} dams... "
                f"({elapsed:.1f}s elapsed, ~{remaining:.1f}s remaining)")

    elapsed_total = time.time() - start_time
    print(f"\nCompleted in {elapsed_total:.1f} seconds")

    df_results = pd.DataFrame(results, columns=[
        'Dam', 'Dam Name', 'Purposes',
        'Downstream Dam', 'Downstream Dam Name', 'Downstream Dam Purposes'
    ])
    df_results = df_results.sort_values('Dam').reset_index(drop=True)
    df_results.to_csv(DOWNSTREAM_LINKS_CSV, index=False)

    number_of_downstream_dams = df_results['Downstream Dam'].notna().sum()
    print(f"\nSaved {len(df_results)} rows to {DOWNSTREAM_LINKS_CSV}")
    print(f"{number_of_downstream_dams} dams found a downstream dam out of {len(df_results)} total dams "
        f"({number_of_downstream_dams / len(df_results) * 100:.1f}%)")

CASCADE_SYSTEMS_CSV = 'cascading_systems.csv'
CASCADE_SYSTEMS_SUMMARY_CSV = 'cascading_systems_summary.csv'
MIN_HYDROELECTRIC_DAMS_PER_CASCADE = 2
REQUIRE_SAME_OWNER = False

# NOTE: changing MIN_HYDROELECTRIC_DAMS_PER_CASCADE or REQUIRE_SAME_OWNER
# requires deleting the existing cascading_systems.csv (and
# cascading_systems_summary.csv) before rerunning, since this whole
# stage is skipped if that file already exists.

# Using our downstream dam pairs, we can now build the full cascading systems starting from each qualifying hydroelectric root candidate. 
# Each system is a directed graph of dams connected by downstream links, and may include multiple converging cascades that share a downstream dam.
if not os.path.exists(CASCADE_SYSTEMS_CSV):
    print(f"No existing {CASCADE_SYSTEMS_CSV} found, building cascading systems...")

    df_links = pd.read_csv(DOWNSTREAM_LINKS_CSV, dtype={'Dam': str, 'Downstream Dam': str})

    downstream_map = dict(zip(df_links['Dam'], df_links['Downstream Dam']))

    root_candidate_ids = set(hydroelectric_dams_matched_filtered['NID ID'])

    def is_hydroelectric(dam_id):
        return is_hydroelectric_purpose(damid_to_purposes.get(dam_id))

    def same_owner(dam_id_a, dam_id_b) -> bool:
        owner_a = damid_to_owner.get(dam_id_a)
        owner_b = damid_to_owner.get(dam_id_b)
        if pd.isna(owner_a) or pd.isna(owner_b):
            return False
        return owner_a == owner_b

    def build_full_chain(start_dam_id, require_same_owner: bool = REQUIRE_SAME_OWNER) -> list:
        """
        Follow the downstream chain starting at start_dam_id, continuing
        through dams of ANY purpose, stopping if ownership diverges from
        start_dam_id (when require_same_owner is True) or when no
        further downstream dam is recorded at all.
        """
        chain = [start_dam_id]
        visited = {start_dam_id}
        current = start_dam_id

        while True:
            downstream = downstream_map.get(current)

            if pd.isna(downstream):
                break

            if downstream in visited:
                print(f"Warning: cycle detected involving dam {downstream}, stopping chain early.")
                break

            if require_same_owner and not same_owner(start_dam_id, downstream):
                break

            chain.append(downstream)
            visited.add(downstream)
            current = downstream

        return chain

    print(f"Qualifying hydroelectric root candidates: {len(root_candidate_ids)}")
    print(f"Same-owner continuation requirement: {REQUIRE_SAME_OWNER}")

    candidate_chains = {
        candidate_id: build_full_chain(candidate_id)
        for candidate_id in root_candidate_ids
    }

    covered_ids = set()
    for candidate_id, chain in candidate_chains.items():
        for dam_id in chain[1:]:
            if dam_id in root_candidate_ids:
                covered_ids.add(dam_id)

    root_dams = [candidate_id for candidate_id in root_candidate_ids if candidate_id not in covered_ids]

    print(f"Root candidates that are covered by another root's chain: {len(covered_ids)}")
    print(f"True root dams (initial cascade starting points): {len(root_dams)}")

    # --- Build and filter the INITIAL cascades (same rules as before) ---
    cascades = [candidate_chains[root_id] for root_id in root_dams]
    cascades = [c for c in cascades if len(c) >= 2]

    def count_hydroelectric(chain: list) -> int:
        return sum(1 for dam_id in chain if is_hydroelectric(dam_id))

    cascades = [c for c in cascades if count_hydroelectric(c) >= MIN_HYDROELECTRIC_DAMS_PER_CASCADE]

    print(f"Initial cascades passing both filters (>=2 dams, >={MIN_HYDROELECTRIC_DAMS_PER_CASCADE} hydroelectric): {len(cascades)}")

    # --- Merge initial cascades that share any node ---
    # Two (or more) independently-built cascades that converge at a
    # shared river node -- e.g. two upstream hydroelectric chains that
    # both flow into the same downstream dam -- represent ONE real-world
    # connected system, not two separate ones. A union-find (disjoint
    # set) over dam IDs detects any such overlap among the initial
    # cascades above and merges them.
    parent = {}

    def dsu_find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def dsu_union(a, b):
        ra, rb = dsu_find(a), dsu_find(b)
        if ra != rb:
            parent[ra] = rb

    for chain in cascades:
        for dam_id in chain:
            dsu_find(dam_id)
        for a, b in zip(chain[:-1], chain[1:]):
            dsu_union(a, b)

    group_edges = {}
    group_nodes = {}
    for chain in cascades:
        group_key = dsu_find(chain[0])
        group_edges.setdefault(group_key, set())
        group_nodes.setdefault(group_key, set())
        group_nodes[group_key].update(chain)
        for a, b in zip(chain[:-1], chain[1:]):
            group_edges[group_key].add((a, b))

    print(f"Combined systems after merging shared-node cascades: {len(group_edges)}")

    damid_to_node_lookup = dam_inventory_matched.set_index('NID ID')['node_id']

    systems = [] # Cascading systems
    for group_key, edges in group_edges.items():
        nodes = group_nodes[group_key]

        incoming = {n: 0 for n in nodes}
        for (a, b) in edges:
            incoming[b] = incoming.get(b, 0) + 1
        root_dams_in_system = sorted(n for n in nodes if incoming.get(n, 0) == 0)

        hydro_count = sum(1 for n in nodes if is_hydroelectric(n))

        # System ID: the alphanumerically smallest root dam in the
        # merged group -- gives each system a stable, human-recognizable
        # identifier even when multiple original cascades were combined.
        system_id = min(root_dams_in_system)

        systems.append({
            'system_id': system_id,
            'nodes': nodes,
            'edges': edges,
            'root_dams': root_dams_in_system,
            'hydro_count': hydro_count
        })

    # --- Export edge-list CSV: one row per direct downstream link ---
    # A dam may have more than one upstream predecessor. An edge-list format
    # (one row per link) handles branching naturally.
    edge_rows = []
    for system in systems:
        for (dam_id_a, dam_id_b) in system['edges']:
            node_a = int(damid_to_node_lookup.loc[dam_id_a])
            node_b = int(damid_to_node_lookup.loc[dam_id_b])
            distance = get_segment_distance_miles(graph, node_a, node_b)

            edge_rows.append({
                'System ID': system['system_id'],
                'Upstream Dam ID': dam_id_a,
                'Upstream Dam Name': damid_to_name.get(dam_id_a),
                'Upstream Dam Purposes': damid_to_purposes.get(dam_id_a),
                'Upstream Dam Owner': damid_to_owner.get(dam_id_a),
                'Downstream Dam ID': dam_id_b,
                'Downstream Dam Name': damid_to_name.get(dam_id_b),
                'Downstream Dam Purposes': damid_to_purposes.get(dam_id_b),
                'Downstream Dam Owner': damid_to_owner.get(dam_id_b),
                'Distance (Miles)': round(distance, 2) if distance is not None else None
            })

    df_edges = pd.DataFrame(edge_rows, columns=[
        'System ID',
        'Upstream Dam ID', 'Upstream Dam Name', 'Upstream Dam Purposes', 'Upstream Dam Owner',
        'Downstream Dam ID', 'Downstream Dam Name', 'Downstream Dam Purposes', 'Downstream Dam Owner',
        'Distance (Miles)'
    ])
    df_edges = df_edges.sort_values(['System ID', 'Upstream Dam ID']).reset_index(drop=True)
    df_edges.to_csv(CASCADE_SYSTEMS_CSV, index=False)

    # --- Companion summary CSV: one row per system ---
    summary_rows = []
    for system in systems:
        summary_rows.append({
            'System ID': system['system_id'],
            'Root Dam IDs': '; '.join(system['root_dams']),
            'Root Dam Names': '; '.join(damid_to_name.get(d, d) for d in system['root_dams']),
            'Total Dams': len(system['nodes']),
            'Hydroelectric Dams': system['hydro_count']
        })

    df_summary = pd.DataFrame(summary_rows, columns=[
        'System ID', 'Root Dam IDs', 'Root Dam Names', 'Total Dams', 'Hydroelectric Dams'
    ])
    df_summary = df_summary.sort_values('System ID').reset_index(drop=True)
    df_summary.to_csv(CASCADE_SYSTEMS_SUMMARY_CSV, index=False)

    print(f"\nSaved {len(df_edges)} edges across {len(systems)} systems to {CASCADE_SYSTEMS_CSV}")
    print(f"Saved system summary to {CASCADE_SYSTEMS_SUMMARY_CSV}")
    multi_root_systems = sum(1 for s in systems if len(s['root_dams']) > 1)
    print(f"Systems formed by merging 2+ original cascades: {multi_root_systems}")

def build_cascade_graph(system_id, cascade_csv=CASCADE_SYSTEMS_CSV):
    """
    Build a networkx DiGraph for a single (possibly branching) cascading
    system, identified by system_id, directly from the edge-list
    cascading_systems.csv. A system may have multiple upstream root
    dams converging into a shared downstream path.
    """
    df_edges = pd.read_csv(cascade_csv, dtype=str)
    system_rows = df_edges[df_edges['System ID'] == system_id]

    if system_rows.empty:
        raise ValueError(
            f"No cascading system found with System ID '{system_id}' in {cascade_csv}"
        )

    G = nx.DiGraph()

    for _, row in system_rows.iterrows():
        for prefix in ('Upstream', 'Downstream'):
            dam_id = row[f'{prefix} Dam ID']
            if dam_id not in G:
                G.add_node(
                    dam_id,
                    name=row[f'{prefix} Dam Name'],
                    purposes=row[f'{prefix} Dam Purposes'],
                    owner=row[f'{prefix} Dam Owner'],
                    is_hydroelectric=is_hydroelectric_purpose(row[f'{prefix} Dam Purposes'])
                )

        distance_str = row['Distance (Miles)']
        distance = float(distance_str) if pd.notna(distance_str) and distance_str != '' else None
        G.add_edge(row['Upstream Dam ID'], row['Downstream Dam ID'], distance_miles=distance)

    # Latitude/longitude aren't repeated across every edge row in the
    # CSV (to avoid redundant duplication), so they're joined in here
    # from dam_inventory_matched instead.
    matched_indexed = dam_inventory_matched.set_index('NID ID')
    for dam_id in G.nodes:
        if dam_id not in matched_indexed.index:
            raise ValueError(
                f"Dam '{dam_id}' not found in dam_inventory_matched -- cannot resolve coordinates."
            )
        dam_row = matched_indexed.loc[dam_id]
        G.nodes[dam_id]['latitude'] = dam_row['Latitude']
        G.nodes[dam_id]['longitude'] = dam_row['Longitude']

    return G


def build_all_cascade_graphs(cascade_csv=CASCADE_SYSTEMS_CSV):
    """
    Build a networkx DiGraph for every cascading system listed in
    cascade_csv, keyed by System ID.
    """
    df_edges = pd.read_csv(cascade_csv, dtype=str)
    system_ids = df_edges['System ID'].unique().tolist()

    cascade_graphs = {}
    total = len(system_ids)

    for i, system_id in enumerate(system_ids):
        cascade_graphs[system_id] = build_cascade_graph(system_id, cascade_csv)

        if (i + 1) % 25 == 0 or (i + 1) == total:
            print(f"Built {i + 1}/{total} cascade graphs...")

    return cascade_graphs


if os.path.exists(CASCADE_SYSTEMS_CSV):
    def visualize_cascade(system_id, cascade_csv=CASCADE_SYSTEMS_CSV, output_dir='.', style='animated'):
        """
        Build an interactive folium map for the cascading SYSTEM
        identified by system_id, which may include multiple converging
        cascades sharing a downstream dam. Root dam(s) -- in-degree 0
        within this system -- are green; terminal dam(s) -- out-degree 0
        -- are red; intermediate hydroelectric dams are blue;
        intermediate non-hydroelectric dams are gray.

        Saves the map to '<output_dir>/cascade_<system_id>.html'.
        """
        G = build_cascade_graph(system_id, cascade_csv)

        roots = [n for n in G.nodes if G.in_degree(n) == 0]
        sinks = [n for n in G.nodes if G.out_degree(n) == 0]

        print(f"System {system_id}: {G.number_of_nodes()} dams, "
                f"{len(roots)} root(s), {len(sinks)} terminus/termini")

        avg_lat = sum(attrs['latitude'] for _, attrs in G.nodes(data=True)) / G.number_of_nodes()
        avg_lon = sum(attrs['longitude'] for _, attrs in G.nodes(data=True)) / G.number_of_nodes()

        m = folium.Map(location=[avg_lat, avg_lon], zoom_start=8)

        for dam_id, attrs in G.nodes(data=True):
            if dam_id in roots:
                color = 'green'
            elif dam_id in sinks:
                color = 'red'
            elif attrs['is_hydroelectric']:
                color = 'blue'
            else:
                color = 'gray'

            hydro_label = "Hydroelectric" if attrs['is_hydroelectric'] else "Non-hydroelectric"

            folium.Marker(
                location=[attrs['latitude'], attrs['longitude']],
                popup=f"{attrs['name']} ({dam_id}) -- {hydro_label}"
                        f"<br>Owner: {attrs['owner']}"
                        f"<br>Purposes: {attrs['purposes']}",
                tooltip=f"{attrs['name']} ({hydro_label})",
                icon=folium.Icon(color=color, icon='tint', prefix='fa')
            ).add_to(m)

        for dam_id_a, dam_id_b, edge_attrs in G.edges(data=True):
            coord_a = (G.nodes[dam_id_a]['latitude'], G.nodes[dam_id_a]['longitude'])
            coord_b = (G.nodes[dam_id_b]['latitude'], G.nodes[dam_id_b]['longitude'])

            distance = edge_attrs['distance_miles']
            distance_label = f"{distance:.2f} river miles" if distance is not None else "distance unavailable"
            name_a = G.nodes[dam_id_a]['name']
            name_b = G.nodes[dam_id_b]['name']

            if style == 'animated':
                AntPath(
                    locations=[coord_a, coord_b],
                    color='blue', weight=3, opacity=0.8,
                    delay=1000, dash_array=[10, 20],
                    tooltip=f"{name_a} -> {name_b}: {distance_label}"
                ).add_to(m)
            elif style == 'static':
                line = folium.PolyLine(
                    locations=[coord_a, coord_b],
                    color='blue', weight=3, opacity=0.8,
                    tooltip=f"{name_a} -> {name_b}: {distance_label}"
                ).add_to(m)
                PolyLineTextPath(
                    line, '   \u25BA   ', repeat=True, offset=7,
                    attributes={'fill': '#00008B', 'font-weight': 'bold', 'font-size': '16'}
                ).add_to(m)

            midpoint = [(coord_a[0] + coord_b[0]) / 2, (coord_a[1] + coord_b[1]) / 2]
            folium.map.Marker(
                midpoint,
                icon=folium.DivIcon(
                    html=(
                        '<div style="font-size: 10pt; color: black; background-color: white; '
                        f'padding: 2px; border: 1px solid gray; border-radius: 3px;">{distance_label}</div>'
                    )
                )
            ).add_to(m)

        output_path = f"{output_dir}/cascade_{system_id}.html"
        m.save(output_path)
        print(f"Saved cascade map to {output_path}")

        return m

    # --- Generate a folium map for every cascading system, organized ---
    # --- into a subfolder per state ---
    OUTPUT_MAPS_DIR = 'cascade_maps'
    os.makedirs(OUTPUT_MAPS_DIR, exist_ok=True)

    df_summary = pd.read_csv(CASCADE_SYSTEMS_SUMMARY_CSV, dtype=str)
    system_ids_to_map = df_summary['System ID'].tolist()

    print(f"Generating {len(system_ids_to_map)} cascade maps...")
    succeeded = 0
    failed = []

    for i, system_id in enumerate(system_ids_to_map):
        # System ID is itself a dam's NID ID (the alphanumerically
        # smallest root in that merged system), so its 2-letter prefix
        # is used for the state subfolder. Note a merged system CAN
        # include dams from a different state further downstream (e.g.
        # a Georgia-rooted system flowing into Alabama) -- this places
        # the whole system's map under the identifier's state for organization
        # purposes, not every state it happens to pass through.
        state_code = system_id[:2].upper()
        state_dir = os.path.join(OUTPUT_MAPS_DIR, state_code)
        os.makedirs(state_dir, exist_ok=True)

        try:
            visualize_cascade(system_id, output_dir=state_dir)
            succeeded += 1
        except Exception as e:
            print(f"Failed to visualize system '{system_id}': {e}")
            failed.append((system_id, str(e)))

        if (i + 1) % 25 == 0 or (i + 1) == len(system_ids_to_map):
            print(f"Processed {i + 1}/{len(system_ids_to_map)} systems...")

    print(f"\nDone. {succeeded} maps saved under '{OUTPUT_MAPS_DIR}/<STATE>/'.")
    if failed:
        print(f"{len(failed)} systems failed to visualize:")
        for system_id, error in failed:
            print(f"  {system_id}: {error}")

CASCADE_GRAPHS_CACHE_FILE = 'cascade_graphs.pkl'

if not os.path.exists(CASCADE_GRAPHS_CACHE_FILE):
    print("Building networkx graphs for all cascading systems...")
    cascade_graphs = build_all_cascade_graphs()

    print(f"Saving cascade graphs to cache: {CASCADE_GRAPHS_CACHE_FILE}")
    with open(CASCADE_GRAPHS_CACHE_FILE, 'wb') as file:
        pickle.dump(cascade_graphs, file)
else:
    print(f"Loading cascade graphs from cache: {CASCADE_GRAPHS_CACHE_FILE}")
    with open(CASCADE_GRAPHS_CACHE_FILE, 'rb') as file:
        cascade_graphs = pickle.load(file)

print(f"Total cascading systems stored: {len(cascade_graphs)}")

def print_cascade_graph(system_id: str, cascade_graphs: dict) -> nx.DiGraph:
    """
    Print a formatted summary of the cascade graph for system_id: node
    count, edge count, root dam(s), terminal dam(s), each node's
    attributes tagged [hydroelectric]/[non-hydroelectric], and every direct downstream
    link with distance. Iterates G.edges() directly rather than zipping
    a topological-sort order, since a branching system can have nodes
    that are adjacent in topological order without a direct edge
    between them.
    """
    if system_id not in cascade_graphs:
        raise ValueError(
            f"No cascade found with System ID '{system_id}' in cascade_graphs"
        )

    G = cascade_graphs[system_id]

    roots = [n for n in G.nodes if G.in_degree(n) == 0]
    sinks = [n for n in G.nodes if G.out_degree(n) == 0]
    root_descriptions = ', '.join(f"{r} ({G.nodes[r]['name']})" for r in roots)
    sink_descriptions = ', '.join(f"{s} ({G.nodes[s]['name']})" for s in sinks)

    print(f"\n{'='*60}")
    print(f"Cascade system: {system_id}")
    print(f"{'='*60}")
    print(f"Number of nodes (dams): {G.number_of_nodes()}")
    print(f"Number of edges (downstream links): {G.number_of_edges()}")
    print(f"Root dam(s): {root_descriptions}")
    print(f"Terminal dam(s): {sink_descriptions}")
    hydro_count = sum(1 for _, attrs in G.nodes(data=True) if attrs['is_hydroelectric'])
    print(f"Hydroelectric dams in this system: {hydro_count}")

    print("\nNodes:")
    for node_id, attrs in G.nodes(data=True):
        tag = "[hydroelectric]" if attrs['is_hydroelectric'] else "[non-hydroelectric]"
        print(f"  {tag} {node_id}: {attrs['name']!r} "
                f"(owner={attrs['owner']}, lat={attrs['latitude']}, lon={attrs['longitude']})")

    print("\nEdges:")
    for u, v, edge_attrs in G.edges(data=True):
        distance = edge_attrs['distance_miles']
        distance_str = f"{distance:.2f} miles" if distance is not None else "unavailable"
        print(f"  {u} ({G.nodes[u]['name']}) -> {v} ({G.nodes[v]['name']}): {distance_str}")

    return G

CONUS_MAP_FILE = 'conus_cascade_map.html'

CASCADE_LINE_COLORS = [
    'blue', 'darkred', 'darkgreen', 'purple', 'orange',
    'darkblue', 'cadetblue', 'deeppink', 'black', 'darkorange'
]

def bearing_degrees(lat1, lon1, lat2, lon2):
    """Compute compass bearing (degrees, 0=north) from point 1 to point 2."""
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    delta_lon = math.radians(lon2 - lon1)
    x = math.sin(delta_lon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(delta_lon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def build_conus_cascade_map(cascade_graphs, output_file=CONUS_MAP_FILE,
                                show_distance_labels=False):
    """
    Build a single lightweight folium map showing every cascading SYSTEM
    in cascade_graphs at once, each as its own toggleable layer.

    Updated for branching systems (post shared-node merge):
    - Root/terminal dams are now determined via in_degree/out_degree == 0
        rather than "first/last position in a chain", since a merged
        system can have multiple roots converging into one or more shared
        downstream paths.
    - Edges are drawn by iterating G.edges() directly, rather than
        zipping a topological-sort ordering -- the latter only produces
        correct adjacent pairs for a strictly linear chain, and would
        silently skip or misrepresent edges in a branching graph.

    Performance notes (carried over from the earlier fix):
    - CircleMarker instead of Icon markers for lighter rendering.
    - A single rotated arrowhead per edge at its midpoint, instead of
        PolyLineTextPath (which repeats glyphs along the entire line
        length and was the cause of a previous 4MB+ file that crashed on
        open).
    """
    all_lats, all_lons = [], []
    for G in cascade_graphs.values():
        for _, attrs in G.nodes(data=True):
            all_lats.append(attrs['latitude'])
            all_lons.append(attrs['longitude'])

    center = [(min(all_lats) + max(all_lats)) / 2, (min(all_lons) + max(all_lons)) / 2]

    m = folium.Map(location=center, zoom_start=5, tiles='cartodbpositron')

    color_cycle = itertools.cycle(CASCADE_LINE_COLORS)

    for system_id, G in cascade_graphs.items():
        line_color = next(color_cycle)

        roots = {n for n in G.nodes if G.in_degree(n) == 0}
        sinks = {n for n in G.nodes if G.out_degree(n) == 0}

        root_names = ', '.join(G.nodes[r]['name'] for r in roots)
        fg = folium.FeatureGroup(name=f"{root_names} ({system_id})", show=True)

        # --- Markers ---
        for dam_id, attrs in G.nodes(data=True):
            lat, lon = attrs['latitude'], attrs['longitude']

            if dam_id in roots:
                color = 'green'
            elif dam_id in sinks:
                color = 'red'
            elif attrs['is_hydroelectric']:
                color = 'blue'
            else:
                color = 'gray'

            hydro_label = "Hydroelectric" if attrs['is_hydroelectric'] else "Non-hydroelectric"

            folium.CircleMarker(
                location=[lat, lon],
                radius=5,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.9,
                weight=1,
                popup=f"{attrs['name']} ({dam_id}) -- {hydro_label}"
                        f"<br>System: {system_id}"
                        f"<br>Owner: {attrs['owner']}"
                        f"<br>Purposes: {attrs['purposes']}",
                tooltip=f"{attrs['name']} ({hydro_label})"
            ).add_to(fg)

        # --- Edges: iterate G.edges() directly (correct for branching) ---
        for u, v, edge_attrs in G.edges(data=True):
            distance = edge_attrs['distance_miles']
            distance_label = f"{distance:.2f} river miles" if distance is not None else "distance unavailable"
            u_name = G.nodes[u]['name']
            v_name = G.nodes[v]['name']

            loc_u = (G.nodes[u]['latitude'], G.nodes[u]['longitude'])
            loc_v = (G.nodes[v]['latitude'], G.nodes[v]['longitude'])

            folium.PolyLine(
                locations=[loc_u, loc_v],
                color=line_color,
                weight=2,
                opacity=0.7,
                tooltip=f"{u_name} -> {v_name}: {distance_label}"
            ).add_to(fg)

            midpoint = [(loc_u[0] + loc_v[0]) / 2, (loc_u[1] + loc_v[1]) / 2]
            angle = bearing_degrees(loc_u[0], loc_u[1], loc_v[0], loc_v[1])

            arrow_html = (
                '<div style="width:20px; height:20px; display:flex; '
                'align-items:center; justify-content:center; '
                f'transform: rotate({angle}deg); transform-origin: center center;">'
                f'<span style="font-size:16px; color:{line_color};">&#9650;</span>'
                '</div>'
            )

            folium.map.Marker(
                midpoint,
                icon=folium.DivIcon(
                    html=arrow_html,
                    icon_size=(20, 20),
                    icon_anchor=(10, 10)
                ),
                tooltip=f"{u_name} -> {v_name}: {distance_label}"
            ).add_to(fg)

            if show_distance_labels:
                folium.map.Marker(
                    midpoint,
                    icon=folium.DivIcon(
                        html=(
                            '<div style="font-size: 9pt; color: black; background-color: white; '
                            f'padding: 1px; border: 1px solid gray; border-radius: 3px; '
                            f'margin-top: 14px;">{distance_label}</div>'
                        ),
                        icon_size=(0, 0)
                    )
                ).add_to(fg)

        fg.add_to(m)

    folium.LayerControl(collapsed=True).add_to(m)

    m.save(output_file)
    print(f"Saved CONUS cascade map with {len(cascade_graphs)} systems to {output_file}")

    return m


# --- Build the national overview map ---
build_conus_cascade_map(cascade_graphs)