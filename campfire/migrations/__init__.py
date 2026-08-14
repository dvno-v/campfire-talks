"""Ordered Campfire database migrations.

Each module owns one immutable schema version. The runner records a version
only after that module commits, so an interrupted upgrade is retried cleanly.
"""

from . import v001_initial, v002_legacy_compatibility, v003_security_hardening
from . import v004_voice_rooms

MIGRATIONS = (v001_initial, v002_legacy_compatibility, v003_security_hardening,
              v004_voice_rooms)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].VERSION
