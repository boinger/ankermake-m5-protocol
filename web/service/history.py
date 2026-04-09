"""Print history service backed by SQLite."""

import os
import sqlite3
import threading
import logging
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone

import cli.util

log = logging.getLogger("history")



_DEFAULT_RETENTION_DAYS = 90
_DEFAULT_MAX_ENTRIES = 500

_PLACEHOLDER_NAMES = frozenset({"unknown", "unknown.gcode", ""})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS print_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'started',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    duration_sec INTEGER,
    progress    INTEGER DEFAULT 0,
    failure_reason TEXT,
    task_id     TEXT,
    archive_relpath TEXT,
    archive_size INTEGER,
    preview_url TEXT
);
"""


class PrintHistory:
    """Thread-safe SQLite print history store."""

    def __init__(self, db_path=None, retention_days=None, max_entries=None):
        self._db_path = db_path or os.path.expanduser("~/.config/ankerctl/history.db")
        self._archive_dir = self._default_archive_dir()
        self._retention_days = int(os.getenv("PRINT_HISTORY_RETENTION_DAYS", retention_days or _DEFAULT_RETENTION_DAYS))
        self._max_entries = int(os.getenv("PRINT_HISTORY_MAX_ENTRIES", max_entries or _DEFAULT_MAX_ENTRIES))
        self._lock = threading.Lock()
        self._memory_conn = None
        self._init_db()

    def _default_archive_dir(self):
        db_path = os.fspath(self._db_path)
        if db_path == ":memory:":
            return None
        return os.path.join(os.path.dirname(os.path.abspath(db_path)), "gcode_archive")

    def _recreate_db_after_corruption(self, exc):
        db_path = os.fspath(self._db_path)
        if db_path == ":memory:":
            raise exc
        log.warning(f"History: database corruption detected at {db_path}: {exc}. Recreating database.")
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass

    def _init_db(self):
        try:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                self._migrate_schema(conn)
                conn.commit()
            finally:
                if os.fspath(self._db_path) != ":memory:":
                    conn.close()
        except sqlite3.DatabaseError as exc:
            self._recreate_db_after_corruption(exc)
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                self._migrate_schema(conn)
                conn.commit()
            finally:
                if os.fspath(self._db_path) != ":memory:":
                    conn.close()

    def _migrate_schema(self, conn):
        """Ensure schema is up to date."""
        try:
            cursor = conn.execute("PRAGMA table_info(print_history)")
            columns = [row[1] for row in cursor.fetchall()]
            if "task_id" not in columns:
                log.info("History: migrating schema, adding task_id column")
                conn.execute("ALTER TABLE print_history ADD COLUMN task_id TEXT")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_task_id ON print_history(task_id)")
            if "archive_relpath" not in columns:
                log.info("History: migrating schema, adding archive_relpath column")
                conn.execute("ALTER TABLE print_history ADD COLUMN archive_relpath TEXT")
            if "archive_size" not in columns:
                log.info("History: migrating schema, adding archive_size column")
                conn.execute("ALTER TABLE print_history ADD COLUMN archive_size INTEGER")
            if "preview_url" not in columns:
                log.info("History: migrating schema, adding preview_url column")
                conn.execute("ALTER TABLE print_history ADD COLUMN preview_url TEXT")
        except Exception as e:
            log.warning(f"History: schema migration failed: {e}")


    def _connect(self):
        if os.fspath(self._db_path) == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._memory_conn.row_factory = sqlite3.Row
            return self._memory_conn
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _prune(self, conn):
        """Remove old entries beyond retention and max count."""
        if self._retention_days > 0:
            cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=self._retention_days)).isoformat()
            conn.execute("DELETE FROM print_history WHERE started_at < ?", (cutoff,))

        if self._max_entries > 0:
            conn.execute("""
                DELETE FROM print_history WHERE id NOT IN (
                    SELECT id FROM print_history ORDER BY id DESC LIMIT ?
                )
            """, (self._max_entries,))
        self._delete_unreferenced_archives(conn)

    def _ensure_archive_dir(self):
        if not self._archive_dir:
            return None
        os.makedirs(self._archive_dir, exist_ok=True)
        return self._archive_dir

    @staticmethod
    def _sanitize_archive_filename(filename):
        base = os.path.basename(str(filename or "").strip()) or "upload.gcode"
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip(" .")
        return safe or "upload.gcode"

    def archive_upload(self, filename, data):
        archive_dir = self._ensure_archive_dir()
        if not archive_dir:
            return None
        payload = bytes(data or b"")
        if not payload:
            raise ValueError("Cannot archive an empty GCode upload")

        safe_name = self._sanitize_archive_filename(filename)
        stored_name = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_name}"
        archive_path = os.path.join(archive_dir, stored_name)
        with open(archive_path, "wb") as fh:
            fh.write(payload)
        thumbnail_bytes = cli.util.extract_gcode_thumbnail(payload)
        if thumbnail_bytes:
            thumbnail_path = self._thumbnail_abspath(self._thumbnail_relpath(stored_name))
            if thumbnail_path:
                with open(thumbnail_path, "wb") as fh:
                    fh.write(thumbnail_bytes)
        return {
            "archive_relpath": stored_name,
            "archive_size": len(payload),
        }

    def _archive_abspath(self, archive_relpath):
        archive_dir = self._archive_dir
        if not archive_dir or not archive_relpath:
            return None
        archive_dir = os.path.realpath(archive_dir)
        archive_path = os.path.realpath(os.path.join(archive_dir, str(archive_relpath)))
        if not archive_path.startswith(archive_dir + os.sep):
            return None
        return archive_path

    @staticmethod
    def _thumbnail_relpath(archive_relpath):
        if not archive_relpath:
            return None
        return f"{archive_relpath}.thumbnail.png"

    def _thumbnail_abspath(self, thumbnail_relpath):
        return self._archive_abspath(thumbnail_relpath)

    def _decorate_entry(self, row):
        entry = dict(row)
        archive_path = self._archive_abspath(entry.get("archive_relpath"))
        thumbnail_path = self._thumbnail_abspath(self._thumbnail_relpath(entry.get("archive_relpath")))
        entry["archive_available"] = bool(archive_path and os.path.exists(archive_path))
        entry["can_reprint"] = entry["archive_available"]
        entry["thumbnail_available"] = bool(
            (thumbnail_path and os.path.exists(thumbnail_path))
            or entry.get("preview_url")
        )
        return entry

    def _delete_unreferenced_archives(self, conn):
        archive_dir = self._archive_dir
        if not archive_dir or not os.path.isdir(archive_dir):
            return
        keep = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT archive_relpath FROM print_history WHERE archive_relpath IS NOT NULL AND archive_relpath != ''"
            ).fetchall()
            if row[0]
        }
        keep |= {
            self._thumbnail_relpath(name)
            for name in keep
            if self._thumbnail_relpath(name)
        }
        for name in os.listdir(archive_dir):
            if name in keep:
                continue
            path = os.path.join(archive_dir, name)
            try:
                if os.path.isfile(path):
                    os.unlink(path)
            except FileNotFoundError:
                continue
            except Exception as exc:
                log.warning(f"History: could not delete unreferenced archive {path}: {exc}")

    def record_start(self, filename, task_id=None, archive_relpath=None, archive_size=None, preview_url=None):
        """Record a print start. Returns the row id.

        If an open 'started' entry exists with the same task_id, it is resumed.
        Otherwise, any existing open entries are closed (orphaned) and a new one is created.

        Returns None immediately if *filename* is a placeholder (empty, 'unknown', etc.)
        to avoid polluting history with uninformative entries.
        """
        if not filename or filename.strip().lower() in _PLACEHOLDER_NAMES:
            log.debug(f"History: skipping placeholder filename {filename!r}")
            return None

        with self._lock:
            with self._connect() as conn:
                # 1. Resume existing open entry for the same task_id
                if task_id:
                    existing = conn.execute(
                        "SELECT id FROM print_history WHERE status='started' AND task_id=?",
                        (task_id,)
                    ).fetchone()
                    if existing:
                        if preview_url:
                            conn.execute(
                                "UPDATE print_history SET preview_url=COALESCE(?, preview_url) WHERE id=?",
                                (preview_url, existing["id"]),
                            )
                        log.info(f"History: resuming entry id={existing['id']} for task_id={task_id}")
                        conn.commit()
                        return existing["id"]

                # Fallback: Resume via filename if no task_id or legacy (only if same filename)
                # This is risky if job restarts, but we prefer task_id now.
                # If we have task_id, we trust it above filename.
                
                # 2. Close any *other* open entries (orphans)
                # If we reached here, we are starting a NEW print session.
                orp_sql = "SELECT id, started_at FROM print_history WHERE status='started'"
                orphans = conn.execute(orp_sql).fetchall()
                
                if orphans:
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    for row in orphans:
                        # Optional: If we match via filename here? No, let's strictly rely on task_id for resume if possible.
                        # If task_id was NOT provided, maybe we fall back to filename matching?
                        # But caller (mqtt) usually provides task_id now.
                        
                        started = datetime.fromisoformat(row["started_at"])
                        duration = int((now - started).total_seconds())
                        conn.execute(
                            "UPDATE print_history SET status='interrupted', finished_at=?,"
                            " duration_sec=? WHERE id=?",
                            (now.isoformat(), duration, row["id"]),
                        )
                    log.info(f"History: marked {len(orphans)} orphaned entries as 'interrupted' before new print")

                # 3. Create new entry
                cur = conn.execute(
                    "INSERT INTO print_history (filename, status, started_at, task_id, archive_relpath, archive_size, preview_url) "
                    "VALUES (?, 'started', ?, ?, ?, ?, ?)",
                    (
                        filename,
                        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                        task_id,
                        archive_relpath,
                        archive_size,
                        preview_url,
                    )
                )
                row_id = cur.lastrowid
                self._prune(conn)
                conn.commit()
                return row_id


    def record_finish(self, filename=None, progress=100, task_id=None):
        """Mark the most recent matching print as finished."""
        with self._lock:
            with self._connect() as conn:
                row = self._find_active(conn, filename, task_id)
                if not row:
                    log.debug("No active print to finish")
                    return
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                started = datetime.fromisoformat(row["started_at"])
                duration = int((now - started).total_seconds())
                conn.execute(
                    "UPDATE print_history SET status='finished', finished_at=?, duration_sec=?, progress=? WHERE id=?",
                    (now.isoformat(), duration, progress, row["id"])
                )
                conn.commit()

    def record_fail(self, filename=None, reason=None, task_id=None):
        """Mark the most recent matching print as failed."""
        with self._lock:
            with self._connect() as conn:
                row = self._find_active(conn, filename, task_id)
                if not row:
                    log.debug("No active print to fail")
                    return
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                started = datetime.fromisoformat(row["started_at"])
                duration = int((now - started).total_seconds())
                conn.execute(
                    "UPDATE print_history SET status='failed', finished_at=?, duration_sec=?, failure_reason=? WHERE id=?",
                    (now.isoformat(), duration, reason, row["id"])
                )
                conn.commit()

    def _find_active(self, conn, filename=None, task_id=None):
        """Find the most recent active print, optionally matching task_id or filename."""
        # 1. Try task_id match (strongest)
        if task_id:
            row = conn.execute(
                "SELECT * FROM print_history WHERE status='started' AND task_id=?",
                (task_id,)
            ).fetchone()
            if row:
                return row

        # 2. Try filename match (legacy/fallback)
        if filename:
            row = conn.execute(
                "SELECT * FROM print_history WHERE status='started' AND filename=? ORDER BY id DESC LIMIT 1",
                (filename,)
            ).fetchone()
            if row:
                return row

        # 3. Fallback: Any active print (weakest)
        return conn.execute(
            "SELECT * FROM print_history WHERE status='started' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def init_schema(self):
        """Backward-compatible alias used by older tests and callers."""
        self._init_db()

    def get_history(self, limit=50, offset=0):
        """Return recent print history as list of dicts."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM print_history ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset)
                ).fetchall()
                return [self._decorate_entry(r) for r in rows]

    def get_entry(self, entry_id):
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM print_history WHERE id=?",
                    (int(entry_id),),
                ).fetchone()
                if not row:
                    return None
                return self._decorate_entry(row)

    def get_archive_path(self, entry_id):
        entry = self.get_entry(entry_id)
        if not entry:
            return None
        archive_path = self._archive_abspath(entry.get("archive_relpath"))
        if not archive_path or not os.path.exists(archive_path):
            return None
        return archive_path

    def get_thumbnail_path(self, entry_id):
        entry = self.get_entry(entry_id)
        if not entry:
            return None
        thumbnail_path = self._thumbnail_abspath(self._thumbnail_relpath(entry.get("archive_relpath")))
        if not thumbnail_path or not os.path.exists(thumbnail_path):
            return None
        return thumbnail_path

    def update_preview_url(self, preview_url, filename=None, task_id=None):
        if not preview_url:
            return False
        with self._lock:
            with self._connect() as conn:
                row = self._find_active(conn, filename, task_id)
                if not row:
                    return False
                conn.execute(
                    "UPDATE print_history SET preview_url=? WHERE id=?",
                    (preview_url, row["id"]),
                )
                conn.commit()
                return True

    def list_entries(self, limit=50, offset=0):
        """Backward-compatible alias for get_history()."""
        return self.get_history(limit=limit, offset=offset)

    def get_count(self):
        """Return total number of entries."""
        with self._lock:
            with self._connect() as conn:
                return conn.execute("SELECT COUNT(*) FROM print_history").fetchone()[0]

    def clear(self):
        """Delete all history entries."""
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM print_history")
                conn.commit()
                log.info("Print history cleared")
        if self._archive_dir and os.path.isdir(self._archive_dir):
            shutil.rmtree(self._archive_dir, ignore_errors=True)
