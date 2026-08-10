"""Ordered Campfire database migrations.

Each module owns one immutable schema version. The runner records a version
only after that module commits, so an interrupted upgrade is retried cleanly.
"""

from . import v001_initial, v002_legacy_compatibility

MIGRATIONS = (v001_initial, v002_legacy_compatibility)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].VERSION

