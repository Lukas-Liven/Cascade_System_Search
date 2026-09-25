"""
ResNet crosswalk metadata, download, and checksum-validation service.

This module retrieves the configured ResNet file from Zenodo over HTTPS,
enforces download size limits, validates the locally downloaded or cached
content against Zenodo's published MD5 checksum, and atomically replaces
invalid or incomplete local files.

MD5 is used only because Zenodo publishes the ResNet artifact checksum in
that format. It is not used for passwords, digital signatures, or any
authentication decision.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Callable

import requests

from cascade_research_tool.constants import (
    MAX_RESNET_DOWNLOAD_BYTES,
    RESNET_FILENAME,
    RESNET_RECORD_ID,
    ZENODO_RECORD_API,
)
from cascade_research_tool.exceptions import DataValidationError


def get_zenodo_file_metadata(
    record: dict[str, Any],
    filename: str,
) -> dict[str, Any]:
    """
    Return the metadata entry for a named file in a Zenodo record response.

    Raises:
        DataValidationError: If the configured Zenodo record does not contain
            the expected ResNet filename.
    """

    for file_info in record.get("files", []):
        if file_info.get("key") == filename:
            return file_info

    raise DataValidationError(
        f"Zenodo record {RESNET_RECORD_ID} does not contain expected file "
        f"'{filename}'."
    )


def _get_resnet_file_info(
    log: Callable[[str], None],
) -> dict[str, Any]:
    """
    Retrieve and validate the configured ResNet file metadata from Zenodo.

    This helper is intentionally internal. Both cache verification and fresh
    downloads use the same Zenodo record-validation process.
    """

    log("Retrieving ResNet file metadata from Zenodo...")

    response = requests.get(
        ZENODO_RECORD_API,
        timeout=(10, 60),
    )
    response.raise_for_status()

    record = response.json()

    if not isinstance(record, dict):
        raise DataValidationError(
            "Zenodo returned invalid ResNet record metadata."
        )

    file_info = get_zenodo_file_metadata(
        record,
        RESNET_FILENAME,
    )

    if not isinstance(file_info, dict):
        raise DataValidationError(
            "Zenodo returned invalid ResNet file metadata."
        )

    return file_info


def _extract_expected_md5(
    file_info: dict[str, Any],
) -> str:
    """
    Extract and validate a Zenodo-published MD5 checksum from file metadata.
    """

    expected_checksum = str(
        file_info.get("checksum", "")
    ).strip().lower()

    if not expected_checksum.startswith("md5:"):
        raise DataValidationError(
            "Zenodo did not provide the required MD5 checksum for ResNet."
        )

    expected_md5 = expected_checksum.split(":", 1)[1].strip()

    if not re.fullmatch(r"[0-9a-f]{32}", expected_md5):
        raise DataValidationError(
            "Zenodo provided an invalid MD5 checksum for ResNet."
        )

    return expected_md5


def get_resnet_expected_md5(
    log: Callable[[str], None],
) -> str:
    """
    Retrieve the currently published ResNet MD5 checksum from Zenodo.

    This function deliberately queries current Zenodo metadata before a cache
    is accepted. A cached file is valid only when it matches the checksum for
    the configured record and filename.
    """

    file_info = _get_resnet_file_info(log)

    return _extract_expected_md5(file_info)


def calculate_file_md5(
    source_file: Path,
    maximum_bytes: int = MAX_RESNET_DOWNLOAD_BYTES,
) -> str:
    """
    Calculate the MD5 digest of a bounded-size local file.

    Raises:
        DataValidationError: If the local file exceeds the configured size
            ceiling while it is being read.
    """

    digest = hashlib.md5()
    total_bytes = 0

    with open(source_file, "rb") as input_file:
        while True:
            chunk = input_file.read(1024 * 1024)

            if not chunk:
                break

            total_bytes += len(chunk)

            if total_bytes > maximum_bytes:
                raise DataValidationError(
                    "Cached ResNet file exceeded the configured safety limit."
                )

            digest.update(chunk)

    return digest.hexdigest().lower()


def download_resnet_with_validation(
    destination: Path,
    log: Callable[[str], None],
) -> None:
    """
    Download ResNet from Zenodo and atomically save a checksum-verified copy.

    The destination is replaced only after:
    1. Zenodo metadata has been validated;
    2. the server provides an HTTPS download URL;
    3. the download remains inside the configured maximum size;
    4. the actual MD5 equals the Zenodo-published MD5 checksum.

    Any incomplete or checksum-mismatched temporary download is removed.
    """

    file_info = _get_resnet_file_info(log)
    expected_md5 = _extract_expected_md5(file_info)

    download_url = file_info.get("links", {}).get("self")

    if (
        not isinstance(download_url, str)
        or not download_url.lower().startswith("https://")
    ):
        raise DataValidationError(
            "Zenodo did not provide a valid HTTPS download URL."
        )

    log("Downloading ResNet crosswalk...")

    temporary_file = destination.with_suffix(
        destination.suffix + ".download"
    )
    downloaded_bytes = 0
    digest = hashlib.md5()

    try:
        with requests.get(
            download_url,
            stream=True,
            timeout=(10, 120),
        ) as download_response:
            download_response.raise_for_status()

            with open(temporary_file, "wb") as output_file:
                for chunk in download_response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if not chunk:
                        continue

                    downloaded_bytes += len(chunk)

                    if downloaded_bytes > MAX_RESNET_DOWNLOAD_BYTES:
                        raise DataValidationError(
                            "ResNet download exceeded the configured "
                            "safety limit."
                        )

                    digest.update(chunk)
                    output_file.write(chunk)

                output_file.flush()
                os.fsync(output_file.fileno())

        actual_md5 = digest.hexdigest().lower()

        if actual_md5 != expected_md5:
            raise DataValidationError(
                "ResNet checksum verification failed. "
                "The downloaded file was discarded."
            )

        if os.name != "nt":
            os.chmod(temporary_file, 0o600)

        os.replace(
            temporary_file,
            destination,
        )

        log(
            "ResNet downloaded and validated "
            f"({downloaded_bytes:,} bytes)."
        )

    finally:
        temporary_file.unlink(missing_ok=True)