import pandas as pd
import os
import requests
import pickle
import sys

#import folium
#import networkx as nx
import pynhd
from pynhd import GeoConnex
import pygeohydro as gh
from StoragePlots import analyze_dam_storage, analyze_power_capacity, POWER_CONVERSION_FACTOR


# --- Load River Network and Node Mapping ---
print("Loading US River Network data...")
if not os.path.exists('US_River_Network.pkl'): # If this is your first run or the pkl file is not found will download the data. This takes a long time to download, the file is ~1 GB
    graph, node_to_comid, _ = pynhd.enhd_flowlines_nx() # ENHD River Network graph and mapping from node ID to Common Identifier (COMID) for river segments
    with open('US_River_Network.pkl', 'wb') as file: # Saving the download to a pkl to avoid having to download on future runs
        pickle.dump((graph, node_to_comid), file) 
else: # Skip the download if the pkl exists
    with open('US_River_Network.pkl', 'rb') as file: # Load the data from the pkl
        graph, node_to_comid = pickle.load(file) 

print("Data loaded successfully!")
print(f"Graph has {graph.number_of_nodes()} nodes and {graph.number_of_edges()} edges") #The graph is a directed graph representing the river network, where nodes are river segments and edges represent flow direction.

nid = gh.NID() # This will cache after the first run
dam_inventory = nid.df

# --- Filter dam inventory ---
dam_inventory = dam_inventory.dropna(subset=['Max Storage (Acre-Ft)','Longitude', 'Latitude']) # If we don't have storage or coordinates, we can't use the dam for our analysis, so we drop those rows.
dam_inventory = dam_inventory[dam_inventory['Max Storage (Acre-Ft)'] > 100] # Filter out dams with storage less than 100 acre-feet, as they are likely too small to be relevant for our analysis.

# We explicitly don't use primary purpose here, because dams have multiple purposes and hydroelectric may not be listed as the primary one. 
# We want to capture all dams that have hydroelectric as one of their purposes.
df_hydroelectric = dam_inventory[dam_inventory['Purposes'].str.contains('Hydroelectric', case=False, na=False)]

print(f"Total dams with coordinates: {len(dam_inventory)}")
print(f"Hydroelectric dams: {len(df_hydroelectric)}")

# --- Get dam data from GeoConnex ---
GEOCONNEX_CACHE_FILE = 'geoconnex_dams.pkl'
if not os.path.exists(GEOCONNEX_CACHE_FILE): # If this is your first run or the pkl file is not found, query the data from GeoConnex
    print("Fetching dam data from GeoConnex...")
    global_geo_dams = GeoConnex("dams") # API call
    us_geo_dams = global_geo_dams.bybox((-125, 25, -65, 50))  # Bounding box for CONUS
    print(f"Retrieved {len(us_geo_dams)} dams from GeoConnex")

    print(f"Saving to cache: {GEOCONNEX_CACHE_FILE}")
    with open(GEOCONNEX_CACHE_FILE, 'wb') as file:
        pickle.dump(us_geo_dams, file)
else: # If you have the pkl file already we can skip the API call and load from the system
    print(f"Loading GeoConnex dam data from cache: {GEOCONNEX_CACHE_FILE}")
    with open(GEOCONNEX_CACHE_FILE, 'rb') as file:
        us_geo_dams = pickle.load(file)
    print(f"Loaded {len(us_geo_dams)} dams from GeoConnex")

# --- Map NID ID to COMID --- 
damid_to_comid = {
    # provider_id is the key in the GeoConnex data for NID ID. The COMID is found in a url under the 'nhdpv2_comid' field, so we extract the last part of the URL and convert it to an integer.
    dam['provider_id']: int(float(str(dam['nhdpv2_comid']).split('/')[-1])) 
    if pd.notna(dam['nhdpv2_comid']) else None 
    for k, dam in us_geo_dams.iterrows()
}
print(f"Created NID ID to COMID mapping for {len(damid_to_comid)} dams")

# --- Create reverse mapping from COMID to node ---
comid_to_node = {v: k for k, v in node_to_comid.items()}
print(f"Created COMID to node mapping for {len(comid_to_node)} nodes") # These nodes are the nodes in the river network graph and are required for location and traversal.

# --- Find nodes that contain dams ---
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
damid_to_node = {
    damid: comid_to_node[damid_to_comid[damid]]
    for damid in dam_inventory['NID ID']
    if (damid in damid_to_comid)
    and (damid_to_comid[damid] in comid_to_node)
}
# Add node ID column to dam_inventory
dam_inventory['node_id'] = dam_inventory['NID ID'].map(damid_to_node)

# --- Get dam data from ResNet ---
# This is used to recover dams that were not matched
# via the GeoConnex mapping above. You can include other datasets to recover COMID for matching following a similar approach if you have them available.
# Both datasets are recommended to use, they find less than 70% of the NID database when used independently.

record_id = "15644268"
resnet_file = "ResNet.csv"

# Only download if the file doesn't already exist locally, avoiding
# unnecessary repeated network requests on subsequent runs.
if not os.path.exists(resnet_file):
    url = f"https://zenodo.org/api/records/{record_id}/files/{resnet_file}/content"

    # A timeout is set to prevent the request from hanging indefinitely
    # if the server is slow or unresponsive.
    response = requests.get(url, stream=True, timeout=30)

    if response.status_code == 200:
        with open(resnet_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"Successfully downloaded {resnet_file}")
    else:
        print(f"Failed to download. Status code: {response.status_code}")
else:
    print(f"{resnet_file} already exists locally, skipping download.")

resnet_df = pd.read_csv('ResNet.csv')

# Normalize the join key
resnet_map = resnet_df.dropna(subset=['COMID']).set_index('NID')['COMID'].astype(int).to_dict()

# Repeating the same mapping process that was done for the GeoConnex data
resnet_node_map = {
    nid_id: comid_to_node[comid]
    for nid_id, comid in resnet_map.items()
    if comid in comid_to_node
}

# --- Attempt to recover missing dams using ResNet crosswalk, check how effective this data source is ---
still_missing_before = dam_inventory['node_id'].isna().sum()
dam_inventory['node_id'] = dam_inventory['node_id'].fillna(
    dam_inventory['NID ID'].map(resnet_node_map)
)
still_missing_after = dam_inventory['node_id'].isna().sum()

print(f"Dams recovered via ResNet crosswalk: {still_missing_before - still_missing_after}")
print(f"Updated match rate: "
        f"{(len(dam_inventory) - still_missing_after) / len(dam_inventory) * 100:.1f}%")

# --- Filtering to dams with nodes ---
dam_inventory_matched = dam_inventory.dropna(subset=['node_id']).copy() # Drop any dams that don't have an associated node after checking with available datasets
dam_inventory_matched['node_id'] = dam_inventory_matched['node_id'].astype(int)
# Drop duplicates to attempt to make it so each dam is represented only once to reduce search redundancy. This is not a perfect solution because some dams 
# have multiple NID IDs (e.g., different structures at the same site). Removing name duplicates would remove dams that are the same, so this flaw is accepted for now.  
dam_inventory_matched = dam_inventory_matched.drop_duplicates(subset='NID ID', keep='first') 

hydroelectric_dams_matched = dam_inventory_matched[dam_inventory_matched['Purposes'].str.contains('Hydroelectric', case=False, na=False)]

#Filter only for dams with 10 MW power capacity - multiply hydraulic height Ft and Max Discharge CFS to get MW capacity (1 MW = 5500 Ft-CFS)
hydroelectric_dams_matched_filtered = hydroelectric_dams_matched[hydroelectric_dams_matched['Hydraulic Height (Ft)'] * hydroelectric_dams_matched['Max Discharge (Cubic Ft/Second)'] / POWER_CONVERSION_FACTOR > 10]

print(f"Dams with matched nodes: {len(dam_inventory_matched)}")
print(f"Hydroelectric dams that surpass the power capacity threshold with matched nodes: {len(hydroelectric_dams_matched_filtered)}")

analyze_dam_storage(dam_inventory_matched)
sys.exit()

# Set of ALL dam node IDs (any purpose) — this is what we search against
dam_nodes = set(dam_inventory_matched['node_id'])

# Map node_id -> NID ID (for translating a found node back to a dam identifier)
node_to_damid = (
    dam_inventory_matched
    .drop_duplicates(subset='node_id', keep='first')
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

LENGTH_ATTR = 'lengthkm' #The attribute in the graph edges that contains the length of the river segment in kilometers
KM_TO_MILES = 0.621371
MAX_DISTANCE_MILES = 100.0

def find_downstream_dam(graph, start_node: int, dam_nodes: set,
                               length_attr: str = LENGTH_ATTR, max_distance: float = MAX_DISTANCE_MILES, return_path: bool = False) -> tuple|None:
    """
    Iterative DFS following the river network downstream (via graph.successors)
    starting from `start_node`, searching for the first node containing a dam.

    - Fully explores one downstream path before backtracking to try the next
      branch at a fork (standard DFS order).
    - Tracks cumulative distance along the CURRENT path only.
    - If cumulative distance would exceed `max_distance` miles, that branch
      is abandoned and the next available branch is tried instead.

    Returns:
    downstream_node OR (downstream_node, path, distance_miles)
    downstream_node is None if no dam was found.
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
                return neighbor, path, new_distance

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
    .drop_duplicates(subset='NID ID', keep='first')
    .set_index('NID ID')['Dam Name']
    .to_dict()
)

# --- Batch search for the downstream dam of each hydroelectric dam ---
import time

results = []
total = len(hydroelectric_dams_matched_filtered)
start_time = time.time()

for i, (idx, dam_row) in enumerate(hydroelectric_dams_matched_filtered.iterrows()):
    start_node = dam_row['node_id']
    start_nid = dam_row['NID ID']
    start_name = dam_row['Dam Name']

    downstream_node = find_downstream_dam(graph, start_node, dam_nodes)

    if downstream_node is not None:
        downstream_nid = node_to_damid[downstream_node]
        downstream_name = damid_to_name.get(downstream_nid)
    else:
        downstream_nid = None
        downstream_name = None

    results.append({
        'Hydroelectric Dam': start_nid,
        'Hydroelectric Dam Name': start_name,
        'Downstream Dam': downstream_nid,
        'Downstream Dam Name': downstream_name
    })

    # This process should be very fast (<1 second) if used for the original purpose, but if the dataset is modified that may not be the case.
    if (i + 1) % 100 == 0 or (i + 1) == total:
        elapsed = time.time() - start_time
        rate = (i + 1) / elapsed
        remaining = (total - (i + 1)) / rate if rate > 0 else 0
        print(f"Processed {i + 1}/{total} hydroelectric dams... "
              f"({elapsed:.1f}s elapsed, ~{remaining:.1f}s remaining)")

elapsed_total = time.time() - start_time
print(f"\nCompleted in {elapsed_total:.1f} seconds")

# --- Export to CSV ---
df_results = pd.DataFrame(results, columns=[
    'Hydroelectric Dam', 'Hydroelectric Dam Name',
    'Downstream Dam', 'Downstream Dam Name'
])
# Sort alphanumerically by Hydroelectric Dam NID ID for easier reading and comparison, given that most cascading dams are similar in NID ID, for state and number
df_results = df_results.sort_values('Hydroelectric Dam').reset_index(drop=True)
df_results.to_csv('downstream_dams_filtered_sorted_resnet.csv', index=False)

number_of_downstream_dams = df_results['Downstream Dam'].notna().sum()
print(f"\nSaved {len(df_results)} rows to downstream_dams_filtered_sorted_resnet.csv")
print(f"{number_of_downstream_dams} dams found downstream of {len(df_results)} hydroelectric dams "
      f"({number_of_downstream_dams / len(df_results) * 100:.1f}%)")