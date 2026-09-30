"""SQLite connection defaults shared by collector, marts, and dashboard."""
from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 30_000


def connect(path: str | Path, *, foreign_keys: bool = False) -> sqlite3.Connection:
    """Open a connection with an explicit lock wait and consistent FK policy."""
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys=ON")
    return connection


def enable_wal(connection: sqlite3.Connection) -> None:
    """Enable WAL once during database initialization for concurrent readers."""
    connection.execute("PRAGMA journal_mode=WAL")
