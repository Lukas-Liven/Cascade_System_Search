"""
Application-managed cache and artifact persistence services.

This module contains the controlled persistence boundary for the Cascade
Research Tool. Pickle files are used only for application-created artifacts
inside the private user-local cache directory. Never use these functions to
load arbitrary user-selected pickle files.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import time
from pathlib import Path
from typing import Any

import pandas as pd

from cascade_research_tool.constants import (
    APP_NAME,
    CACHE_METADATA_NAME,
    CACHE_SCHEMA_VERSION,
    WORKFLOW_ARTIFACT_SCHEMA_VERSION,
)
from cascade_research_tool.exceptions import DataValidationError
from cascade_research_tool.models import InitializationResult
from cascade_research_tool.utilities.identifiers import normalize_identifier
from cascade_research_tool.utilities.security import (
    atomic_write_bytes,
    ensure_pickle_cache_is_private,
)


def get_application_cache_directory() -> Path:
    """
    Return the private per-user application cache directory.

    The location follows common operating-system conventions:

    - Windows: LOCALAPPDATA/cascade_research_tool
    - POSIX: XDG_CACHE_HOME/cascade_research_tool, or ~/.cache as fallback

    On POSIX platforms, the directory is restricted to the current user.
    Windows access is ordinarily governed by the user-profile ACL.
    """

    if os.name == "nt":
        base_directory = Path(
            os.environ.get(
                "LOCALAPPDATA",
                Path.home() / "AppData" / "Local",
            )
        )
    else:
        base_directory = Path(
            os.environ.get(
                "XDG_CACHE_HOME",
                Path.home() / ".cache",
            )
        )

    cache_directory = base_directory / "cascade_research_tool"

    cache_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    # POSIX mode 0700 restricts access to the current user. chmod does not
    # provide equivalent Windows ACL management, so do not rely on it there.
    if os.name != "nt":
        os.chmod(cache_directory, 0o700)

    return cache_directory


def write_cache_metadata(cache_directory: Path) -> None:
    """
    Record the current cache schema version and update timestamp.

    The metadata is informational and contains no researcher data, source
    data, credentials, or other sensitive content.
    """

    metadata = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "application": APP_NAME,
        "created_or_updated_unix_time": time.time(),
    }

    atomic_write_bytes(
        cache_directory / CACHE_METADATA_NAME,
        json.dumps(
            metadata,
            indent=2,
        ).encode("utf-8"),
    )


def save_application_artifact(
    artifact_file: Path,
    artifact_type: str,
    payload: dict[str, Any],
) -> None:
    """
    Persist an application-managed artifact using an atomic pickle write.

    Pickle is used because workflow artifacts can contain pandas DataFrames
    and NetworkX graphs. It must never be used for arbitrary user-supplied
    files.

    Security boundary:
    - Call this only with paths inside the application's private cache area.
    - Application code must never offer a file-picker workflow for loading
      pickle artifacts.
    - Atomic writes prevent incomplete artifacts from becoming valid caches.
    """

    artifact = {
        "schema_version": WORKFLOW_ARTIFACT_SCHEMA_VERSION,
        "artifact_type": artifact_type,
        "created_unix_time": time.time(),
        "payload": payload,
    }

    serialized_artifact = pickle.dumps(
        artifact,
        protocol=pickle.HIGHEST_PROTOCOL,
    )

    atomic_write_bytes(
        artifact_file,
        serialized_artifact,
    )


def load_application_artifact(
    artifact_file: Path,
    expected_artifact_type: str,
) -> dict[str, Any]:
    """
    Load and validate one application-managed pickle artifact.

    The caller is responsible for supplying only an application-controlled
    path beneath the private cache directory. This function checks that the
    artifact exists, applies the POSIX private-permissions safeguard, and
    validates the outer artifact schema/type before returning its payload.
    """

    if not artifact_file.exists():
        raise FileNotFoundError(
            f"Application artifact does not exist: {artifact_file}"
        )

    ensure_pickle_cache_is_private(artifact_file)

    with open(artifact_file, "rb") as input_file:
        artifact = pickle.load(input_file)

    if not isinstance(artifact, dict):
        raise DataValidationError(
            f"Invalid artifact structure in {artifact_file.name}."
        )

    if artifact.get("schema_version") != WORKFLOW_ARTIFACT_SCHEMA_VERSION:
        raise DataValidationError(
            f"Artifact schema mismatch for {artifact_file.name}. "
            "Rebuild the artifact using the current application version."
        )

    if artifact.get("artifact_type") != expected_artifact_type:
        raise DataValidationError(
            f"Unexpected artifact type in {artifact_file.name}."
        )

    payload = artifact.get("payload")

    if not isinstance(payload, dict):
        raise DataValidationError(
            f"Invalid payload structure in {artifact_file.name}."
        )

    return payload


def compute_initialization_fingerprint(
    initialization_result: InitializationResult,
) -> str:
    """
    Create a deterministic compatibility fingerprint for initialization data.

    The fingerprint is not authentication or authorization material. It is a
    compatibility indicator used to warn a researcher when restoring a case
    study against a potentially different NHD/NID matching context.
    """

    matched_dams = (
        initialization_result.dam_inventory_matched[
            ["NID ID", "node_id"]
        ]
        .copy()
        .drop_duplicates(
            subset=["NID ID"],
            keep="first",
        )
    )

    matched_dams["NID ID"] = matched_dams["NID ID"].map(
        normalize_identifier
    )
    matched_dams["node_id"] = matched_dams["node_id"].map(str)

    matched_dams = (
        matched_dams.dropna(
            subset=["NID ID", "node_id"]
        )
        .sort_values(
            ["NID ID", "node_id"],
            kind="stable",
        )
    )

    fingerprint_source = {
        "graph_nodes": initialization_result.graph.number_of_nodes(),
        "graph_edges": initialization_result.graph.number_of_edges(),
        "matched_dams": matched_dams.to_dict(
            orient="records"
        ),
    }

    serialized_source = json.dumps(
        fingerprint_source,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(serialized_source).hexdigest()