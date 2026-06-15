import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from src.storages.s3 import CommonS3Client
from src.db import get_db, init_db, build_tag_cache

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)

DATA_PATH = Path(os.getenv("DATA_PATH", "/data"))
DB_PATH = DATA_PATH / "data.db"
TEMP_EMOTIONS = DATA_PATH / "temp" / "emotions"

_DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")


def _is_debug() -> bool:
    return _DEBUG

# ── Temp cleanup timer ──────────────────────────────────────────────────────
_temp_uploaded_at = time.time()
_timer_task: asyncio.Task | None = None

TEMP_TTL = 6 * 3600  # 6 hours


async def _temp_cleanup_loop():
    global _temp_uploaded_at
    while True:
        elapsed = time.time() - _temp_uploaded_at
        if elapsed >= TEMP_TTL and TEMP_EMOTIONS.exists():
            shutil.rmtree(TEMP_EMOTIONS)
            TEMP_EMOTIONS.mkdir(parents=True, exist_ok=True)
        await asyncio.sleep(TEMP_TTL)


def _reset_temp_timer():
    global _temp_uploaded_at
    _temp_uploaded_at = time.time()


# ── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def _lifespan(_: FastAPI):
    global _timer_task
    TEMP_EMOTIONS.mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH)
    _refresh_tag_cache()
    _timer_task = asyncio.create_task(_temp_cleanup_loop())
    yield
    if _timer_task:
        _timer_task.cancel()


app = FastAPI(title="Emotion Site", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Tag cache ────────────────────────────────────────────────────────────────
_tag_list: list[dict] = []
_untagged_count: int = 0


def _refresh_tag_cache() -> None:
    global _tag_list, _untagged_count
    _tag_list, _untagged_count = build_tag_cache(DB_PATH)


# ── S3 client ────────────────────────────────────────────────────────────────
_s3: CommonS3Client | None = None


def _get_s3() -> CommonS3Client:
    """Return a singleton CommonS3Client configured from environment variables."""
    global _s3
    if _s3 is None:
        _s3 = CommonS3Client(
            endpoint_url=os.getenv("S3_ENDPOINT_URL", ""),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", ""),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
            config=BotoConfig(
                max_pool_connections=50,
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    return _s3


# ── Models ───────────────────────────────────────────────────────────────────
class EmotionUpdate(BaseModel):
    desc: str = ""
    tags: list[str] = []


class EmotionCreate(BaseModel):
    desc: str = ""
    tags: list[str] = []
    sha256: str = ""
    format: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _detect_image_format(data: bytes) -> str:
    """Detect image format from magic bytes.

    Returns one of 'png', 'jpg', 'gif', 'bmp', 'webp', or '' if unrecognised.
    """
    if len(data) < 12:
        return ""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"GIF8":
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _validate_sha256(sha256: str) -> None:
    if not all(c in "0123456789abcdef" for c in sha256) or len(sha256) != 64:
        raise HTTPException(status_code=400, detail="Invalid sha256")


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _build_where(tags: list[str] | None, desc: str | None) -> tuple[str, list]:
    clauses: list[str] = ["is_deleted = 0"]
    params: list = []

    if desc:
        clauses.append("desc LIKE ?")
        params.append(f"%{desc}%")

    if tags:
        or_parts: list[str] = []
        for tag in tags:
            if tag == "":
                or_parts.append("tags = '[]'")
            else:
                or_parts.append("tags LIKE ?")
                params.append(f'%"{tag}"%')
        if or_parts:
            clauses.append("(" + " OR ".join(or_parts) + ")")

    return " AND ".join(clauses), params


# ── Routes: tags ─────────────────────────────────────────────────────────────
@app.get("/tags")
def list_tags():
    return {"tags": _tag_list, "untagged": _untagged_count}


# ── Routes: list emotions ────────────────────────────────────────────────────
@app.get("/emotions")
def list_emotions(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    desc: str | None = Query(None),
    tags: str | None = Query(None),
):
    tag_list = [t.strip() for t in tags.split(";")] if tags is not None else None
    where, where_params = _build_where(tag_list, desc)

    db = get_db(DB_PATH)
    try:
        total = db.execute(
            f"SELECT COUNT(*) FROM emotion WHERE {where}", where_params
        ).fetchone()[0]

        offset = (page - 1) * page_size
        rows = db.execute(
            f"SELECT desc, sha256, tags, format FROM emotion WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            where_params + [page_size, offset],
        ).fetchall()
    finally:
        db.close()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {"sha256": r["sha256"], "desc": r["desc"], "tags": json.loads(r["tags"]), "format": r["format"]}
            for r in rows
        ],
    }


# ── Routes: get emotion image ────────────────────────────────────────────────
@app.get("/emotions/{sha256}")
async def get_emotion(sha256: str):
    bucket = os.getenv("S3_BUCKET_NAME")
    if not bucket:
        raise HTTPException(status_code=500, detail="S3_BUCKET_NAME not configured")

    _validate_sha256(sha256)
    s3_key = f"sha256/{sha256[0:2]}/{sha256[2:4]}/{sha256}"

    def _fetch():
        try:
            return _get_s3().get(bucket, s3_key)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "NoSuchKey":
                raise HTTPException(status_code=404, detail="Emotion not found")
            raise HTTPException(status_code=500, detail=f"S3 error: {code}")
        except EndpointConnectionError as exc:
            cause = exc.__cause__
            cause_info = f": {cause.__class__.__name__}: {cause}" if cause else ""
            detail = f"Cannot reach S3 endpoint: {_get_s3().endpoint_url}{cause_info}"
            if _is_debug():
                detail += f"\n{traceback.format_exc()}"
            raise HTTPException(status_code=502, detail=detail)
        except Exception as exc:
            detail = f"S3 fetch failed: {exc.__class__.__name__}: {exc}"
            if _is_debug():
                detail += f"\n{traceback.format_exc()}"
            raise HTTPException(status_code=500, detail=detail)

    try:
        body = await run_in_threadpool(_fetch)
    except HTTPException:
        raise
    except Exception as exc:
        detail = f"Unexpected S3 error: {exc.__class__.__name__}: {exc}"
        if _is_debug():
            detail += f"\n{traceback.format_exc()}"
        logging.error("S3 fetch error", exc_info=True)
        raise HTTPException(status_code=500, detail=detail)

    return Response(
        content=body,
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000"},
    )


# ── Routes: upload temp file ─────────────────────────────────────────────────
@app.post("/emotions/upload")
async def upload_temp_file(file: UploadFile):
    """Accept an image file, save to temp dir, return its sha256 and format."""
    data = await file.read()
    sha256 = _sha256_hex(data)
    fmt = _detect_image_format(data)

    TEMP_EMOTIONS.mkdir(parents=True, exist_ok=True)
    dest = TEMP_EMOTIONS / sha256
    if not dest.exists():
        dest.write_bytes(data)

    _reset_temp_timer()
    return {"sha256": sha256, "format": fmt}


# ── Routes: create emotion ───────────────────────────────────────────────────
@app.post("/emotions")
def create_emotion(body: EmotionCreate):
    _validate_sha256(body.sha256)

    bucket = os.getenv("S3_BUCKET_NAME")
    if not bucket:
        raise HTTPException(status_code=500, detail="S3_BUCKET_NAME not configured")

    s3_key = f"sha256/{body.sha256[0:2]}/{body.sha256[2:4]}/{body.sha256}"
    temp_file = TEMP_EMOTIONS / body.sha256

    if not temp_file.exists():
        raise HTTPException(status_code=400, detail="No uploaded file found — upload first")

    s3 = _get_s3()
    try:
        if not s3.head(bucket, s3_key):
            s3.upload(bucket, s3_key, str(temp_file))
    except EndpointConnectionError as exc:
        cause = exc.__cause__
        cause_info = f": {cause.__class__.__name__}: {cause}" if cause else ""
        detail = f"Cannot reach S3 endpoint: {s3.endpoint_url}{cause_info}"
        if _is_debug():
            detail += f"\n{traceback.format_exc()}"
        raise HTTPException(status_code=502, detail=detail)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        raise HTTPException(status_code=500, detail=f"S3 error: {code}")
    except Exception as exc:
        detail = f"S3 upload failed: {exc.__class__.__name__}: {exc}"
        if _is_debug():
            detail += f"\n{traceback.format_exc()}"
        logging.error("S3 upload error", exc_info=True)
        raise HTTPException(status_code=500, detail=detail)
    finally:
        if temp_file.exists():
            temp_file.unlink()

    now = _now()
    db = get_db(DB_PATH)
    try:
        db.execute(
            "INSERT INTO emotion (desc, sha256, tags, format, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (body.desc, body.sha256, json.dumps(body.tags, ensure_ascii=False), body.format, now, now),
        )
        db.commit()
    finally:
        db.close()

    _refresh_tag_cache()
    return Response(status_code=201)


# ── Routes: update emotion ───────────────────────────────────────────────────
@app.put("/emotions/{sha256}")
def update_emotion(sha256: str, body: EmotionUpdate):
    _validate_sha256(sha256)

    db = get_db(DB_PATH)
    try:
        row = db.execute(
            "SELECT id FROM emotion WHERE sha256 = ? AND is_deleted = 0", (sha256,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Emotion not found")

        db.execute(
            "UPDATE emotion SET desc = ?, tags = ?, updated_at = ? WHERE sha256 = ?",
            (body.desc, json.dumps(body.tags, ensure_ascii=False), _now(), sha256),
        )
        db.commit()
    finally:
        db.close()

    _refresh_tag_cache()
    return Response(status_code=204)


# ── Routes: delete emotion ───────────────────────────────────────────────────
@app.delete("/emotions/{sha256}")
def delete_emotion(sha256: str):
    _validate_sha256(sha256)

    db = get_db(DB_PATH)
    try:
        row = db.execute(
            "SELECT id FROM emotion WHERE sha256 = ? AND is_deleted = 0", (sha256,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Emotion not found")

        db.execute(
            "UPDATE emotion SET is_deleted = 1, updated_at = ? WHERE sha256 = ?",
            (_now(), sha256),
        )
        db.commit()
    finally:
        db.close()

    _refresh_tag_cache()
    return Response(status_code=204)


# ── Root ──────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return FileResponse(static_dir / "index.html")
