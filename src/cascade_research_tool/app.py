"""
Application startup entry point.

This module configures runtime dependencies before importing the main Tkinter
window controller. Keep this import sequence intact: initialization services
eventually import pynhd and pygeohydro, which must load only after the aiohttp
DNS resolver workaround has been applied.
"""

from __future__ import annotations

from cascade_research_tool.runtime import (
    configure_aiohttp_dns_resolver,
)


def main() -> None:
    """
    Configure runtime dependencies and start the main Tkinter application.
    """

    configure_aiohttp_dns_resolver()

    # Delayed import is intentional. main_window imports or invokes services
    # that use pynhd and pygeohydro, so it must occur after DNS configuration.
    from cascade_research_tool.ui.main_window import CascadeResearchApp

    application = CascadeResearchApp()
    application.mainloop()