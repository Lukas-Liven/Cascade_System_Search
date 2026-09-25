"""
Application service layer.

Service modules implement data acquisition, caching, persistence, validation,
and analysis behavior independently of Tkinter widgets. UI code may call
services, but services must not import UI modules.
"""

from __future__ import annotations