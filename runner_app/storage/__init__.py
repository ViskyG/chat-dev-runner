"""Private local storage and versioned SQLite foundations."""

from .database import SCHEMA_VERSION, Database, MigrationError, UnsupportedSchemaVersion
from .paths import Paths, UnsafePathError, ensure_private_directory, open_private_file

__all__ = [
    "SCHEMA_VERSION",
    "Database",
    "MigrationError",
    "Paths",
    "UnsafePathError",
    "UnsupportedSchemaVersion",
    "ensure_private_directory",
    "open_private_file",
]
