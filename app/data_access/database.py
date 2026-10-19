"""SQLite database connection and management."""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    """Thread-safe SQLite database manager."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._initialized = False
        self._init_lock = threading.Lock()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a thread-local database connection."""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.connection = conn
        return self._local.connection

    @contextmanager
    def get_connection(self):
        """Context manager for database connection."""
        conn = self._get_connection()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise

    @contextmanager
    def transaction(self, immediate: bool = False):
        """Context manager for database transaction.

        Args:
            immediate: Take the write lock up-front (BEGIN IMMEDIATE) so that
                read-validate-write sequences are atomic across connections.
        """
        conn = self._get_connection()
        if conn.in_transaction:
            conn.commit()
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def execute(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Execute a read query and return results."""
        with self.get_connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall()

    def execute_one(self, query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """Execute a read query and return a single result."""
        with self.get_connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchone()

    def execute_write(self, query: str, params: tuple = ()) -> int:
        """Execute a write query within a transaction and return rows affected."""
        with self.transaction() as conn:
            cursor = conn.execute(query, params)
            return cursor.rowcount

    def initialize_extra_tables(self):
        """Create additional tables needed by the application.

        Does NOT modify existing tables (employees, leave_balances, leave_requests).
        Only adds new tables for holidays and ingestion metadata.
        """
        with self._init_lock:
            if self._initialized:
                return

            with self.transaction() as conn:
                # The holidays table is owned by this application (it is not one of
                # the three provided tables). Rebuild it if an older schema without
                # calendar metadata is present; it is fully re-derived at ingestion.
                cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(holidays)").fetchall()
                }
                if cols and "doc_effective_date" not in cols:
                    logger.info("Upgrading holidays table schema")
                    conn.execute("DROP TABLE holidays")

                # Table for holiday dates extracted from calendar documents
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS holidays (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        region TEXT NOT NULL,
                        holiday_date TEXT NOT NULL,
                        holiday_name TEXT NOT NULL,
                        year INTEGER NOT NULL,
                        source_doc_id TEXT NOT NULL,
                        doc_effective_date TEXT NOT NULL,
                        doc_access_level TEXT NOT NULL,
                        UNIQUE(region, holiday_date, source_doc_id)
                    )
                """)

                # Table for ingestion state tracking
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS ingestion_state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)

                # Index for faster holiday lookups
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_holidays_region_year
                    ON holidays(region, year)
                """)

                # Indexes for frequent leave lookups (additive only)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_leave_requests_employee
                    ON leave_requests(employee_id, status)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_employees_manager
                    ON employees(manager_id)
                """)

            self._initialized = True
            logger.info("Extra database tables initialized")

    def get_state(self, key: str) -> Optional[str]:
        """Read a value from the ingestion_state table."""
        row = self.execute_one("SELECT value FROM ingestion_state WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        """Write a value to the ingestion_state table."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO ingestion_state (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                (key, value),
            )

    def close(self):
        """Close the thread-local connection."""
        if hasattr(self._local, "connection") and self._local.connection:
            self._local.connection.close()
            self._local.connection = None


# Global database instance
_db_instance: Optional[Database] = None


def get_database() -> Database:
    """Get the global database instance."""
    global _db_instance
    if _db_instance is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _db_instance


def init_database(db_path: str) -> Database:
    """Initialize the global database instance."""
    global _db_instance
    _db_instance = Database(db_path)
    _db_instance.initialize_extra_tables()
    logger.info("Database initialized: %s", db_path)
    return _db_instance
