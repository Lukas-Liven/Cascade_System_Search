"""
Runtime environment configuration.

The HyRiver dependency chain can import aiohttp components that select the
aiodns/pycares resolver. In the target Windows environment, pycares DNS
lookups can fail even when normal Windows DNS and HTTPS connectivity work.

This module must be configured before importing modules that import pynhd or
pygeohydro or else you may encounter DNS resolution errors when the application attempts to download NHDPlusV2 data from USGS servers.
"""

from __future__ import annotations

import aiohttp

def configure_aiohttp_dns_resolver() -> None:
    """
    Configure aiohttp to use Python's threaded DNS resolver.

    ThreadedResolver uses Python socket/getaddrinfo resolution instead of the
    optional aiodns/pycares resolver. This function is safe to call more than
    once and must run before importing pynhd or pygeohydro.
    """

    threaded_resolver = aiohttp.ThreadedResolver

    # aiohttp selects DefaultResolver at import time. When aiodns is installed,
    # it can point to AsyncResolver, which uses pycares. Override each public
    # alias used by aiohttp's connector/client setup.
    aiohttp.resolver.DefaultResolver = threaded_resolver
    aiohttp.connector.DefaultResolver = threaded_resolver
    aiohttp.DefaultResolver = threaded_resolver