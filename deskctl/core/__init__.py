"""deskctl core: desktop observe/control primitives.

Importing this package makes the process per-monitor DPI aware (before any
screen capture or UI Automation work). All coordinates are physical virtual-
screen pixels.
"""

from . import dpi as _dpi

_dpi.ensure_dpi_awareness()


class DeskctlError(Exception):
    """User-facing deskctl error (bad target, paused, unavailable feature...)."""
