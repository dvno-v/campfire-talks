"""Ordered Campfire database migrations.

Each module owns one immutable schema version. The runner records a version
only after that module commits, so an interrupted upgrade is retried cleanly.
"""

from . import v001_initial, v002_legacy_compatibility, v003_security_hardening

MIGRATIONS = (v001_initial, v002_legacy_compatibility, v003_security_hardening)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].VERSION
