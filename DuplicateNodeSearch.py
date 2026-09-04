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

def find_and_display_multi_dam_nodes() -> pd.DataFrame:
        """
        Identify any node_id in dam_inventory_matched that has more than one
        associated dam, and print full details for each via
        explore_node_dams().

        This is a re-check specifically motivated by the 'NID ID ==
        Federal ID' primary-dam filter added earlier: that filter was meant
        to eliminate most cases of multiple structures collapsing onto the
        same river node, but it is not guaranteed to be exhaustive (e.g.
        two entirely separate dams that happen to snap to the same COMID/
        node due to coordinate proximity or GeoConnex/ResNet crosswalk
        granularity). This matters because node_to_damid is built via
        set_index(...).to_dict(), which silently keeps only the LAST dam
        for any node_id with duplicates -- any node found here represents
        a place where that dict is discarding a dam without warning.

        Returns:
        A DataFrame of the node_ids with more than one dam, and their
        respective counts, for further inspection if needed.
        """
        node_counts = dam_inventory_matched['node_id'].value_counts()
        multi_dam_node_ids = node_counts[node_counts > 1]

        print(f"Total matched dams: {len(dam_inventory_matched)}")
        print(f"Total distinct nodes: {dam_inventory_matched['node_id'].nunique()}")
        print(f"Nodes with more than one dam: {len(multi_dam_node_ids)}")

        if len(multi_dam_node_ids) == 0:
            print("No nodes with multiple dams found -- node_to_damid is safe "
                  "from silent overwrites for the current dataset.")
            return pd.DataFrame(columns=['node_id', 'dam_count'])

        print(f"\n{'!'*60}")
        print(f"WARNING: {len(multi_dam_node_ids)} node(s) have multiple dams. "
              f"node_to_damid will only retain ONE dam per node below.")
        print(f"{'!'*60}")

        #for node_id, count in multi_dam_node_ids.items():
            #explore_node_dams(node_id)

        return multi_dam_node_ids.rename('dam_count').rename_axis('node_id').reset_index()


def find_multi_dam_nodes_in_cascades(cascade_graphs: dict) -> pd.DataFrame:
        """
        Across every dam appearing in ANY cascading system in cascade_graphs,
        check whether more than one of those dams maps to the same river
        node_id. This is the same underlying concern as
        find_and_display_multi_dam_nodes() (node_to_damid's silent
        last-write-wins behavior), but scoped specifically to dams that
        actually made it into the final cascading systems -- i.e. the exact
        set of dams reflected in cascading_systems.csv and the visualized
        maps, rather than the full dam_inventory_matched population.

        A collision here would mean two DIFFERENT dam IDs, both present in
        (possibly different) cascading systems, share the same underlying
        river node -- which is worth knowing about since it means the
        river-network search treats them as interchangeable at that point,
        even though the cascade CSV/graphs record them as distinct dams.

        Returns:
        A DataFrame with columns ['node_id', 'dam_count', 'dam_ids',
        'system_ids'] for every colliding node_id found, empty if none.
        """
        # Collect every distinct dam ID that appears in any cascade, along
        # with which system(s) it belongs to (a dam could theoretically
        # appear in more than one system if it's a shared downstream point,
        # though the union-find merge step should have already consolidated
        # any such overlap into a single system).
        dam_to_systems = {}
        for system_id, G in cascade_graphs.items():
            for dam_id in G.nodes:
                dam_to_systems.setdefault(dam_id, set()).add(system_id)

        all_cascade_dam_ids = list(dam_to_systems.keys())
        print(f"Total distinct dams appearing across all cascading systems: {len(all_cascade_dam_ids)}")

        # Map each dam ID to its river node_id via dam_inventory_matched --
        # the same source used originally to build node_to_damid.
        damid_to_node_lookup = dam_inventory_matched.set_index('NID ID')['node_id']

        missing_from_inventory = [d for d in all_cascade_dam_ids if d not in damid_to_node_lookup.index]
        if missing_from_inventory:
            print(f"Warning: {len(missing_from_inventory)} dam(s) in cascade_graphs "
                  f"were not found in dam_inventory_matched: {missing_from_inventory}")

        node_to_dams = {}
        for dam_id in all_cascade_dam_ids:
            if dam_id not in damid_to_node_lookup.index:
                continue
            node_id = damid_to_node_lookup.loc[dam_id]
            node_to_dams.setdefault(node_id, []).append(dam_id)

        collisions = {node_id: dams for node_id, dams in node_to_dams.items() if len(dams) > 1}

        print(f"Distinct river nodes represented across all cascades: {len(node_to_dams)}")
        print(f"Nodes with more than one dam among cascade dams: {len(collisions)}")

        if not collisions:
            print("No colliding nodes found among dams present in the cascading systems.")
            return pd.DataFrame(columns=['node_id', 'dam_count', 'dam_ids', 'system_ids'])

        print(f"\n{'!'*60}")
        print(f"WARNING: {len(collisions)} river node(s) are shared by multiple "
              f"dams that appear in the cascading systems.")
        print(f"{'!'*60}")

        rows = []
        for node_id, dam_ids in collisions.items():
            systems_involved = sorted(set().union(*(dam_to_systems[d] for d in dam_ids)))

            print(f"\n{'='*60}")
            print(f"Node ID: {node_id}")
            print(f"Dams at this node: {len(dam_ids)}")
            print(f"System(s) involved: {', '.join(systems_involved)}")
            print(f"{'='*60}")
            for dam_id in dam_ids:
                dam_row = dam_inventory_matched.set_index('NID ID').loc[dam_id]
                print(f"  Dam: {dam_row['Dam Name']} ({dam_id})")
                print(f"    Purposes: {dam_row['Purposes']}")
                print(f"    Owner: {dam_row['Owner Names']}")
                print(f"    In system(s): {', '.join(sorted(dam_to_systems[dam_id]))}")

            rows.append({
                'node_id': node_id,
                'dam_count': len(dam_ids),
                'dam_ids': '; '.join(dam_ids),
                'system_ids': '; '.join(systems_involved)
            })

        return pd.DataFrame(rows, columns=['node_id', 'dam_count', 'dam_ids', 'system_ids'])