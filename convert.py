"""Convert 表情-目录表.txt into an SQLite database."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_PREFIX = "S:\\OneDrive-Now\\OneDrive\\个人文件\\表情\\"
CATALOG_FILE = Path(__file__).parent / "表情-目录表.txt"
DATA_PATH = Path(os.getenv("DATA_PATH", str(Path(__file__).parent.parent / "data")))
DB_FILE = DATA_PATH / "data.db"
SKIP_TAG = "bilibiliEmotions"

_now = int(datetime.now(timezone.utc).timestamp())


def parse_line(line: str) -> tuple[str, str, str] | None:
    """Return (desc, sha256, tags_json) or None."""
    line = line.strip()
    if not line:
        return None

    idx = line.rfind(":")
    if idx == -1:
        return None

    full_path = line[:idx]
    sha256 = line[idx + 1:].strip().lower()

    if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
        return None

    norm = full_path.strip()

    if norm.upper().startswith(BASE_PREFIX.upper()):
        relative = norm[len(BASE_PREFIX):]
    else:
        marker = "OneDrive\\"
        marker_idx = norm.upper().find(marker.upper())
        if marker_idx != -1:
            relative = norm[marker_idx + len(marker):]
        else:
            relative = norm.split("\\")[-1]

    relative = relative.replace("\\", "/").lstrip("/")

    # Strip file extension from desc
    desc = relative.rsplit(".", 1)[0] if "." in relative else relative

    # Extract tags from parent directories, skip excluded tags
    parts = relative.split("/")
    dirs = parts[:-1] if len(parts) > 1 else []
    tags = [d for d in dirs if d != SKIP_TAG]

    return desc, sha256, json.dumps(tags, ensure_ascii=False)


def main() -> None:
    if DB_FILE.exists():
        DB_FILE.unlink()
        print(f"Removed existing {DB_FILE}")

    conn = sqlite3.connect(str(DB_FILE))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.execute(
        """
        CREATE TABLE emotion (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            desc       TEXT    NOT NULL,
            sha256     TEXT    NOT NULL,
            tags       TEXT    NOT NULL DEFAULT '[]',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            is_deleted INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    raw = CATALOG_FILE.read_text(encoding="utf-8")
    lines = raw.splitlines()

    batch: list[tuple[str, str, str, str, str]] = []
    skipped = 0

    for line in lines:
        parsed = parse_line(line)
        if parsed is None:
            skipped += 1
            continue
        desc, sha256, tags_json = parsed
        batch.append((desc, sha256, tags_json, _now, _now))

        if len(batch) >= 1000:
            conn.executemany(
                "INSERT INTO emotion (desc, sha256, tags, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                batch,
            )
            batch.clear()

    if batch:
        conn.executemany(
            "INSERT INTO emotion (desc, sha256, tags, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            batch,
        )

    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM emotion").fetchone()[0]
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emotion_deleted ON emotion(is_deleted)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emotion_updated ON emotion(updated_at)")
    conn.commit()
    conn.close()

    print(f"Imported {count} emotions, skipped {skipped} lines → {DB_FILE}")


if __name__ == "__main__":
    main()
