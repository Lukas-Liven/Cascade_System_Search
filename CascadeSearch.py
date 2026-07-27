import numpy as np
#from math import radians, sin, cos, sqrt, atan2
#from scipy.spatial.distance import pdist, squareform
#from sklearn.neighbors import KDTree
import pandas as pd
import matplotlib.pyplot as plt
import pynhd as nhd
from pynhd import NLDI, NHDPlusHR, GeoConnex, WaterData, NHD
import pygeohydro as gh
import sys
from StoragePlots import analyze_dam_storage

import folium
import networkx as nx

import pickle

#from shapely.geometry import Point

print("Loading US River Network data...")
with open('US_River_Network.pkl', 'rb') as file:
    graph, node2comid = pickle.load(file)

print("Data loaded successfully!")
print(f"Graph has {graph.number_of_nodes()} nodes and {graph.number_of_edges()} edges") #The graph is a directed graph representing the river network, where nodes are river segments and edges represent flow direction.

nid = gh.NID()
df_dams = nid.df

df_dams = df_dams.dropna(subset=['Max Storage (Acre-Ft)'])
df_dams = df_dams.dropna(subset=['Longitude', 'Latitude'])

df_dams = df_dams[df_dams['Max Storage (Acre-Ft)'] > 100]

is_hydroelectric = df_dams['Purposes'].str.contains('Hydroelectric', case=False, na=False)
df_hydroelectric = df_dams[is_hydroelectric]

print(f"Total dams with coordinates: {len(df_dams)}")
print(f"Hydroelectric dams: {len(df_hydroelectric)}")

# Step 1: Get dam data from GeoConnex
print("Fetching dam data from GeoConnex...")
dam = GeoConnex("dams")
dams = dam.bybox((-125, 25, -65, 50))
print(f"Retrieved {len(dams)} dams from GeoConnex")

# Step 2: Map dam ID to COMID
dict_damid_comid = {
    dam['provider_id']: int(float(str(dam['nhdpv2_comid']).split('/')[-1])) 
    if pd.notna(dam['nhdpv2_comid']) else None 
    for k, dam in dams.iterrows()
}
print(f"Created dam ID to COMID mapping for {len(dict_damid_comid)} dams")

# Step 3: Create reverse mapping from COMID to node
comid2node = {v: k for k, v in node2comid.items()}
print(f"Created COMID to node mapping for {len(comid2node)} nodes")

# Step 4: Find nodes for dams
nodes_of_dams = set([
    comid2node[dict_damid_comid[damid]] 
    for damid in df_dams['NID ID'] 
    if (damid in dict_damid_comid) 
    and (dict_damid_comid[damid] in comid2node)
])

print(f"\nResults:")
print(f"Total dams in df_dams: {len(df_dams)}")
print(f"Total number of nodes with matching dams: {len(nodes_of_dams)}")
print(f"Match rate: {len(nodes_of_dams)/len(df_dams)*100:.1f}%")

# Create a dictionary mapping dam ID to node ID for easy lookup
damid_to_node = {
    damid: comid2node[dict_damid_comid[damid]]
    for damid in df_dams['NID ID']
    if (damid in dict_damid_comid)
    and (dict_damid_comid[damid] in comid2node)
}

# Add node ID column to df_dams
df_dams['node_id'] = df_dams['NID ID'].map(damid_to_node)

# Filter to only dams that have a matching node
df_dams_matched = df_dams.dropna(subset=['node_id']).copy()
df_dams_matched['node_id'] = df_dams_matched['node_id'].astype(int)

#Filter out duplicates based on 'NID ID' to ensure each dam is counted only once
df_dams_matched = df_dams_matched.drop_duplicates(subset='NID ID', keep='first')

hydroelectric_dams_matched = df_dams_matched[df_dams_matched['Purposes'].str.contains('Hydroelectric', case=False, na=False)]
#Filter only for dams with 10 MW power capacity - multiply hydraulic height Ft and Max Discharge CFS to get MW capacity (1 MW = 5500 Ft-CFS)
#hydroelectric_dams_matched = hydroelectric_dams_matched[hydroelectric_dams_matched['Hydraulic Height (Ft)'] * hydroelectric_dams_matched['Max Discharge (Cubic Ft/Second)'] / 11800 > 10]

print(f"Dams with matched nodes: {len(df_dams_matched)}")
print(f"Hydroelectric dams with matched nodes: {len(hydroelectric_dams_matched)}")

# Set of ALL dam node IDs (any purpose) — this is what we search against
dam_nodes = set(df_dams_matched['node_id'])

# Map node_id -> NID ID (for translating a found node back to a dam identifier)
node_to_damid = (
    df_dams_matched
    .drop_duplicates(subset='node_id', keep='first')
    .set_index('node_id')['NID ID']
    .to_dict()
)

def explore_node_dams(node_id):
    """
    Display all dams at a specific node
    """
    dams_at_node = df_dams_matched[df_dams_matched['node_id'] == node_id]
    
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

"""
if len(multi_hydroelectric_dam_nodes_df['node_id'].unique()) > 0:
    for node in multi_hydroelectric_dam_nodes_df['node_id'].unique():
        explore_node_dams(node)
"""

LENGTH_ATTR = 'lengthkm'
KM_TO_MILES = 0.621371
MAX_DISTANCE_MILES = 100.0

def find_downstream_dam(graph, start_node, dam_nodes,
                         length_attr=LENGTH_ATTR, max_distance=MAX_DISTANCE_MILES):
    """
    Iterative DFS following the river network downstream (via graph.successors)
    starting from `start_node`, searching for the first node containing a dam.

    - Fully explores one downstream path before backtracking to try the next
      branch at a fork (standard DFS order).
    - Tracks cumulative distance along the CURRENT path only.
    - If cumulative distance would exceed `max_distance` miles, that branch
      is abandoned and the next available branch is tried instead.
    - Returns None if no dam is found within budget on any path.
    """
    visited = {start_node}
    stack = [(start_node, 0.0, iter(graph.successors(start_node)))]

    while stack:
        node, distance_so_far, successors_iter = stack[-1]
        advanced = False

        for neighbor in successors_iter:
            seg_length_km = graph[node][neighbor].get(length_attr, 0) or 0
            new_distance = distance_so_far + (seg_length_km * KM_TO_MILES)

            if new_distance > max_distance:
                continue  # exceeds budget, try next sibling branch

            if neighbor in dam_nodes:
                return neighbor

            if neighbor in visited:
                continue  # avoid cycles, just in case

            visited.add(neighbor)
            stack.append((neighbor, new_distance, iter(graph.successors(neighbor))))
            advanced = True
            break  # go deeper before trying siblings

        if not advanced:
            visited.discard(node)
            stack.pop()

    return None

def find_downstream_dam_debug(graph, start_node, dam_nodes,
                               length_attr=LENGTH_ATTR, max_distance=MAX_DISTANCE_MILES):
    """
    Same logic as find_downstream_dam, but also returns the path taken
    and cumulative distance, for manual verification/debugging.

    Returns:
        (downstream_node, path, distance_miles)
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

    return None, path, None

damid_to_name = (
    df_dams_matched
    .drop_duplicates(subset='NID ID', keep='first')
    .set_index('NID ID')['Dam Name']
    .to_dict()
)

# --- Batch search with names included ---
import time

results = []
total = len(hydroelectric_dams_matched)
start_time = time.time()

for i, (idx, dam_row) in enumerate(hydroelectric_dams_matched.iterrows()):
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

df_results.to_csv('downstream_dams.csv', index=False)

found = df_results['Downstream Dam'].notna().sum()
print(f"\nSaved {len(df_results)} rows to downstream_dams.csv")
print(f"Downstream dam found for {found} of {len(df_results)} hydroelectric dams "
      f"({found / len(df_results) * 100:.1f}%)")