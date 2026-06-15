"""Database schema, connection helpers, and tag-cache logic."""

import json
import sqlite3
from pathlib import Path

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS emotion (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    desc       TEXT    NOT NULL,
    sha256     TEXT    NOT NULL,
    tags       TEXT    NOT NULL DEFAULT '[]',
    format     TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0
)
"""

INDEX_SQLS = [
    "CREATE INDEX IF NOT EXISTS idx_emotion_deleted ON emotion(is_deleted)",
    "CREATE INDEX IF NOT EXISTS idx_emotion_updated ON emotion(updated_at)",
]


def get_db(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and row factory."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: Path) -> None:
    """Create the database file and schema if they donʼt exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(CREATE_TABLE_SQL)
        for sql in INDEX_SQLS:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def build_tag_cache(db_path: Path) -> tuple[list[dict], int]:
    """Return (tag_list, untagged_count) from the emotion table."""
    db = get_db(db_path)
    try:
        rows = db.execute(
            "SELECT tags FROM emotion WHERE is_deleted = 0"
        ).fetchall()
        counts: dict[str, int] = {}
        untagged = 0
        for r in rows:
            tag_list = json.loads(r["tags"])
            if tag_list:
                for tag in tag_list:
                    counts[tag] = counts.get(tag, 0) + 1
            else:
                untagged += 1
        tag_list = [{"name": k, "count": v} for k, v in sorted(counts.items())]
        return tag_list, untagged
    finally:
        db.close()
