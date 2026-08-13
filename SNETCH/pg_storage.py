"""
pg_storage.py — Central PostgreSQL file/blob storage for S.N.E.T.C.H
=======================================================================
Every feature that used to save the user's own data (uploaded documents,
downloaded tones, generated images, vault documents, WhatsApp uploads,
SnapLock reference photos, etc.) to a folder ON THIS MACHINE now saves it
here instead — inside PostgreSQL, tied to the user's EMAIL.

Why this matters:
  - Old behaviour: files sat in reminder_tone/, alarm_tone/, task_tone/,
    generated_images/, vault_storage/, snaplock_storage/, whatsapp_uploads/
    on whichever laptop the Flask server happened to run on. Log in with
    a different account (or a fresh machine) and none of that data
    followed you, because it was never linked to the account at all.
  - New behaviour: every file is written to Postgres as a row keyed by
    (user_email, feature, key). Whoever is logged in only ever sees their
    own rows. Sign in with a brand-new email -> nothing to show (fresh
    start). Sign back in with an existing email -> everything they saved
    is right there, no matter which machine/server it's served from.

Connection:
    Reads DATABASE_URL from the environment (.env). Example:
        DATABASE_URL=postgresql://snetch_user:password@localhost:5432/snetch

Usage (from any feature module):
    import pg_storage as pg

    pg.save_file(user_email, "reminder_tone", key="rain.mp3",
                  filename="rain.mp3", data=raw_bytes,
                  content_type="audio/mpeg")

    row = pg.get_file(user_email, "reminder_tone", key="rain.mp3")
    if row:
        row["data"]          # bytes
        row["filename"]      # original filename
        row["content_type"]  # mime type

    pg.list_files(user_email, "reminder_tone")   # -> [{"key", "filename", ...}, ...]
    pg.delete_file(user_email, "reminder_tone", key="rain.mp3")
    pg.delete_all(user_email, "reminder_tone")   # wipe a whole feature bucket
"""

import os
import json
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if not DATABASE_URL:
                    raise RuntimeError(
                        "DATABASE_URL is not set. Add it to your .env file, e.g.\n"
                        "DATABASE_URL=postgresql://snetch_user:password@localhost:5432/snetch"
                    )
                _pool = ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)
    return _pool


@contextmanager
def _conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# Public alias — other modules that need plain relational tables in the
# same Postgres database (e.g. premium.py's subscription rows) reuse this
# same connection pool instead of opening a second one.
db_conn = _conn


def init_db():
    """Create the shared user_files table (idempotent). Call once at app startup."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_files (
                id           BIGSERIAL PRIMARY KEY,
                user_email   TEXT NOT NULL,
                feature      TEXT NOT NULL,
                key          TEXT NOT NULL,
                filename     TEXT NOT NULL,
                content_type TEXT,
                data         BYTEA NOT NULL,
                metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (user_email, feature, key)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_files_lookup
            ON user_files (user_email, feature);
        """)
        conn.commit()


def _norm_email(user_email):
    if not user_email:
        raise ValueError("pg_storage: user_email is required (no anonymous/local storage anymore)")
    return user_email.strip().lower()


# ───────────────────────── CORE API ─────────────────────────

def save_file(user_email, feature, key, filename, data, content_type=None, metadata=None):
    """Insert or overwrite one file for this user+feature+key. `data` must be bytes."""
    user_email = _norm_email(user_email)
    if isinstance(data, str):
        data = data.encode("utf-8")
    meta_json = json.dumps(metadata or {})
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_files (user_email, feature, key, filename, content_type, data, metadata, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, now())
            ON CONFLICT (user_email, feature, key)
            DO UPDATE SET filename = EXCLUDED.filename,
                          content_type = EXCLUDED.content_type,
                          data = EXCLUDED.data,
                          metadata = EXCLUDED.metadata,
                          updated_at = now()
            RETURNING id;
        """, (user_email, feature, key, filename, content_type, psycopg2.Binary(data), meta_json))
        return cur.fetchone()[0]


def get_file(user_email, feature, key):
    """Return {'key','filename','content_type','data','metadata','created_at'} or None."""
    user_email = _norm_email(user_email)
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT key, filename, content_type, data, metadata, created_at, updated_at
            FROM user_files WHERE user_email=%s AND feature=%s AND key=%s
        """, (user_email, feature, key))
        row = cur.fetchone()
        if row:
            row["data"] = bytes(row["data"])
        return dict(row) if row else None


def list_files(user_email, feature):
    """Return metadata (NOT the raw bytes) for every file under this feature, newest first."""
    user_email = _norm_email(user_email)
    with _conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT key, filename, content_type, metadata, created_at, updated_at
            FROM user_files WHERE user_email=%s AND feature=%s
            ORDER BY created_at DESC
        """, (user_email, feature))
        return [dict(r) for r in cur.fetchall()]


def file_exists(user_email, feature, key):
    user_email = _norm_email(user_email)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM user_files WHERE user_email=%s AND feature=%s AND key=%s",
            (user_email, feature, key),
        )
        return cur.fetchone() is not None


def delete_file(user_email, feature, key):
    user_email = _norm_email(user_email)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_files WHERE user_email=%s AND feature=%s AND key=%s",
            (user_email, feature, key),
        )
        return cur.rowcount > 0


def delete_all(user_email, feature):
    """Wipe every file this user has under one feature (e.g. clear a whole vault)."""
    user_email = _norm_email(user_email)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_files WHERE user_email=%s AND feature=%s",
            (user_email, feature),
        )
        return cur.rowcount


def rename_key(user_email, feature, old_key, new_key):
    user_email = _norm_email(user_email)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_files SET key=%s, updated_at=now() WHERE user_email=%s AND feature=%s AND key=%s",
            (new_key, user_email, feature, old_key),
        )
        return cur.rowcount > 0


# ─────────────────── SAFE VARIANTS (never raise) ───────────────────
# For lookups that must gracefully degrade — e.g. checking whether the
# shared default/system tone exists in Postgres yet — instead of taking
# the whole feature down if DATABASE_URL isn't configured/reachable.

def safe_get_file(user_email, feature, key):
    try:
        return get_file(user_email, feature, key)
    except Exception:
        return None


def safe_file_exists(user_email, feature, key):
    try:
        return file_exists(user_email, feature, key)
    except Exception:
        return False


def safe_list_files(user_email, feature):
    try:
        return list_files(user_email, feature)
    except Exception:
        return []
