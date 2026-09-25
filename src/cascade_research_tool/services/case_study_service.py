"""
Case-study persistence service.

Case studies are application-managed pickle artifacts stored only beneath the
private case_studies cache directory. This module must never be used to load
arbitrary user-selected pickle files.

The service intentionally separates persistence/security responsibilities from
Tkinter dialogs and widget state. UI code decides whether a user-approved
overwrite should occur; this module safely identifies the target artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cascade_research_tool.constants import (
    CASE_STUDY_ARTIFACT_TYPE,
    CASE_STUDY_FILE_SUFFIX,
    CASE_STUDY_SCHEMA_VERSION,
)
from cascade_research_tool.exceptions import DataValidationError
from cascade_research_tool.services.cache_service import (
    load_application_artifact,
    save_application_artifact,
)
from cascade_research_tool.utilities.identifiers import (
    normalize_study_name,
    study_name_to_filename,
)


def get_case_study_file(
    case_studies_directory: Path,
    study_name: str,
) -> Path:
    """
    Return the safe application-managed artifact path for one study name.

    The filename is derived from the normalized display name. The resolved
    parent-directory check prevents traversal outside the controlled
    case_studies directory, including if a malformed input unexpectedly
    reaches this service.
    """

    validated_name = normalize_study_name(study_name)

    expected_directory = case_studies_directory.resolve()

    candidate_file = (
        expected_directory
        / study_name_to_filename(validated_name)
    ).resolve()

    if candidate_file.parent != expected_directory:
        raise ValueError("Invalid case-study file location.")

    return candidate_file


def find_existing_case_study(
    case_studies_directory: Path,
    study_name: str,
) -> tuple[Path, str] | None:
    """
    Find an existing study using post-normalization filename identity.

    Two distinct display names can normalize to the same safe filename, such
    as ``Cascade: Georgia`` and ``Cascade? Georgia``. This function treats
    such names as the same persistence target and returns the existing
    artifact's path and stored display name.

    An unreadable artifact is not silently overwritten. A DataValidationError
    is raised so the UI can inform the researcher and preserve the file.
    """

    validated_name = normalize_study_name(study_name)
    requested_filename = study_name_to_filename(
        validated_name
    ).casefold()

    expected_directory = case_studies_directory.resolve()

    for study_file in expected_directory.glob(
        f"*{CASE_STUDY_FILE_SUFFIX}"
    ):
        # Windows filesystems are normally case-insensitive. casefold() gives
        # consistent collision detection across supported platforms.
        if study_file.name.casefold() != requested_filename:
            continue

        try:
            payload = load_case_study(study_file)
            stored_study_name = validate_case_study_payload(
                payload
            )
        except Exception as error:
            raise DataValidationError(
                "A case-study artifact already exists for the normalized "
                f"name '{validated_name}', but it could not be read safely: "
                f"{study_file.name}. Details: {error}"
            ) from error

        return study_file.resolve(), stored_study_name

    return None


def list_case_studies(
    case_studies_directory: Path,
) -> tuple[list[str], list[str]]:
    """
    Return readable study names and non-sensitive warnings for invalid files.

    The first tuple item is a sorted unique list of stored display names. The
    second item contains diagnostic messages for artifacts that were skipped,
    allowing the UI to log them without preventing valid studies from being
    displayed.
    """

    expected_directory = case_studies_directory.resolve()
    study_names: list[str] = []
    warnings: list[str] = []

    for study_file in sorted(
        expected_directory.glob(
            f"*{CASE_STUDY_FILE_SUFFIX}"
        )
    ):
        try:
            payload = load_case_study(study_file)
            study_name = validate_case_study_payload(payload)
            study_names.append(study_name)

        except Exception as error:
            warnings.append(
                f"Skipped unreadable case-study artifact "
                f"'{study_file.name}': {error}"
            )

    return (
        sorted(
            set(study_names),
            key=str.casefold,
        ),
        warnings,
    )


def save_case_study(
    study_file: Path,
    payload: dict[str, Any],
) -> None:
    """
    Validate and save a case-study payload as a trusted private artifact.

    The caller must obtain ``study_file`` through get_case_study_file() or
    find_existing_case_study(); this function does not accept a user-selected
    arbitrary pickle path.
    """

    validate_case_study_payload(payload)

    save_application_artifact(
        artifact_file=study_file,
        artifact_type=CASE_STUDY_ARTIFACT_TYPE,
        payload=payload,
    )


def load_case_study(
    study_file: Path,
) -> dict[str, Any]:
    """
    Load and validate an application-managed case-study artifact.

    The generic artifact schema and type validation occurs in
    load_application_artifact(). This function additionally validates the
    case-study payload schema and required study-name field.
    """

    payload = load_application_artifact(
        artifact_file=study_file,
        expected_artifact_type=CASE_STUDY_ARTIFACT_TYPE,
    )

    validate_case_study_payload(payload)

    return payload


def validate_case_study_payload(
    payload: dict[str, Any],
) -> str:
    """
    Validate a case-study payload's basic required structure.

    Returns:
        The normalized stored case-study display name.

    Deeper validation of optional Part 3–6 DataFrames and NetworkX graphs
    remains in the UI restore method for this extraction stage, because that
    method directly controls current application state.
    """

    if not isinstance(payload, dict):
        raise DataValidationError(
            "The case study does not contain a valid payload."
        )

    if payload.get("study_schema_version") != CASE_STUDY_SCHEMA_VERSION:
        raise DataValidationError(
            "This case study uses an unsupported schema version."
        )

    study_name = payload.get("study_name")

    if not isinstance(study_name, str):
        raise DataValidationError(
            "The case study does not contain a valid study name."
        )

    return normalize_study_name(study_name)