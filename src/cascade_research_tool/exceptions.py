"""
Application-specific exception types.

Services and utilities raise these exceptions to make expected validation and
cache-security failures distinguishable from unexpected programming errors.
UI code can catch them and provide clear, non-technical user messages.
"""

from __future__ import annotations


class CacheSecurityError(RuntimeError):
    """
    Raised when a local cache artifact does not meet security requirements.

    This is primarily used to reject pickle cache files that are writable by
    other users on POSIX systems. Pickle files must never be loaded from
    untrusted locations because malicious pickle content can execute code.
    """


class DataValidationError(RuntimeError):
    """
    Raised when external or cached data do not meet the application's schema,
    integrity, or compatibility requirements.

    Examples include missing NID columns, malformed Zenodo metadata, checksum
    mismatches, incompatible workflow-artifact schemas, and invalid mappings.
    """