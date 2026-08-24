import pandas as pd
import os
import requests
import pickle
import time
import itertools
import sys

import folium
from folium.plugins import PolyLineTextPath
from folium.plugins import AntPath
import pynhd
import networkx as nx
from pynhd import GeoConnex
import pygeohydro as gh
from StoragePlots import analyze_dam_storage, analyze_power_capacity, POWER_CONVERSION_FACTOR


# --- Load River Network and Node Mapping ---
print("Loading US River Network data...")
if not os.path.exists('US_River_Network.pkl'):
    graph, node_to_comid, _ = pynhd.enhd_flowlines_nx()
    with open('US_River_Network.pkl', 'wb') as file:
        pickle.dump((graph, node_to_comid), file)
else:
    with open('US_River_Network.pkl', 'rb') as file:
        graph, node_to_comid = pickle.load(file)

print("Data loaded successfully!")
print(f"Graph has {graph.number_of_nodes()} nodes and {graph.number_of_edges()} edges")

nid = gh.NID()
dam_inventory = nid.df

# --- Filter dam inventory ---
dam_inventory = dam_inventory.dropna(subset=['Max Storage (Acre-Ft)', 'Longitude', 'Latitude'])
dam_inventory = dam_inventory[dam_inventory['NID ID'] == dam_inventory['Federal ID']]
dam_inventory = dam_inventory[dam_inventory['Max Storage (Acre-Ft)'] > 100]

df_hydroelectric = dam_inventory[dam_inventory['Purposes'].str.contains('Hydroelectric', case=False, na=False)]

print(f"Total dams with coordinates: {len(dam_inventory)}")
print(f"Hydroelectric dams: {len(df_hydroelectric)}")

# --- Get dam data from GeoConnex ---
GEOCONNEX_CACHE_FILE = 'geoconnex_dams.pkl'
if not os.path.exists(GEOCONNEX_CACHE_FILE):
    print("Fetching dam data from GeoConnex...")
    global_geo_dams = GeoConnex("dams")
    us_geo_dams = global_geo_dams.bybox((-125, 25, -65, 50))
    print(f"Retrieved {len(us_geo_dams)} dams from GeoConnex")

    print(f"Saving to cache: {GEOCONNEX_CACHE_FILE}")
    with open(GEOCONNEX_CACHE_FILE, 'wb') as file:
        pickle.dump(us_geo_dams, file)
else:
    print(f"Loading GeoConnex dam data from cache: {GEOCONNEX_CACHE_FILE}")
    with open(GEOCONNEX_CACHE_FILE, 'rb') as file:
        us_geo_dams = pickle.load(file)
    print(f"Loaded {len(us_geo_dams)} dams from GeoConnex")

# --- Map NID ID to COMID ---
damid_to_comid = {
    dam['provider_id']: int(float(str(dam['nhdpv2_comid']).split('/')[-1]))
    if pd.notna(dam['nhdpv2_comid']) else None
    for k, dam in us_geo_dams.iterrows()
}
print(f"Created NID ID to COMID mapping for {len(damid_to_comid)} dams")

# --- Create reverse mapping from COMID to node ---
comid_to_node = {v: k for k, v in node_to_comid.items()}
print(f"Created COMID to node mapping for {len(comid_to_node)} nodes")

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

dam_inventory['node_id'] = dam_inventory['NID ID'].map(damid_to_node)

# --- Get dam data from ResNet ---
record_id = "15644268"
resnet_file = "ResNet.csv"

if not os.path.exists(resnet_file):
    url = f"https://zenodo.org/api/records/{record_id}/files/{resnet_file}/content"
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

resnet_map = resnet_df.dropna(subset=['COMID']).set_index('NID')['COMID'].astype(int).to_dict()

resnet_node_map = {
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

# Prioritize hydroelectric dams when multiple dams map to the same node
dam_inventory_matched['is_hydroelectric'] = (
    dam_inventory_matched['Purposes']
    .str.contains('Hydroelectric', case=False, na=False)
)

dam_inventory_unique_nodes = (
    dam_inventory_matched
    .sort_values(
        'is_hydroelectric',
        ascending=False,
        kind='stable'
    )
    .drop_duplicates(subset='node_id', keep='first')
    .copy()
)

hydroelectric_dams_matched = dam_inventory_matched[dam_inventory_matched['Purposes'].str.contains('Hydroelectric', case=False, na=False)]

# Filter only for dams with 10 MW power capacity -- this defines which
# hydroelectric dams are eligible to be the ROOT (starting point) of a
# cascading system.
hydroelectric_dams_matched_filtered = hydroelectric_dams_matched[hydroelectric_dams_matched['Hydraulic Height (Ft)'] * hydroelectric_dams_matched['Max Discharge (Cubic Ft/Second)'] / POWER_CONVERSION_FACTOR > 10]

print(f"Dams with matched nodes: {len(dam_inventory_matched)}")
print(f"Hydroelectric dams that surpass the power capacity threshold with matched nodes: {len(hydroelectric_dams_matched_filtered)}")

# Set of ALL dam node IDs (any purpose) — this is what we search against
dam_nodes = set(dam_inventory_matched['node_id'])

node_to_damid = (
    dam_inventory_unique_nodes
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

LENGTH_ATTR = 'lengthkm'
KM_TO_MILES = 0.621371
MAX_DISTANCE_MILES = 100.0

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

DOWNSTREAM_LINKS_CSV = 'downstream_dam_pairs.csv'

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
MIN_HYDROELECTRIC_DAMS_PER_CASCADE = 2
# Toggle: when True, a cascade only continues past a dam if the next
# downstream dam shares the SAME 'Owner Names' as the cascade's root dam.
# This reflects the real-world pattern that cascading dam systems are
# typically owned/operated by a single company. When a downstream dam's
# owner differs from the root's owner, that dam is treated as NOT part
# of this cascade at all (excluded, not included as a terminal entry --
# see rationale above), and the chain stops immediately before it. A
# missing/NaN Owner Names on either dam is conservatively treated as a
# mismatch, since ownership cannot be confirmed in that case.

REQUIRE_SAME_OWNER = True

if not os.path.exists(CASCADE_SYSTEMS_CSV):
    print(f"No existing {CASCADE_SYSTEMS_CSV} found, building cascading systems...")

    df_links = pd.read_csv(DOWNSTREAM_LINKS_CSV, dtype={'Dam': str, 'Downstream Dam': str})

    downstream_map = dict(zip(df_links['Dam'], df_links['Downstream Dam']))

    root_candidate_ids = set(hydroelectric_dams_matched_filtered['NID ID'])

    def is_hydroelectric(dam_id):
        return is_hydroelectric_purpose(damid_to_purposes.get(dam_id))

    def same_owner(dam_id_a, dam_id_b) -> bool:
        """
        Compare Owner Names between two dams. NaN/missing owner on
        either side is treated as a mismatch, since shared ownership
        cannot be confirmed.
        """
        owner_a = damid_to_owner.get(dam_id_a)
        owner_b = damid_to_owner.get(dam_id_b)
        if pd.isna(owner_a) or pd.isna(owner_b):
            return False
        return owner_a == owner_b

    def build_full_chain(start_dam_id, require_same_owner: bool = REQUIRE_SAME_OWNER):
        """
        Follow the downstream chain starting at start_dam_id, continuing
        through dams of ANY purpose (hydroelectric or not).

        If require_same_owner is True, the chain additionally stops
        (excluding the mismatched dam entirely) as soon as a downstream
        dam's Owner Names differs from start_dam_id's Owner Names -- i.e.
        the cascade is defined as a single-owner system. If False,
        ownership is ignored and the chain only stops when no further
        downstream dam is recorded at all.

        A visited set guards defensively against a cyclical reference in
        the data, which should not occur given the underlying search is
        directional and distance-bounded.
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
                # Ownership diverges -- this dam is not part of the
                # root's system. Stop here without including it.
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
    print(f"True root dams (cascade starting points): {len(root_dams)}")

    cascades = [candidate_chains[root_id] for root_id in root_dams]

    cascades = [c for c in cascades if len(c) >= 2]

    def count_hydroelectric(chain):
        return sum(1 for dam_id in chain if is_hydroelectric(dam_id))

    cascades = [c for c in cascades if count_hydroelectric(c) >= MIN_HYDROELECTRIC_DAMS_PER_CASCADE]

    print(f"Cascades passing both filters (>=2 dams, >={MIN_HYDROELECTRIC_DAMS_PER_CASCADE} hydroelectric): {len(cascades)}")

    chain_lengths = pd.Series([len(c) for c in cascades])
    print(f"\nCascade length distribution (number of dams per cascade):")
    print(chain_lengths.value_counts().sort_index())

    max_length = max(len(c) for c in cascades)

    rows = []
    for chain in cascades:
        row = []
        for dam_id in chain:
            name = damid_to_name.get(dam_id, dam_id)
            purposes = damid_to_purposes.get(dam_id)
            row.extend([dam_id, name, purposes])
        remaining_positions = max_length - len(chain)
        row += [None, None, None] * remaining_positions
        rows.append(row)

    column_names = []
    for i in range(max_length):
        column_names.append(f'Dam {i + 1} ID')
        column_names.append(f'Dam {i + 1} Name')
        column_names.append(f'Dam {i + 1} Purposes')

    df_cascades = pd.DataFrame(rows, columns=column_names)
    df_cascades = df_cascades.sort_values('Dam 1 ID').reset_index(drop=True)

    df_cascades.to_csv(CASCADE_SYSTEMS_CSV, index=False)

    print(f"\nSaved {len(df_cascades)} cascading systems to {CASCADE_SYSTEMS_CSV}")
    print(f"Longest cascade: {max_length} dams")

def get_cascade_chain(root_dam_id, cascade_csv=CASCADE_SYSTEMS_CSV):
        """
        Look up the cascading system row for a given root dam ID (i.e. the
        value in 'Dam 1 ID') from the exported cascading_systems.csv, and
        return it as an ordered list of (dam_id, dam_name, purposes)
        triples, with trailing empty (padded) columns dropped.
        """
        df_cascades = pd.read_csv(cascade_csv, dtype=str)
        row = df_cascades[df_cascades['Dam 1 ID'] == root_dam_id]

        if row.empty:
            raise ValueError(
                f"No cascading system found with root dam ID '{root_dam_id}' in {cascade_csv}"
            )

        row = row.iloc[0]

        chain = []
        i = 1
        while f'Dam {i} ID' in row.index:
            dam_id = row[f'Dam {i} ID']
            dam_name = row[f'Dam {i} Name']
            dam_purposes = row.get(f'Dam {i} Purposes')
            if pd.isna(dam_id):
                break
            chain.append((dam_id, dam_name, dam_purposes))
            i += 1

        return chain


def get_segment_distance_miles(graph, start_node, end_node):
    """
    Compute the downstream river distance (in miles) between two specific
    graph nodes, by reusing find_downstream_dam with a target set
    containing ONLY end_node.
    """
    downstream_node, _path, distance = find_downstream_dam(
        graph, start_node, {end_node}, return_path=True
    )
    return distance


if os.path.exists(CASCADE_SYSTEMS_CSV):
    def visualize_cascade(root_dam_id, cascade_csv=CASCADE_SYSTEMS_CSV, output_dir='.', style='animated'):
        """
        Build an interactive folium map for the cascading system rooted at
        root_dam_id. Green = root, red = last/downstream-most dam, blue =
        intermediate HYDROELECTRIC dams, gray = intermediate
        NON-hydroelectric dams.

        Saves the map to '<output_dir>/cascade_<root_dam_id>.html'.
        """
        chain = get_cascade_chain(root_dam_id, cascade_csv)
        print(f"Cascade for {root_dam_id}: " +
                ' -> '.join(f'{name} ({did})' for did, name, _purposes in chain))

        damid_to_coords = dam_inventory_matched.set_index('NID ID')[['Latitude', 'Longitude']]
        damid_to_node_lookup = dam_inventory_matched.set_index('NID ID')['node_id']

        coords = []
        for dam_id, dam_name, _purposes in chain:
            if dam_id not in damid_to_coords.index:
                raise ValueError(
                    f"Dam '{dam_id}' ({dam_name}) not found in dam_inventory_matched -- "
                    f"cannot plot its location."
                )
            lat = damid_to_coords.loc[dam_id, 'Latitude']
            lon = damid_to_coords.loc[dam_id, 'Longitude']
            coords.append((lat, lon))

        lats = [c[0] for c in coords]
        lons = [c[1] for c in coords]
        center = [(min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2]

        m = folium.Map(location=center, zoom_start=8)

        for i, ((dam_id, dam_name, dam_purposes), (lat, lon)) in enumerate(zip(chain, coords), start=1):
            if i == 1:
                color = 'green'
            elif i == len(chain):
                color = 'red'
            elif is_hydroelectric_purpose(dam_purposes):
                color = 'blue'
            else:
                color = 'gray'

            hydro_label = "Hydroelectric" if is_hydroelectric_purpose(dam_purposes) else "Non-hydroelectric"

            folium.Marker(
                location=[lat, lon],
                popup=f"{i}. {dam_name} ({dam_id}) -- {hydro_label}<br>Purposes: {dam_purposes}",
                tooltip=f"{i}. {dam_name} ({hydro_label})",
                icon=folium.Icon(color=color, icon='tint', prefix='fa')
            ).add_to(m)

        for i in range(len(chain) - 1):
            dam_id_a, dam_name_a, _ = chain[i]
            dam_id_b, dam_name_b, _ = chain[i + 1]

            node_a = int(damid_to_node_lookup.loc[dam_id_a])
            node_b = int(damid_to_node_lookup.loc[dam_id_b])

            distance = get_segment_distance_miles(graph, node_a, node_b)
            distance_label = f"{distance:.2f} river miles" if distance is not None else "distance unavailable"

            if style == 'animated':
                AntPath(
                    locations=[coords[i], coords[i + 1]],
                    color='blue',
                    weight=3,
                    opacity=0.8,
                    delay=1000,
                    dash_array=[10, 20],
                    tooltip=f"{dam_name_a} -> {dam_name_b}: {distance_label}"
                ).add_to(m)
            elif style == 'static':
                line = folium.PolyLine(
                locations=[coords[i], coords[i + 1]],
                color='blue',
                weight=3,
                opacity=0.8,
                tooltip=f"{dam_name_a} -> {dam_name_b}: {distance_label}"
                ).add_to(m)

                PolyLineTextPath(
                    line,
                    '   \u25BA   ',
                    repeat=True,
                    offset=7,
                    attributes={
                        'fill': '#00008B',
                        'font-weight': 'bold',
                        'font-size': '16'
                    }
                ).add_to(m)

            # Add a small always-visible label marker at the segment midpoint
            # showing the distance, so it doesn't require hovering to see.
            midpoint = [
                (coords[i][0] + coords[i + 1][0]) / 2,
                (coords[i][1] + coords[i + 1][1]) / 2
            ]

            # icon_size/icon_anchor ensure this label is actually centered on
            # the true midpoint coordinate, rather than defaulting to
            # top-left anchoring (the same underlying bug fixed in
            # build_conus_cascade_map's arrow markers). Since the label's
            # rendered width varies with the distance text's character count
            # (e.g. "6.37 river miles" vs "100.00 river miles"), a fixed
            # icon_size can't perfectly match every label's actual width --
            # but anchoring at the horizontal center (half of a reasonable
            # fixed width) still centers it far more accurately than the
            # previous unanchored default, and any residual misalignment is
            # now a matter of a few pixels rather than being anchored from
            # the corner entirely.
            label_html = (
                '<div style="width:120px; text-align:center; font-size: 10pt; '
                'color: black; background-color: white; padding: 2px; '
                'border: 1px solid gray; border-radius: 3px; '
                'display: inline-block;">'
                f'{distance_label}</div>'
            )

            folium.map.Marker(
                midpoint,
                icon=folium.DivIcon(
                    html=label_html,
                    icon_size=(120, 20),
                    icon_anchor=(60, 10)
                )
            ).add_to(m)

        output_path = f"{output_dir}/cascade_{root_dam_id}.html"
        m.save(output_path)
        print(f"Saved cascade map to {output_path}")

        return m

    generate_maps = True  # Set to True to generate maps for all cascading systems
    if generate_maps:
        # --- Generate a folium map for every cascading system, organized ---
        # --- into a subfolder per state ---
        OUTPUT_MAPS_DIR = 'cascade_maps'
        os.makedirs(OUTPUT_MAPS_DIR, exist_ok=True)

        df_cascades = pd.read_csv(CASCADE_SYSTEMS_CSV, dtype=str)
        root_ids = df_cascades['Dam 1 ID'].tolist()

        print(f"Generating {len(root_ids)} cascade maps...")
        succeeded = 0
        failed = []

        for i, root_dam_id in enumerate(root_ids):
            # NID IDs are consistently prefixed with a 2-letter state code
            # (e.g. 'CO01675' -> 'CO'), which we use to route each map into
            # its own per-state subfolder for easier browsing.
            state_code = root_dam_id[:2].upper()
            state_dir = os.path.join(OUTPUT_MAPS_DIR, state_code)
            os.makedirs(state_dir, exist_ok=True)

            try:
                visualize_cascade(root_dam_id, output_dir=state_dir)
                succeeded += 1
            except Exception as e:
                # Catching broadly here is intentional: a single malformed or
                # unresolvable cascade should not halt map generation for the
                # remaining cascades. Each failure is logged with its root ID
                # and reason for follow-up.
                print(f"Failed to visualize cascade for root dam '{root_dam_id}': {e}")
                failed.append((root_dam_id, str(e)))

            if (i + 1) % 25 == 0 or (i + 1) == len(root_ids):
                print(f"Processed {i + 1}/{len(root_ids)} cascades...")

        print(f"\nDone. {succeeded} maps saved under '{OUTPUT_MAPS_DIR}/<STATE>/'.")
        if failed:
            print(f"{len(failed)} cascades failed to visualize:")
            for root_dam_id, error in failed:
                print(f"  {root_dam_id}: {error}")

CASCADE_GRAPHS_CACHE_FILE = 'cascade_graphs.pkl'

def build_cascade_graph(root_dam_id, cascade_csv=CASCADE_SYSTEMS_CSV):
    """
    Build a networkx DiGraph representing a single cascading system,
    rooted at root_dam_id. Nodes may be hydroelectric OR
    non-hydroelectric dams -- 'is_hydroelectric' distinguishes them.
    """
    chain = get_cascade_chain(root_dam_id, cascade_csv)

    G = nx.DiGraph()
    matched_indexed = dam_inventory_matched.set_index('NID ID')

    for dam_id, dam_name, dam_purposes in chain:
        if dam_id not in matched_indexed.index:
            raise ValueError(
                f"Dam '{dam_id}' ({dam_name}) not found in dam_inventory_matched -- "
                f"cannot build graph node."
            )
        dam_row = matched_indexed.loc[dam_id]

        G.add_node(
            dam_id,
            name=dam_name,
            latitude=dam_row['Latitude'],
            longitude=dam_row['Longitude'],
            purposes=dam_row['Purposes'],
            is_hydroelectric=is_hydroelectric_purpose(dam_row['Purposes'])
        )

    damid_to_node_lookup = dam_inventory_matched.set_index('NID ID')['node_id']

    for i in range(len(chain) - 1):
        dam_id_a, _, _ = chain[i]
        dam_id_b, _, _ = chain[i + 1]

        node_a = int(damid_to_node_lookup.loc[dam_id_a])
        node_b = int(damid_to_node_lookup.loc[dam_id_b])

        distance = get_segment_distance_miles(graph, node_a, node_b)

        G.add_edge(dam_id_a, dam_id_b, distance_miles=distance)

    return G


def build_all_cascade_graphs(cascade_csv=CASCADE_SYSTEMS_CSV):
    """
    Build a networkx DiGraph for every cascading system listed in
    cascade_csv, keyed by root dam ID (the value in 'Dam 1 ID').
    """
    df_cascades = pd.read_csv(cascade_csv, dtype=str)
    root_ids = df_cascades['Dam 1 ID'].tolist()

    cascade_graphs = {}
    total = len(root_ids)

    for i, root_id in enumerate(root_ids):
        cascade_graphs[root_id] = build_cascade_graph(root_id, cascade_csv)

        if (i + 1) % 25 == 0 or (i + 1) == total:
            print(f"Built {i + 1}/{total} cascade graphs...")

    return cascade_graphs


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

def print_cascade_graph(root_dam_id: str, cascade_graphs: dict) -> nx.DiGraph:
        """
        Print a formatted summary of the cascade graph rooted at root_dam_id,
        marking each node as [HYDRO] or [non-hydro] for readability.
        """
        if root_dam_id not in cascade_graphs:
            raise ValueError(
                f"No cascade found with root dam ID '{root_dam_id}' in cascade_graphs"
            )

        G = cascade_graphs[root_dam_id]

        print(f"\n{'='*60}")
        print(f"Cascade graph for root dam: {root_dam_id}")
        print(f"{'='*60}")
        print(f"Number of nodes (dams): {G.number_of_nodes()}")
        print(f"Number of edges (downstream links): {G.number_of_edges()}")
        hydro_count = sum(1 for _, attrs in G.nodes(data=True) if attrs['is_hydroelectric'])
        print(f"Hydroelectric dams in this cascade: {hydro_count}")

        print("\nNodes:")
        for node_id, attrs in G.nodes(data=True):
            tag = "[HYDRO]" if attrs['is_hydroelectric'] else "[non-hydro]"
            print(f"  {tag} {node_id}: {attrs['name']!r} "
                    f"(lat={attrs['latitude']}, lon={attrs['longitude']})")

        print("\nEdges (in traversal order):")
        ordered_nodes = list(nx.topological_sort(G))
        for u, v in zip(ordered_nodes[:-1], ordered_nodes[1:]):
            edge_attrs = G.edges[u, v]
            distance = edge_attrs['distance_miles']
            distance_str = f"{distance:.2f} miles" if distance is not None else "unavailable"
            print(f"  {u} ({G.nodes[u]['name']}) -> "
                    f"{v} ({G.nodes[v]['name']}): {distance_str}")

        return G

#print_cascade_graph(root_dam_id='CO01675', cascade_graphs=cascade_graphs)  # Replace with a valid root dam ID from your dataset to visualize its cascade graph.

CONUS_MAP_FILE = 'conus_cascade_map.html'

if not os.path.exists(CONUS_MAP_FILE):
    CASCADE_LINE_COLORS = [
    'blue', 'darkred', 'darkgreen', 'purple', 'orange',
    'darkblue', 'cadetblue', 'deeppink', 'black', 'darkorange'
    ]
    def build_conus_cascade_map(cascade_graphs, output_file=CONUS_MAP_FILE,
                                    show_distance_labels=False):
        """
        Build a single lightweight folium map showing every cascading system
        in cascade_graphs at once, each as its own toggleable layer.

        Performance notes (important at CONUS scale):
        - Uses CircleMarker instead of Icon markers -- a plain SVG circle is
            far cheaper for the browser to render than a full Leaflet icon
            marker (which loads an image/Font Awesome glyph and constructs a
            more complex DOM element per marker). At a few hundred markers,
            this difference in render cost becomes significant.
        - Uses a single small arrowhead marker at each segment's midpoint,
            rotated to match the segment's bearing, instead of
            PolyLineTextPath. 
        """
        import math

        all_lats, all_lons = [], []
        for G in cascade_graphs.values():
            for _, attrs in G.nodes(data=True):
                all_lats.append(attrs['latitude'])
                all_lons.append(attrs['longitude'])

        center = [(min(all_lats) + max(all_lats)) / 2, (min(all_lons) + max(all_lons)) / 2]

        m = folium.Map(location=center, zoom_start=5, tiles='cartodbpositron')

        color_cycle = itertools.cycle(CASCADE_LINE_COLORS)

        def bearing_degrees(lat1, lon1, lat2, lon2):
            """Compute compass bearing (degrees, 0=north) from point 1 to point 2."""
            lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
            delta_lon = math.radians(lon2 - lon1)
            x = math.sin(delta_lon) * math.cos(lat2_r)
            y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(delta_lon)
            return (math.degrees(math.atan2(x, y)) + 360) % 360

        for root_dam_id, G in cascade_graphs.items():
            line_color = next(color_cycle)
            root_name = G.nodes[root_dam_id]['name']
            fg = folium.FeatureGroup(name=f"{root_name} ({root_dam_id})", show=True)

            ordered_nodes = list(nx.topological_sort(G))
            node_coords = {
                node_id: (attrs['latitude'], attrs['longitude'])
                for node_id, attrs in G.nodes(data=True)
            }

            # --- Markers (lightweight CircleMarker instead of Icon) ---
            for i, node_id in enumerate(ordered_nodes, start=1):
                attrs = G.nodes[node_id]
                lat, lon = node_coords[node_id]

                if i == 1:
                    color = 'green'
                elif i == len(ordered_nodes):
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
                    popup=f"{i}. {attrs['name']} ({node_id}) -- {hydro_label}"
                            f"<br>Cascade root: {root_name} ({root_dam_id})"
                            f"<br>Purposes: {attrs['purposes']}",
                    tooltip=f"{attrs['name']} ({hydro_label})"
                ).add_to(fg)

            # --- Edges: plain line + ONE arrowhead at midpoint, not
            # --- repeated glyphs along the whole path ---
            for u, v in zip(ordered_nodes[:-1], ordered_nodes[1:]):
                edge_attrs = G.edges[u, v]
                distance = edge_attrs['distance_miles']
                distance_label = f"{distance:.2f} river miles" if distance is not None else "distance unavailable"
                u_name = G.nodes[u]['name']
                v_name = G.nodes[v]['name']

                loc_u, loc_v = node_coords[u], node_coords[v]

                folium.PolyLine(
                    locations=[loc_u, loc_v],
                    color=line_color,
                    weight=2,
                    opacity=0.7,
                    tooltip=f"{u_name} -> {v_name}: {distance_label}"
                ).add_to(fg)

                midpoint = [(loc_u[0] + loc_v[0]) / 2, (loc_u[1] + loc_v[1]) / 2]
                angle = bearing_degrees(loc_u[0], loc_u[1], loc_v[0], loc_v[1])

                # Fixed-size container (20x20) with the glyph centered inside
                # via flexbox BEFORE rotation is applied. icon_anchor is set
                # to half of icon_size (10, 10), which tells Leaflet to place
                # the CENTER of this div at `midpoint`, rather than its
                # default top-left corner -- this is what was causing the
                # arrows to appear offset from the actual segment midpoint.
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
                            )
                        )
                    ).add_to(fg)

            fg.add_to(m)

        folium.LayerControl(collapsed=True).add_to(m)

        m.save(output_file)
        print(f"Saved CONUS cascade map with {len(cascade_graphs)} cascades to {output_file}")

        return m

    print(f"Building CONUS cascade map for all cascading systems...")
    build_conus_cascade_map(cascade_graphs, output_file=CONUS_MAP_FILE)
    print(f"Saved CONUS cascade map to {CONUS_MAP_FILE}")