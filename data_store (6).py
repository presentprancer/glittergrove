"""Legacy data_store entrypoint.

This thin shim re-exports the canonical helpers from ``cogs.utils.data_store``
so any code (or pending merges) that still reference the historical
``data_store (6).py`` path keep working without conflicts.
"""

from __future__ import annotations

from cogs.utils.data_store import *  # type: ignore  # noqa: F401,F403

# Re-export the public API for explicit star import users
from cogs.utils.data_store import __all__  # noqa: F401
