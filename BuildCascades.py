import pandas as pd

INPUT_CSV = 'downstream_dams_filtered_sorted_resnet.csv'
OUTPUT_CSV = 'cascading_systems.csv'

# Force string dtype on ID columns to avoid pandas silently mixing
# str/float types on columns containing NaN alongside ID strings.
df = pd.read_csv(INPUT_CSV, dtype={
    'Hydroelectric Dam': str,
    'Downstream Dam': str
})

# Lookup: dam ID -> downstream dam ID
downstream_map = dict(zip(df['Hydroelectric Dam'], df['Downstream Dam']))

# Lookup: dam ID -> dam name, for exporting human-readable names alongside
# IDs. Also captures names for downstream dams that only ever appear in
# the 'Downstream Dam' column (e.g. non-hydroelectric terminal dams),
# as a defensive fallback in case a dam never appears as a root elsewhere.
damid_to_name = dict(zip(df['Hydroelectric Dam'], df['Hydroelectric Dam Name']))
downstream_name_map = dict(zip(df['Downstream Dam'], df['Downstream Dam Name']))
for damid, name in downstream_name_map.items():
    if pd.notna(damid):
        damid_to_name.setdefault(damid, name)

# The set of dam IDs that are hydroelectric dams in this dataset -- a
# downstream dam only continues a cascade (i.e. gets its own downstream
# looked up in turn) if it's also in this set.
hydro_dam_ids = set(df['Hydroelectric Dam'])

# Dams that are the downstream target of another hydroelectric dam,
# counting only links where that downstream dam is ALSO hydroelectric.
# These are excluded from being treated as roots, since they are already
# part of another dam's chain -- this is what prevents a chain like
# A->B->C from also being separately listed as a standalone B->C chain.
downstream_targets = {
    downstream for downstream in downstream_map.values()
    if pd.notna(downstream) and downstream in hydro_dam_ids
}

# Root dams: hydroelectric dams that are NOT downstream of any other
# hydroelectric dam -- these are the starting points of each distinct
# cascading system. A dam like B in A->B->C is excluded here since it's
# in downstream_targets, but B can still appear as part of A's chain,
# or as part of a different root's chain (e.g. D->B->C), if applicable.
root_dams = [damid for damid in df['Hydroelectric Dam'] if damid not in downstream_targets]

print(f"Total hydroelectric dams: {len(hydro_dam_ids)}")
print(f"Dams that are downstream of another hydroelectric dam: {len(downstream_targets)}")
print(f"Root dams (cascade starting points): {len(root_dams)}")


def build_chain(start_dam_id):
    """
    Follow the downstream chain starting at start_dam_id.

    - If a downstream dam is found at all (hydroelectric or not), it is
        always appended to the chain as the terminal entry.
    - The chain continues to look further downstream ONLY if that
        downstream dam is itself hydroelectric and present in this dataset.
    - The chain stops growing (without adding anything further) once a
        dam has no downstream dam recorded, or once a non-hydroelectric
        downstream dam has been added as the terminal entry.

    A visited set guards defensively against a cyclical reference in the
    data, which should not occur given the underlying search is
    directional and distance-bounded.
    """
    chain = [start_dam_id]
    visited = {start_dam_id}
    current = start_dam_id

    while True:
        downstream = downstream_map.get(current)

        # No downstream dam recorded at all
        if pd.isna(downstream):
            break

        # Defensive cycle guard. This should never realistically happen but a faulty dataset could cause an issue.
        if downstream in visited:
            print(f"Warning: cycle detected involving dam {downstream}, stopping chain early.")
            break

        # A downstream dam was found
        chain.append(downstream)
        visited.add(downstream)

        # Only continue the search further downstream if this dam is
        # itself hydroelectric and in our dataset; otherwise this is a
        # terminal (non-hydroelectric) dam and the chain stops here.
        if downstream not in hydro_dam_ids:
            break

        current = downstream

    return chain


# Build one chain per root dam -- these are the distinct cascading systems.
cascades = [build_chain(root) for root in root_dams]

# Only keep cascades with 2 or more dams (i.e. an actual downstream dam
# was found; a lone root with no downstream at all is not a cascade).
cascades = [c for c in cascades if len(c) >= 2]

# Additional rule: a cascade of EXACTLY 2 dams is only meaningful if both
# dams are hydroelectric. For a chain of 2, chain[0] is always the root
# (already known to be hydroelectric, since root_dams is drawn from the
# 'Hydroelectric Dam' column), so we only need to check chain[1]. This
# does not affect cascades of length 3+, since every dam prior to the
# last is already guaranteed hydroelectric by build_chain's continuation
# logic -- only the very last dam in any chain can ever be non-hydro.
def is_valid_cascade(chain):
    if len(chain) == 2:
        return chain[1] in hydro_dam_ids
    return True

cascades = [c for c in cascades if is_valid_cascade(c)]

print(f"Cascades with 2 or more dams (after 2-dam hydro-only filter): {len(cascades)}")

# Quick sanity-check summary of chain lengths
chain_lengths = pd.Series([len(c) for c in cascades])
print(f"\nCascade length distribution (number of dams per cascade):")
print(chain_lengths.value_counts().sort_index())

# --- Export to CSV ---
# Each row is a cascading system, ordered upstream-to-downstream, with
# both an ID and a Name column for every dam position in the chain.
# Shorter rows are padded with blanks so every row has the same number
# of columns, matching the longest cascade found.
max_length = max(len(c) for c in cascades)

rows = []
for chain in cascades:
    row = []
    for damid in chain:
        row.append(damid)
        row.append(damid_to_name.get(damid))
    # Pad remaining (ID, Name) column pairs with blanks for shorter chains
    remaining_positions = max_length - len(chain)
    row += [None, None] * remaining_positions
    rows.append(row)

column_names = []
for i in range(max_length):
    column_names.append(f'Dam {i + 1} ID')
    column_names.append(f'Dam {i + 1} Name')

df_cascades = pd.DataFrame(rows, columns=column_names)

# Sort alphanumerically by the root dam's NID ID (i.e. the first dam's
# ID column in each cascade).
df_cascades = df_cascades.sort_values('Dam 1 ID').reset_index(drop=True)

df_cascades.to_csv(OUTPUT_CSV, index=False)

print(f"\nSaved {len(df_cascades)} cascading systems to {OUTPUT_CSV}")
print(f"Longest cascade: {max_length} dams")