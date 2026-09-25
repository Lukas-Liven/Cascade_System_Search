"""
Folium map-generation service for cascade-system visualizations.

This module creates local interactive HTML maps from already constructed
NetworkX cascade graphs. It deliberately does not depend on Tkinter, browser
launching, dialogs, or application state.

Security controls:
- External NID-derived values are HTML-escaped before entering popup, tooltip,
  layer-name, and distance-label HTML contexts.
- Map files are saved through a temporary file and atomically replaced, so an
  interrupted write is not presented as a completed map.
"""

from __future__ import annotations

import html
import itertools
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import folium
import networkx as nx

from cascade_research_tool.constants import (
    CASCADE_LINE_COLORS,
    CONUS_CASCADE_MAP_FILENAME,
    USGS_TOPO_ATTRIBUTION,
    USGS_TOPO_TILE_URL,
)
from cascade_research_tool.utilities.map_helpers import (
    bearing_degrees,
    display_value,
    get_valid_map_coordinate,
)


@dataclass(frozen=True)
class MapGenerationResult:
    """
    Result metadata returned after a map has been successfully generated.

    The calling UI/controller is responsible for displaying messages, writing
    logs, updating status text, and deciding whether to open output_file in a
    browser.
    """

    output_file: Path
    mapped_system_count: int
    mapped_node_count: int
    skipped_node_count: int
    skipped_edge_count: int


def _configure_usgs_topo_basemap(
    cascade_map: folium.Map,
) -> None:
    """
    Add the USGS National Map topographic tile layer to a Folium map.

    Generated maps are opened as local file:// documents. The USGS layer avoids
    reliance on public OSM standard tiles, whose usage policy can reject local
    unauthenticated browser tile requests.
    """

    folium.TileLayer(
        tiles=USGS_TOPO_TILE_URL,
        attr=USGS_TOPO_ATTRIBUTION,
        name="USGS Topo",
        overlay=False,
        control=False,
    ).add_to(cascade_map)


def _save_map_atomically(
    cascade_map: folium.Map,
    output_file: Path,
) -> None:
    """
    Save a Folium document through a temporary file and atomic replacement.

    The output file remains unavailable as a completed map until Folium has
    finished writing the entire HTML document successfully.
    """

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if os.name != "nt":
        os.chmod(output_file.parent, 0o700)

    temporary_file = output_file.with_suffix(
        output_file.suffix + ".tmp"
    )

    try:
        cascade_map.save(str(temporary_file))

        if os.name != "nt":
            os.chmod(temporary_file, 0o600)

        os.replace(
            temporary_file,
            output_file,
        )

    finally:
        temporary_file.unlink(missing_ok=True)


def _get_graph_coordinates(
    cascade_graph: nx.DiGraph,
) -> dict[str, tuple[float, float]]:
    """
    Return valid latitude/longitude coordinates keyed by string dam ID.

    Invalid source coordinates are omitted rather than allowing one malformed
    NID record to prevent otherwise valid system mapping.
    """

    coordinates_by_dam: dict[str, tuple[float, float]] = {}

    for dam_id, attributes in cascade_graph.nodes(data=True):
        latitude = get_valid_map_coordinate(
            attributes.get("Latitude"),
            -90.0,
            90.0,
        )
        longitude = get_valid_map_coordinate(
            attributes.get("Longitude"),
            -180.0,
            180.0,
        )

        if latitude is not None and longitude is not None:
            coordinates_by_dam[str(dam_id)] = (
                latitude,
                longitude,
            )

    return coordinates_by_dam


def _get_graph_roles(
    cascade_graph: nx.DiGraph,
) -> tuple[set[str], set[str]]:
    """
    Return string-normalized root and terminal dam IDs for one cascade graph.
    """

    root_dams = {
        str(dam_id)
        for dam_id in cascade_graph.nodes
        if cascade_graph.in_degree(dam_id) == 0
    }

    terminal_dams = {
        str(dam_id)
        for dam_id in cascade_graph.nodes
        if cascade_graph.out_degree(dam_id) == 0
    }

    return root_dams, terminal_dams


def _get_marker_style(
    dam_id: str,
    attributes: dict[str, Any],
    root_dams: set[str],
    terminal_dams: set[str],
) -> tuple[str, str]:
    """
    Determine a marker color and human-readable graph-role label.
    """

    if dam_id in root_dams:
        return "green", "Root dam"

    if dam_id in terminal_dams:
        return "red", "Terminal dam"

    if bool(attributes.get("is_hydroelectric", False)):
        return "blue", "Intermediate hydroelectric dam"

    return "gray", "Intermediate non-hydroelectric dam"


def _build_dam_popup_html(
    dam_id: str,
    system_id: str,
    dam_name: str,
    role_label: str,
    hydroelectric_label: str,
    owner: str,
    purposes: str,
) -> str:
    """
    Build escaped popup HTML for a dam marker.

    Source values originate from NID-derived data and must not be trusted as
    safe HTML. Every dynamic value is output-encoded before interpolation.
    """

    return (
        "<div style='min-width:260px;'>"
        f"<strong>{html.escape(dam_name)}</strong><br>"
        f"<strong>NID ID:</strong> {html.escape(dam_id)}<br>"
        f"<strong>System:</strong> {html.escape(system_id)}<br>"
        f"<strong>Role:</strong> {html.escape(role_label)}<br>"
        f"<strong>Type:</strong> {html.escape(hydroelectric_label)}<br>"
        f"<strong>Owner:</strong> {html.escape(owner)}<br>"
        f"<strong>Purposes:</strong> {html.escape(purposes)}"
        "</div>"
    )


def _build_marker_tooltip(
    dam_name: str,
    hydroelectric_label: str,
    role_label: str,
) -> str:
    """
    Build escaped marker tooltip content.

    Folium/Leaflet tooltips can render HTML, so source-derived values are
    escaped even though their intended presentation is plain text.
    """

    return html.escape(
        f"{dam_name} ({hydroelectric_label}; {role_label})"
    )


def _build_edge_tooltip(
    upstream_name: str,
    downstream_name: str,
    distance_label: str,
) -> str:
    """
    Build escaped direct-downstream edge tooltip content.
    """

    return html.escape(
        f"{upstream_name} → {downstream_name}: {distance_label}"
    )


def _get_distance_label(
    edge_attributes: dict[str, Any],
) -> str:
    """
    Return a user-readable river-distance label for one cascade graph edge.
    """

    raw_distance = edge_attributes.get("distance_miles")

    try:
        if raw_distance is not None:
            return f"{float(raw_distance):.2f} river miles"
    except (TypeError, ValueError):
        pass

    return "distance unavailable"


def _add_directional_edge(
    feature_group: folium.FeatureGroup | folium.Map,
    upstream_coordinate: tuple[float, float],
    downstream_coordinate: tuple[float, float],
    line_color: str,
    line_weight: int,
    line_opacity: float,
    edge_tooltip: str,
    distance_label: str,
    show_distance_label: bool,
) -> None:
    """
    Draw a direct downstream line, directional midpoint arrow, and optional
    distance label on the supplied Folium map layer.
    """

    folium.PolyLine(
        locations=[
            upstream_coordinate,
            downstream_coordinate,
        ],
        color=line_color,
        weight=line_weight,
        opacity=line_opacity,
        tooltip=edge_tooltip,
    ).add_to(feature_group)

    midpoint = [
        (
            upstream_coordinate[0]
            + downstream_coordinate[0]
        ) / 2,
        (
            upstream_coordinate[1]
            + downstream_coordinate[1]
        ) / 2,
    ]

    flow_bearing = bearing_degrees(
        upstream_coordinate[0],
        upstream_coordinate[1],
        downstream_coordinate[0],
        downstream_coordinate[1],
    )

    # The triangle entity points upward/north by default. Rotating it by the
    # calculated compass bearing indicates the upstream-to-downstream flow.
    arrow_html = (
        '<div style="width:20px; height:20px; display:flex; '
        'align-items:center; justify-content:center; '
        f'transform:rotate({flow_bearing}deg); '
        'transform-origin:center center;">'
        f'<span style="font-size:16px; color:{html.escape(line_color)};">'
        "&#9650;</span>"
        "</div>"
    )

    folium.Marker(
        location=midpoint,
        icon=folium.DivIcon(
            html=arrow_html,
            icon_size=(20, 20),
            icon_anchor=(10, 10),
        ),
        tooltip=edge_tooltip,
    ).add_to(feature_group)

    if show_distance_label:
        distance_label_html = (
            '<div style="font-size:9pt; color:black; '
            "background-color:white; padding:1px; "
            "border:1px solid gray; border-radius:3px; "
            'margin-top:14px;">'
            f"{html.escape(distance_label)}"
            "</div>"
        )

        folium.Marker(
            location=midpoint,
            icon=folium.DivIcon(
                html=distance_label_html,
                icon_size=(0, 0),
            ),
        ).add_to(feature_group)


def _add_map_legend(
    cascade_map: folium.Map,
) -> None:
    """
    Add a fixed legend explaining dam marker colors.
    """

    legend_html = (
        "<div style='position:fixed; bottom:28px; left:28px; "
        "z-index:9999; background-color:white; border:1px solid gray; "
        "border-radius:4px; padding:8px; font-size:12px;'>"
        "<strong>Cascade System Legend</strong><br>"
        "<span style='color:green;'>&#9679;</span> Root dam<br>"
        "<span style='color:red;'>&#9679;</span> Terminal dam<br>"
        "<span style='color:blue;'>&#9679;</span> "
        "Intermediate hydroelectric dam<br>"
        "<span style='color:gray;'>&#9679;</span> "
        "Intermediate non-hydroelectric dam"
        "</div>"
    )

    cascade_map.get_root().html.add_child(
        folium.Element(legend_html)
    )


def build_selected_cascade_map(
    system_id: str,
    cascade_graph: nx.DiGraph,
    maps_directory: Path,
) -> MapGenerationResult:
    """
    Build and save an interactive map for one selected cascade system.

    Returns:
        MapGenerationResult containing output location and skipped coordinate
        statistics.

    Raises:
        ValueError:
            If no graph node has valid map coordinates.
    """

    coordinates_by_dam = _get_graph_coordinates(cascade_graph)

    if not coordinates_by_dam:
        raise ValueError(
            f"System {system_id} has no valid dam coordinates available "
            "for mapping."
        )

    root_dams, terminal_dams = _get_graph_roles(cascade_graph)

    latitude_values = [
        latitude
        for latitude, _ in coordinates_by_dam.values()
    ]
    longitude_values = [
        longitude
        for _, longitude in coordinates_by_dam.values()
    ]

    map_center = [
        sum(latitude_values) / len(latitude_values),
        sum(longitude_values) / len(longitude_values),
    ]

    cascade_map = folium.Map(
        location=map_center,
        zoom_start=8,
        tiles=None,
        control_scale=True,
    )
    _configure_usgs_topo_basemap(cascade_map)

    for dam_id, attributes in cascade_graph.nodes(data=True):
        normalized_dam_id = str(dam_id)
        coordinate = coordinates_by_dam.get(normalized_dam_id)

        if coordinate is None:
            continue

        marker_color, role_label = _get_marker_style(
            normalized_dam_id,
            attributes,
            root_dams,
            terminal_dams,
        )

        dam_name = display_value(attributes.get("name"))
        owner = display_value(attributes.get("owner"))
        purposes = display_value(attributes.get("purposes"))
        hydroelectric_label = (
            "Hydroelectric"
            if bool(attributes.get("is_hydroelectric", False))
            else "Non-hydroelectric"
        )

        popup_html = _build_dam_popup_html(
            dam_id=normalized_dam_id,
            system_id=system_id,
            dam_name=dam_name,
            role_label=role_label,
            hydroelectric_label=hydroelectric_label,
            owner=owner,
            purposes=purposes,
        )

        folium.Marker(
            location=coordinate,
            popup=folium.Popup(
                popup_html,
                max_width=350,
            ),
            tooltip=_build_marker_tooltip(
                dam_name,
                hydroelectric_label,
                role_label,
            ),
            icon=folium.Icon(
                color=marker_color,
                icon="tint",
                prefix="fa",
            ),
        ).add_to(cascade_map)

    skipped_edge_count = 0

    for upstream_dam_id, downstream_dam_id, edge_attributes in (
        cascade_graph.edges(data=True)
    ):
        upstream_id = str(upstream_dam_id)
        downstream_id = str(downstream_dam_id)

        upstream_coordinate = coordinates_by_dam.get(upstream_id)
        downstream_coordinate = coordinates_by_dam.get(downstream_id)

        if (
            upstream_coordinate is None
            or downstream_coordinate is None
        ):
            skipped_edge_count += 1
            continue

        upstream_name = display_value(
            cascade_graph.nodes[upstream_dam_id].get("name")
        )
        downstream_name = display_value(
            cascade_graph.nodes[downstream_dam_id].get("name")
        )
        distance_label = _get_distance_label(edge_attributes)

        _add_directional_edge(
            feature_group=cascade_map,
            upstream_coordinate=upstream_coordinate,
            downstream_coordinate=downstream_coordinate,
            line_color="#1f5aa6",
            line_weight=3,
            line_opacity=0.8,
            edge_tooltip=_build_edge_tooltip(
                upstream_name,
                downstream_name,
                distance_label,
            ),
            distance_label=distance_label,
            show_distance_label=False,
        )

    _add_map_legend(cascade_map)

    safe_system_id = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        system_id,
    )

    output_file = maps_directory / f"cascade_{safe_system_id}.html"

    _save_map_atomically(
        cascade_map,
        output_file,
    )

    return MapGenerationResult(
        output_file=output_file,
        mapped_system_count=1,
        mapped_node_count=len(coordinates_by_dam),
        skipped_node_count=(
            cascade_graph.number_of_nodes()
            - len(coordinates_by_dam)
        ),
        skipped_edge_count=skipped_edge_count,
    )


def build_query_results_overview_map(
    cascade_graphs: dict[str, nx.DiGraph],
    selected_system_ids: list[str],
    maps_directory: Path,
    show_distance_labels: bool,
) -> MapGenerationResult:
    """
    Build and save a CONUS overview map for selected Part 6 query results.

    Each cascade system has its own toggleable Folium FeatureGroup. Line colors
    distinguish systems while marker colors continue to represent graph role.
    """

    selected_graphs = {
        system_id: cascade_graphs[system_id]
        for system_id in selected_system_ids
        if system_id in cascade_graphs
    }

    if not selected_graphs:
        raise ValueError(
            "The systems from the query result are no longer available."
        )

    coordinates_by_system: dict[
        str,
        dict[str, tuple[float, float]],
    ] = {}

    all_latitudes: list[float] = []
    all_longitudes: list[float] = []

    for system_id, cascade_graph in selected_graphs.items():
        coordinates = _get_graph_coordinates(cascade_graph)

        coordinates_by_system[system_id] = coordinates

        for latitude, longitude in coordinates.values():
            all_latitudes.append(latitude)
            all_longitudes.append(longitude)

    if not all_latitudes or not all_longitudes:
        raise ValueError(
            "No valid dam coordinates were available in the query results."
        )

    map_center = [
        (min(all_latitudes) + max(all_latitudes)) / 2,
        (min(all_longitudes) + max(all_longitudes)) / 2,
    ]

    cascade_map = folium.Map(
        location=map_center,
        zoom_start=5,
        tiles=None,
        control_scale=True,
    )
    _configure_usgs_topo_basemap(cascade_map)

    line_color_cycle = itertools.cycle(CASCADE_LINE_COLORS)
    skipped_node_count = 0
    skipped_edge_count = 0
    mapped_node_count = 0

    for system_id in sorted(selected_graphs):
        cascade_graph = selected_graphs[system_id]
        coordinates = coordinates_by_system[system_id]
        line_color = next(line_color_cycle)

        root_dams, terminal_dams = _get_graph_roles(cascade_graph)

        root_names = ", ".join(
            display_value(
                cascade_graph.nodes[dam_id].get("name")
            )
            for dam_id in sorted(root_dams)
            if dam_id in cascade_graph.nodes
        )

        # Feature-group names are HTML-rendered by Leaflet controls. Escape all
        # source-derived fields before constructing the visible layer label.
        safe_layer_name = html.escape(
            f"{root_names} ({system_id})"
        )

        feature_group = folium.FeatureGroup(
            name=safe_layer_name,
            show=True,
        )

        for dam_id, attributes in cascade_graph.nodes(data=True):
            normalized_dam_id = str(dam_id)
            coordinate = coordinates.get(normalized_dam_id)

            if coordinate is None:
                skipped_node_count += 1
                continue

            mapped_node_count += 1

            marker_color, role_label = _get_marker_style(
                normalized_dam_id,
                attributes,
                root_dams,
                terminal_dams,
            )

            dam_name = display_value(attributes.get("name"))
            owner = display_value(attributes.get("owner"))
            purposes = display_value(attributes.get("purposes"))
            hydroelectric_label = (
                "Hydroelectric"
                if bool(attributes.get("is_hydroelectric", False))
                else "Non-hydroelectric"
            )

            popup_html = _build_dam_popup_html(
                dam_id=normalized_dam_id,
                system_id=system_id,
                dam_name=dam_name,
                role_label=role_label,
                hydroelectric_label=hydroelectric_label,
                owner=owner,
                purposes=purposes,
            )

            folium.CircleMarker(
                location=coordinate,
                radius=5,
                color=marker_color,
                fill=True,
                fill_color=marker_color,
                fill_opacity=0.9,
                weight=1,
                popup=folium.Popup(
                    popup_html,
                    max_width=350,
                ),
                tooltip=_build_marker_tooltip(
                    dam_name,
                    hydroelectric_label,
                    role_label,
                ),
            ).add_to(feature_group)

        for upstream_dam_id, downstream_dam_id, edge_attributes in (
            cascade_graph.edges(data=True)
        ):
            upstream_id = str(upstream_dam_id)
            downstream_id = str(downstream_dam_id)

            upstream_coordinate = coordinates.get(upstream_id)
            downstream_coordinate = coordinates.get(downstream_id)

            if (
                upstream_coordinate is None
                or downstream_coordinate is None
            ):
                skipped_edge_count += 1
                continue

            upstream_name = display_value(
                cascade_graph.nodes[upstream_dam_id].get("name")
            )
            downstream_name = display_value(
                cascade_graph.nodes[downstream_dam_id].get("name")
            )
            distance_label = _get_distance_label(edge_attributes)

            _add_directional_edge(
                feature_group=feature_group,
                upstream_coordinate=upstream_coordinate,
                downstream_coordinate=downstream_coordinate,
                line_color=line_color,
                line_weight=2,
                line_opacity=0.7,
                edge_tooltip=_build_edge_tooltip(
                    upstream_name,
                    downstream_name,
                    distance_label,
                ),
                distance_label=distance_label,
                show_distance_label=show_distance_labels,
            )

        feature_group.add_to(cascade_map)

    folium.LayerControl(
        collapsed=True,
    ).add_to(cascade_map)

    _add_map_legend(cascade_map)

    output_file = maps_directory / CONUS_CASCADE_MAP_FILENAME

    _save_map_atomically(
        cascade_map,
        output_file,
    )

    return MapGenerationResult(
        output_file=output_file,
        mapped_system_count=len(selected_graphs),
        mapped_node_count=mapped_node_count,
        skipped_node_count=skipped_node_count,
        skipped_edge_count=skipped_edge_count,
    )