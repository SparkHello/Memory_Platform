"""Shared SQLite helpers."""

from __future__ import annotations

import sqlite3


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit/rollback and then release the OS handle at block exit.

    sqlite3's context manager only commits/rolls back; it does not close.  The
    project accesses the database as "with store._connect() as connection:", so
    this also closes the handle at block exit instead of relying on GC.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()
