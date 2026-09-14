#!/usr/bin/env python3
"""Local web panel for image similarity search."""

from __future__ import annotations

import base64
import csv
import heapq
import hashlib
import html
import io
import json
import os
import shutil
import sqlite3
import threading
import time
import tempfile
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, render_template_string, request, send_file
from werkzeug.utils import secure_filename
import imagehash
from PIL import Image
import requests

from image_matcher import (
    Match,
    SUPPORTED_EXTENSIONS,
    image_phash,
    similarity_from_distance,
    result_folder_names,
    validate_inputs,
    write_csv,
    write_html,
)
from synology_cloud import SynologyClient, SynologyFile, normalize_remote_path
from visual_search import VisualSearchIndex


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "work" / "uploads"
WEB_RESULTS_DIR = BASE_DIR / "results"
DEFAULT_SHARE_FOLDER = Path(os.environ.get("IMAGE_MATCHER_SHARE_FOLDER", "\\\\Oygx2026\\\u6b27\u91ce\u56fe\u5e93\u533a\u57df"))
CLOUD_INDEX_DIR = Path(
    os.environ.get("IMAGE_MATCHER_INDEX_DIR", "\\\\Oygx2026\\\u4e1a\u52a1-\u8fd0\u8425\u5171\u4eab\u6587\u4ef6\\image_matcher_cloud_index")
)
CLOUD_HASH_DB = CLOUD_INDEX_DIR / "hash_cache.sqlite3"
LOCAL_INDEX_SNAPSHOT_DIR = BASE_DIR / "work" / "cloud_index_snapshot"
LOCAL_HASH_DB = LOCAL_INDEX_SNAPSHOT_DIR / "hash_cache.sqlite3"
LOCAL_SNAPSHOT_MAX_AGE_SECONDS = 6 * 60 * 60
CLOUD_WORKERS = 8
LOCAL_SHARE_WORKERS = 4
LOCAL_SCAN_BATCH_SIZE = 500
LOCAL_PROGRESS_INTERVAL_SECONDS = 1.0
LOCAL_SHARE_CACHE_SOURCE = "local-share"
LOCAL_SHARE_CACHE_SOURCE_V2 = "local-share-v2"
LOCAL_SHARE_ID = os.environ.get("IMAGE_MATCHER_SHARE_ID", "oygx2026-gallery")
EXCLUDED_SHARE_FOLDER_NAMES = {".recycle", "#recycle", "@recycle", "$recycle.bin"}
LEGACY_SHARE_PREFIX = os.environ.get(
    "IMAGE_MATCHER_LEGACY_SHARE_PREFIX",
    "\\\\Oygx2026\\\u6b27\u91ce\u56fe\u5e93\u533a\u57df",
)
HOST = os.environ.get("IMAGE_MATCHER_HOST", "127.0.0.1")
PORT = int(os.environ.get("IMAGE_MATCHER_PORT", os.environ.get("PORT", "5000")))
APP_TITLE = os.environ.get("IMAGE_MATCHER_APP_TITLE", "\u4e91\u7aef\u6848\u4f8b\u56fe\u641c\u7d22")
APP_SUBTITLE = os.environ.get(
    "IMAGE_MATCHER_APP_SUBTITLE",
    "\u4e0a\u4f20\u53c2\u8003\u56fe\uff0c\u4ece\u516c\u53f8\u5171\u4eab\u56fe\u5e93\u4e2d\u5feb\u901f\u627e\u5230\u76f8\u4f3c\u6848\u4f8b\u56fe\u3002",
)
APP_AUDIENCE_TEXT = os.environ.get(
    "IMAGE_MATCHER_AUDIENCE_TEXT",
    "\u4e1a\u52a1\u5458\u53ea\u9700\u8981\u4e0a\u4f20\u4e00\u5f20\u53c2\u8003\u56fe\uff0c\u7cfb\u7edf\u4f1a\u81ea\u52a8\u4ece\u5171\u4eab\u56fe\u5e93\u4e2d\u641c\u7d22\u76f8\u4f3c\u6848\u4f8b\u3002",
)
APP_BADGE = os.environ.get("IMAGE_MATCHER_APP_BADGE", "\u5171\u4eab\u56fe\u5e93 / \u6307\u7eb9\u7f13\u5b58")
KIMI_API_KEY = os.environ.get("KIMI_API_KEY", "").strip()
KIMI_API_BASE = os.environ.get("KIMI_API_BASE", "https://api.moonshot.cn/v1").rstrip("/")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "kimi-k2.6")
KIMI_ENABLED = os.environ.get("KIMI_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
KIMI_CANDIDATE_COUNT = max(4, min(20, int(os.environ.get("KIMI_CANDIDATE_COUNT", "20"))))
KIMI_RETURN_COUNT = max(1, min(12, int(os.environ.get("KIMI_RETURN_COUNT", "10"))))
VISUAL_SEARCH_ENABLED = os.environ.get("VISUAL_SEARCH_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
VISUAL_MODEL_ID = os.environ.get("VISUAL_MODEL_ID", "openai/clip-vit-base-patch32")
VISUAL_BATCH_SIZE = max(1, min(32, int(os.environ.get("VISUAL_BATCH_SIZE", "8"))))
VISUAL_TORCH_THREADS = max(1, int(os.environ.get("VISUAL_TORCH_THREADS", "6")))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
visual_index = VisualSearchIndex(
    CLOUD_INDEX_DIR,
    LOCAL_SHARE_ID,
    model_id=VISUAL_MODEL_ID,
    batch_size=VISUAL_BATCH_SIZE,
    torch_threads=VISUAL_TORCH_THREADS,
)

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


def init_cloud_hash_cache() -> None:
    CLOUD_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CLOUD_HASH_DB, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS image_hashes (
                cache_key TEXT PRIMARY KEY,
                server_url TEXT NOT NULL,
                file_id TEXT,
                remote_path TEXT,
                filename TEXT,
                hash_value TEXT NOT NULL,
                local_path TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )


def cloud_cache_key(server_url: str, file) -> str:
    raw = f"{server_url}|{file.file_id or file.path}|{file.name}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def temporary_cloud_image_path(temp_dir: Path, cache_key: str, filename: str) -> Path:
    suffix = Path(filename).suffix.lower() or ".img"
    return temp_dir / f"{cache_key}{suffix}"


def read_cached_hash(cache_key: str) -> imagehash.ImageHash | None:
    hashes = read_cached_hashes([cache_key])
    if cache_key not in hashes:
        return None
    return hashes[cache_key]


def refresh_local_hash_snapshot() -> Path | None:
    LOCAL_INDEX_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if not CLOUD_HASH_DB.exists():
            return LOCAL_HASH_DB if LOCAL_HASH_DB.exists() else None
        source_stat = CLOUD_HASH_DB.stat()
        if LOCAL_HASH_DB.exists():
            local_stat = LOCAL_HASH_DB.stat()
            if local_stat.st_size == source_stat.st_size and local_stat.st_mtime >= source_stat.st_mtime:
                return LOCAL_HASH_DB
        temp_path = LOCAL_HASH_DB.with_suffix(".sqlite3.tmp")
        if temp_path.exists():
            temp_path.unlink()
        shutil.copy2(CLOUD_HASH_DB, temp_path)
        temp_path.replace(LOCAL_HASH_DB)
        return LOCAL_HASH_DB
    except Exception:
        return LOCAL_HASH_DB if LOCAL_HASH_DB.exists() else None


def read_cached_hashes(cache_keys: list[str]) -> dict[str, imagehash.ImageHash]:
    if not cache_keys:
        return {}
    db_path = refresh_local_hash_snapshot()
    if not db_path or not db_path.exists():
        return {}
    unique_keys = list(dict.fromkeys(cache_keys))
    hashes: dict[str, imagehash.ImageHash] = {}
    with sqlite3.connect(db_path, timeout=60) as conn:
        conn.execute("PRAGMA busy_timeout = 60000")
        for index in range(0, len(unique_keys), 500):
            chunk = unique_keys[index : index + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT cache_key, hash_value FROM image_hashes WHERE cache_key IN ({placeholders})",
                chunk,
            ).fetchall()
            for key, hash_value in rows:
                try:
                    hashes[key] = imagehash.hex_to_hash(hash_value)
                except Exception:
                    continue
    return hashes


def cloud_folder_cache_prefixes(remote_folder: str) -> list[str]:
    folder = normalize_remote_path(remote_folder)
    prefixes = [folder.rstrip("/")]
    parts = [part for part in folder.split("/") if part]
    if len(parts) >= 3 and parts[0].lower() == "team-folders":
        prefixes.append("/" + "/".join(parts[2:]))
    if len(parts) >= 2 and parts[0].lower() == "team-folders":
        prefixes.append("/" + "/".join(parts[1:]))
    return [prefix for prefix in dict.fromkeys(prefixes) if prefix and prefix != "/"]


def read_cached_cloud_images_for_folder(remote_folder: str) -> list[CachedCloudImage]:
    db_path = refresh_local_hash_snapshot()
    if not db_path or not db_path.exists():
        return []
    prefixes = cloud_folder_cache_prefixes(remote_folder)
    if not prefixes:
        return []
    where = " OR ".join("remote_path = ? OR remote_path LIKE ?" for _ in prefixes)
    params: list[str] = []
    for prefix in prefixes:
        prefix = prefix.rstrip("/")
        params.extend([prefix, prefix + "/%"])
    records: list[CachedCloudImage] = []
    with sqlite3.connect(db_path, timeout=60) as conn:
        conn.execute("PRAGMA busy_timeout = 60000")
        rows = conn.execute(
            f"""
            SELECT cache_key, server_url, file_id, remote_path, filename, hash_value
            FROM image_hashes
            WHERE {where}
            """,
            params,
        ).fetchall()
    for cache_key, cached_server, file_id, remote_path, filename, hash_value in rows:
        records.append(
            CachedCloudImage(
                cache_key=str(cache_key or ""),
                server_url=str(cached_server or ""),
                file_id=str(file_id or ""),
                remote_path=str(remote_path or ""),
                filename=str(filename or Path(str(remote_path or "")).name),
                hash_value=str(hash_value or ""),
            )
        )
    return records


def normalize_local_cache_path(path: Path) -> str:
    return str(path.resolve()).rstrip("\\/")


def share_relative_path(path: Path, root: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except Exception:
        relative = Path(path.name)
    return relative.as_posix()


def share_record_path(root: Path, remote_path: str) -> Path:
    cleaned = str(remote_path or "").replace("\\", "/").strip("/")
    if not cleaned:
        return root
    return root.joinpath(*[part for part in cleaned.split("/") if part])


def is_excluded_share_path(path: Path | str) -> bool:
    parts = [part.lower() for part in str(path).replace("\\", "/").split("/") if part]
    return any(part in EXCLUDED_SHARE_FOLDER_NAMES for part in parts)


def legacy_share_record_path(root: Path, remote_path: str) -> Path | None:
    path_text = str(remote_path or "").replace("/", "\\").rstrip("\\")
    prefix = LEGACY_SHARE_PREFIX.replace("/", "\\").rstrip("\\")
    path_lower = path_text.lower()
    prefix_lower = prefix.lower()
    if path_lower == prefix_lower:
        return root
    if path_lower.startswith(prefix_lower + "\\"):
        return share_record_path(root, path_text[len(prefix) :])
    return None


def local_image_cache_key(path: Path, root: Path | None = None) -> str:
    stat = path.stat()
    if root is not None:
        relative_path = share_relative_path(path, root)
        raw = f"{LOCAL_SHARE_CACHE_SOURCE_V2}|{LOCAL_SHARE_ID}|{relative_path}|{stat.st_size}|{stat.st_mtime_ns}"
    else:
        raw = f"{LOCAL_SHARE_CACHE_SOURCE}|{normalize_local_cache_path(path)}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def read_cached_local_images_for_folder(folder: Path) -> list[CachedCloudImage]:
    db_path = refresh_local_hash_snapshot()
    if not db_path or not db_path.exists():
        return []
    folder_prefix = normalize_local_cache_path(folder).lower().rstrip("\\/")
    records: list[CachedCloudImage] = []
    with sqlite3.connect(db_path, timeout=60) as conn:
        conn.execute("PRAGMA busy_timeout = 60000")
        rows = conn.execute(
            """
            SELECT cache_key, server_url, file_id, remote_path, filename, hash_value
            FROM image_hashes
            WHERE server_url = ?
            """,
            (LOCAL_SHARE_CACHE_SOURCE,),
        ).fetchall()
    for cache_key, source, file_id, remote_path, filename, hash_value in rows:
        path_text = str(remote_path or "")
        if is_excluded_share_path(path_text):
            continue
        path_lower = path_text.lower().rstrip("\\/")
        if path_lower == folder_prefix or path_lower.startswith(folder_prefix + "\\"):
            image_path = Path(path_text)
        else:
            image_path = legacy_share_record_path(folder, path_text)
        if image_path is not None:
            records.append(
                CachedCloudImage(
                    cache_key=str(cache_key or ""),
                    server_url=str(source or LOCAL_SHARE_CACHE_SOURCE),
                    file_id=str(file_id or ""),
                    remote_path=str(image_path),
                    filename=str(filename or image_path.name),
                    hash_value=str(hash_value or ""),
                )
            )
    with sqlite3.connect(db_path, timeout=60) as conn:
        conn.execute("PRAGMA busy_timeout = 60000")
        rows = conn.execute(
            """
            SELECT cache_key, server_url, file_id, remote_path, filename, hash_value
            FROM image_hashes
            WHERE server_url = ? AND file_id = ?
            """,
            (LOCAL_SHARE_CACHE_SOURCE_V2, LOCAL_SHARE_ID),
        ).fetchall()
    for cache_key, source, file_id, remote_path, filename, hash_value in rows:
        image_path = share_record_path(folder, str(remote_path or ""))
        if is_excluded_share_path(image_path) or not image_path.is_file():
            continue
        records.append(
            CachedCloudImage(
                cache_key=str(cache_key or ""),
                server_url=str(source or LOCAL_SHARE_CACHE_SOURCE_V2),
                file_id=str(file_id or LOCAL_SHARE_ID),
                remote_path=str(image_path),
                filename=str(filename or image_path.name),
                hash_value=str(hash_value or ""),
            )
        )
    return records


def cached_local_hash_record(
    path: Path,
    cache_key: str,
    hash_value: imagehash.ImageHash,
    root: Path | None = None,
) -> tuple[Any, ...]:
    if root is not None:
        return (
            cache_key,
            LOCAL_SHARE_CACHE_SOURCE_V2,
            LOCAL_SHARE_ID,
            share_relative_path(path, root),
            path.name,
            str(hash_value),
            "",
            time.time(),
        )
    return (
        cache_key,
        LOCAL_SHARE_CACHE_SOURCE,
        "",
        normalize_local_cache_path(path),
        path.name,
        str(hash_value),
        "",
        time.time(),
    )


def cached_hash_record(server_url: str, file, cache_key: str, hash_value: imagehash.ImageHash) -> tuple[Any, ...]:
    return (
        cache_key,
        server_url,
        file.file_id,
        file.path,
        file.name,
        str(hash_value),
        "",
        time.time(),
    )


def save_cached_hash_batch(records: list[tuple[Any, ...]]) -> None:
    if not records:
        return
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with sqlite3.connect(CLOUD_HASH_DB, timeout=60) as conn:
                conn.execute("PRAGMA busy_timeout = 60000")
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO image_hashes
                    (cache_key, server_url, file_id, remote_path, filename, hash_value, local_path, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    records,
                )
            try:
                if LOCAL_HASH_DB.exists():
                    LOCAL_HASH_DB.unlink()
            except Exception:
                pass
            return
        except sqlite3.Error as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    if last_error:
        raise last_error


def save_cached_hash(server_url: str, file, cache_key: str, hash_value: imagehash.ImageHash) -> None:
    save_cached_hash_batch([cached_hash_record(server_url, file, cache_key, hash_value)])


@dataclass
class CloudMatch:
    rank: int
    similarity: float
    filename: str
    remote_path: str
    local_path: Path


@dataclass(frozen=True)
class CachedCloudImage:
    cache_key: str
    server_url: str
    file_id: str
    remote_path: str
    filename: str
    hash_value: str


def set_job(job_id: str, **updates: Any) -> None:
    with jobs_lock:
        job = jobs.setdefault(job_id, {})
        job.update(updates)


def get_job(job_id: str) -> dict[str, Any] | None:
    with jobs_lock:
        job = jobs.get(job_id)
        return dict(job) if job else None


def encode_path(path: Path) -> str:
    raw = str(path.resolve()).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_path(token: str) -> Path:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except Exception:
        abort(404)
    return Path(raw)


def clean_folder_input(value: str) -> Path:
    cleaned = value.strip().strip('"').strip("'")
    parsed = urlparse(cleaned)
    if parsed.scheme in {"http", "https"}:
        raise ValueError(
            "You entered a Synology web page URL. Please use the mapped cloud drive path instead, "
            "for example Z:\\Product Folder or Z:\\Yunyinggx\\..."
        )
    return Path(cleaned).expanduser().resolve()


def allowed_upload(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


def iter_share_image_files(
    folder: Path,
    directory_callback: Callable[[Path], None] | None = None,
) -> Iterable[Path]:
    """Walk large shared folders without first building a full file list."""
    stack = [folder]
    while stack:
        current = stack.pop()
        if directory_callback:
            directory_callback(current)
        try:
            with os.scandir(current) as entries:
                child_dirs: list[Path] = []
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child_path = Path(entry.path)
                            if not is_excluded_share_path(child_path):
                                child_dirs.append(child_path)
                        elif entry.is_file(follow_symlinks=False):
                            suffix = Path(entry.name).suffix.lower()
                            if suffix in SUPPORTED_EXTENSIONS:
                                yield Path(entry.path)
                    except OSError:
                        continue
                stack.extend(reversed(child_dirs))
        except OSError:
            continue


def path_exists_label(path_text: str) -> dict[str, Any]:
    try:
        path = clean_folder_input(path_text)
        return {"path": str(path), "exists": path.is_dir()}
    except Exception as exc:
        return {"path": path_text, "exists": False, "error": str(exc)}


def match_to_json(match, reasons: dict[str, str] | None = None) -> dict[str, Any]:
    project_folder, image_folder = result_folder_names(match.path)
    result = {
        "rank": match.rank,
        "similarity": f"{match.similarity:.2f}",
        "filename": match.filename,
        "project_folder": project_folder,
        "image_folder": image_folder,
        "path": str(match.path),
        "image_url": f"/image/{encode_path(match.path)}",
    }
    if reasons:
        result["ai_reason"] = reasons.get(str(match.path), "")
    return result


def remember_ai_candidate(
    heap: list[tuple[float, int, Path]],
    similarity: float,
    path: Path,
    sequence: int,
    limit: int,
) -> None:
    item = (similarity, sequence, path)
    if len(heap) < limit:
        heapq.heappush(heap, item)
    elif similarity > heap[0][0]:
        heapq.heapreplace(heap, item)


def hybrid_candidate_pool(
    phash_candidates: list[tuple[float, Path]],
    visual_candidates: list[tuple[float, Path]],
    limit: int,
) -> list[tuple[float, Path]]:
    """Interleave both retrieval methods so either one can surface the right image."""
    phash_ranked = sorted(phash_candidates, key=lambda item: (-item[0], str(item[1]).lower()))
    visual_ranked = sorted(visual_candidates, key=lambda item: (-item[0], str(item[1]).lower()))
    result: list[tuple[float, Path]] = []
    seen: set[str] = set()

    for rank in range(max(len(phash_ranked), len(visual_ranked))):
        for source in (visual_ranked, phash_ranked):
            if rank >= len(source):
                continue
            score, path = source[rank]
            key = os.path.normcase(os.path.abspath(str(path)))
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            result.append((score, path))
            if len(result) >= limit:
                return result
    return result


def fallback_hybrid_matches(candidates: list[tuple[float, Path]]) -> list[Match]:
    return [
        Match(rank=rank, similarity=round(score, 2), filename=path.name, path=path)
        for rank, (score, path) in enumerate(candidates[:KIMI_RETURN_COUNT], start=1)
    ]


def kimi_image_data_url(path: Path) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((768, 768), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=78, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def parse_kimi_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        cleaned = cleaned.rsplit("```", 1)[0].strip()
    return json.loads(cleaned)


def kimi_rank_candidates(
    query_path: Path,
    candidates: list[tuple[float, Path]],
) -> tuple[list[Match], dict[str, str]]:
    if not KIMI_ENABLED or not KIMI_API_KEY:
        return [], {}

    usable = [(score, path) for score, path in candidates if path.is_file()][:KIMI_CANDIDATE_COUNT]
    if not usable:
        return [], {}

    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "你是公司案例图库的图片检索专家。第一张图片是客户参考图，后面是候选案例图。"
                "请从空间类型、主体产品、布局结构、视角、颜色与整体风格判断视觉相似度。"
                "不要因为文件名相同而加分。返回严格 JSON："
                '{"results":[{"id":1,"score":0到100的整数,"reason":"20字以内中文理由"}]}。'
                f"最多返回 {KIMI_RETURN_COUNT} 张，按 score 从高到低，只保留 score>=35 的候选。"
            ),
        },
        {"type": "image_url", "image_url": {"url": kimi_image_data_url(query_path)}},
        {"type": "text", "text": "以上是客户参考图。下面开始是候选案例图。"},
    ]
    for index, (_, path) in enumerate(usable, start=1):
        content.extend(
            [
                {"type": "text", "text": f"候选图 ID={index}"},
                {"type": "image_url", "image_url": {"url": kimi_image_data_url(path)}},
            ]
        )

    response = requests.post(
        f"{KIMI_API_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {KIMI_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": KIMI_MODEL,
            "messages": [{"role": "user", "content": content}],
            "temperature": 1,
            "response_format": {"type": "json_object"},
        },
        timeout=(20, 180),
    )
    response.raise_for_status()
    payload = parse_kimi_json(response.json()["choices"][0]["message"]["content"])
    ranked: list[tuple[float, Path, str]] = []
    for item in payload.get("results", []):
        try:
            candidate_id = int(item["id"])
            score = max(0.0, min(100.0, float(item["score"])))
            path = usable[candidate_id - 1][1]
            reason = str(item.get("reason", "")).strip()[:80]
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if score >= 35:
            ranked.append((score, path, reason))
    ranked.sort(key=lambda item: (-item[0], str(item[1]).lower()))
    ranked = ranked[:KIMI_RETURN_COUNT]
    reasons = {str(path): reason for _, path, reason in ranked}
    matches = [
        Match(rank=index, similarity=round(score, 2), filename=path.name, path=path)
        for index, (score, path, _) in enumerate(ranked, start=1)
    ]
    return matches, reasons


def cloud_match_to_json(match: CloudMatch) -> dict[str, Any]:
    project_folder, image_folder = result_folder_names(match.remote_path)
    return {
        "rank": match.rank,
        "similarity": f"{match.similarity:.2f}",
        "filename": match.filename,
        "project_folder": project_folder,
        "image_folder": image_folder,
        "path": match.remote_path,
        "image_url": f"/image/{encode_path(match.local_path)}",
    }


def safe_cache_name(index: int, filename: str) -> str:
    safe = secure_filename(filename) or f"image-{index}"
    suffix = Path(filename).suffix.lower()
    if suffix and not safe.lower().endswith(suffix):
        safe += suffix
    return f"{index:06d}-{safe}"


def write_cloud_csv(matches: list[CloudMatch], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            ["rank", "similarity", "project_folder", "image_folder", "filename", "synology_path", "cached_preview_file"]
        )
        for match in matches:
            project_folder, image_folder = result_folder_names(match.remote_path)
            writer.writerow(
                [
                    match.rank,
                    f"{match.similarity:.2f}",
                    project_folder,
                    image_folder,
                    match.filename,
                    match.remote_path,
                    str(match.local_path),
                ]
            )


def write_cloud_html(
    matches: list[CloudMatch],
    query_path: Path,
    output_path: Path,
    threshold: float,
    scanned: int,
    skipped: int,
) -> None:
    rows = []
    for match in matches:
        image_uri = match.local_path.resolve().as_uri()
        project_folder, image_folder = result_folder_names(match.remote_path)
        rows.append(
            f"""
            <article class="card">
              <a href="{html.escape(image_uri, quote=True)}" target="_blank" rel="noreferrer">
                <img src="{html.escape(image_uri, quote=True)}" alt="{html.escape(match.filename, quote=True)}">
              </a>
              <div class="meta">
                <strong>#{match.rank} - {match.similarity:.2f}%</strong>
                <span><b>\u9879\u76ee\u6587\u4ef6\u5939\uff1a</b>{html.escape(project_folder)}</span>
                <span><b>\u56fe\u7247\u6587\u4ef6\u5939\uff1a</b>{html.escape(image_folder)}</span>
                <span>{html.escape(match.filename)}</span>
                <small>{html.escape(match.remote_path)}</small>
              </div>
            </article>
            """
        )
    cards_html = "\n".join(rows) if rows else '<p class="empty">No matching images found.</p>'
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Synology Image Search Results</title>
  <style>
    body {{ margin: 0; padding: 28px; font-family: Arial, sans-serif; color: #1f2933; background: #f6f7f9; }}
    header, main {{ max-width: 1180px; margin: 0 auto; }}
    header {{ margin-bottom: 22px; }}
    h1 {{ margin: 0 0 14px; font-size: 28px; }}
    .summary, .card, .empty {{ background: #fff; border: 1px solid #e1e5ea; border-radius: 8px; }}
    .summary {{ display: grid; grid-template-columns: 220px 1fr; gap: 20px; padding: 18px; }}
    .summary img {{ width: 100%; max-height: 220px; object-fit: contain; background: #eef1f4; border-radius: 6px; }}
    .summary p {{ margin: 6px 0; line-height: 1.6; overflow-wrap: anywhere; }}
    main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }}
    .card {{ overflow: hidden; }}
    .card img {{ width: 100%; height: 190px; object-fit: contain; display: block; background: #eef1f4; }}
    .meta {{ padding: 12px; display: grid; gap: 7px; }}
    .meta strong {{ color: #0f766e; font-size: 16px; }}
    .meta span, .meta small {{ overflow-wrap: anywhere; line-height: 1.35; }}
    .meta small {{ color: #637083; font-size: 12px; }}
    .empty {{ grid-column: 1 / -1; padding: 22px; }}
  </style>
</head>
<body>
  <header>
    <h1>Synology Image Similarity Search Results</h1>
    <section class="summary">
      <img src="{html.escape(query_path.resolve().as_uri(), quote=True)}" alt="Reference image">
      <div>
        <p><strong>Threshold: </strong>{threshold:.2f}%</p>
        <p><strong>Scanned images: </strong>{scanned}</p>
        <p><strong>Matches: </strong>{len(matches)}</p>
        <p><strong>Skipped images: </strong>{skipped}</p>
      </div>
    </section>
  </header>
  <main>{cards_html}</main>
</body>
</html>
"""
    output_path.write_text(document, encoding="utf-8")


def background_search(
    job_id: str,
    query_path: Path,
    folder: Path,
    threshold: float,
    output_dir: Path,
    force_index_scan: bool = False,
) -> None:
    try:
        set_job(
            job_id,
            status="running",
            stage="checking",
            message="Checking the shared image folder",
            scanned=0,
            total=0,
            skipped=0,
            match_count=0,
            current_file="",
        )
        init_cloud_hash_cache()
        validate_inputs(query_path, folder, threshold)
        output_dir.mkdir(parents=True, exist_ok=True)
        query_hash = image_phash(query_path)

        if not force_index_scan:
            set_job(
                job_id,
                stage="matching",
                message="Reading the local fingerprint index",
                current_file=str(LOCAL_HASH_DB if LOCAL_HASH_DB.exists() else CLOUD_HASH_DB),
                scanned=0,
                total=0,
                skipped=0,
                match_count=0,
                cached_count=0,
                percent=0,
            )
            cached_records = read_cached_local_images_for_folder(folder)
            if cached_records:
                candidates: list[tuple[float, Path]] = []
                ai_candidate_heap: list[tuple[float, int, Path]] = []
                skipped = 0
                total = len(cached_records)
                visual_count = visual_index.count() if VISUAL_SEARCH_ENABLED else 0
                visual_ready = visual_count >= max(100, int(total * 0.8))
                for scanned, record in enumerate(cached_records, start=1):
                    image_path = Path(record.remote_path)
                    try:
                        candidate_hash = imagehash.hex_to_hash(record.hash_value)
                        similarity = similarity_from_distance(query_hash - candidate_hash)
                        remember_ai_candidate(ai_candidate_heap, similarity, image_path, scanned, KIMI_CANDIDATE_COUNT * 5)
                        if similarity >= threshold and image_path.is_file():
                            candidates.append((similarity, image_path))
                    except Exception:
                        skipped += 1
                    if scanned % 5000 == 0 or scanned == total:
                        set_job(
                            job_id,
                            stage="matching",
                            message=f"Fast cached search: compared {scanned}/{total} fingerprints",
                            total=total,
                            scanned=scanned,
                            skipped=skipped,
                            match_count=len(candidates),
                            cached_count=total,
                            visual_count=visual_count,
                            current_file=record.remote_path,
                            percent=round((scanned / total) * 100, 1) if total else 100,
                        )

                ai_reasons: dict[str, str] = {}
                ai_used = False
                visual_used = False
                phash_pool = sorted(ai_candidate_heap, key=lambda item: (-item[0], item[1]))
                phash_pool_paths = [(score, path) for score, _, path in phash_pool]
                visual_pool_paths: list[tuple[float, Path]] = []
                if VISUAL_SEARCH_ENABLED and visual_ready:
                    set_job(job_id, stage="matching", message="本地 AI 正在检索视觉相似案例", percent=98)
                    visual_candidates = visual_index.search(query_path, limit=KIMI_CANDIDATE_COUNT * 5)
                    visual_pool_paths = [(item.similarity, item.path) for item in visual_candidates]
                    visual_used = bool(visual_pool_paths)

                ai_pool_paths = hybrid_candidate_pool(
                    phash_pool_paths,
                    visual_pool_paths,
                    KIMI_CANDIDATE_COUNT,
                )
                if KIMI_ENABLED and KIMI_API_KEY and ai_pool_paths:
                    set_job(job_id, stage="matching", message="Kimi 正在复核混合搜索候选案例", percent=99)
                    try:
                        matches, ai_reasons = kimi_rank_candidates(query_path, ai_pool_paths)
                        ai_used = True
                    except Exception as exc:
                        set_job(job_id, message=f"Kimi 暂时不可用，已返回本地混合搜索结果：{exc}")
                        matches = fallback_hybrid_matches(ai_pool_paths)
                else:
                    candidates.sort(key=lambda item: (-item[0], str(item[1]).lower()))
                    matches = [
                        Match(
                            rank=index,
                            similarity=round(similarity, 2),
                            filename=path.name,
                            path=path,
                        )
                        for index, (similarity, path) in enumerate(candidates, start=1)
                    ]
                csv_path = output_dir / "matches.csv"
                html_path = output_dir / "preview.html"
                write_csv(matches, csv_path)
                write_html(matches, query_path, html_path, threshold, total, skipped)
                set_job(
                    job_id,
                    status="done",
                    stage="done",
                    message=("图片指纹 + 本地 AI + Kimi 混合搜索完成" if visual_used else "图片指纹 + Kimi 智能搜索完成") if ai_used else "本地混合搜索完成",
                    scanned=total,
                    total=total,
                    skipped=skipped,
                    match_count=len(matches),
                    cached_count=total,
                    visual_count=visual_count,
                    percent=100,
                    csv_url=f"/download/{encode_path(csv_path)}",
                    html_url=f"/download/{encode_path(html_path)}",
                    result_dir=str(output_dir),
                    query_url=f"/image/{encode_path(query_path)}",
                    matches=[match_to_json(match, ai_reasons) for match in matches],
                    ai_used=ai_used,
                    visual_used=visual_used,
                )
                return

        set_job(
            job_id,
            stage="matching",
            message="Scanning shared folder and building missing fingerprints",
            current_file=str(folder),
            scanned=0,
            total=0,
            skipped=0,
            match_count=0,
            cached_count=0,
            visual_count=visual_index.count() if VISUAL_SEARCH_ENABLED else 0,
            percent=0,
        )
        candidates: list[tuple[float, Path]] = []
        ai_candidate_heap: list[tuple[float, int, Path]] = []
        scanned = 0
        discovered = 0
        skipped = 0
        cached = 0
        visual_cached = 0
        visual_created = 0
        visual_failed = 0
        cache_write_errors = 0
        pending_cache_records: list[tuple[Any, ...]] = []
        finished_discovery = False
        last_progress_at = 0.0
        current_directory = folder

        def flush_cache_records() -> None:
            nonlocal cache_write_errors
            if not pending_cache_records:
                return
            records = pending_cache_records[:]
            pending_cache_records.clear()
            try:
                save_cached_hash_batch(records)
            except Exception:
                cache_write_errors += len(records)

        def publish_progress(current_file: Path | None = None, force: bool = False) -> None:
            nonlocal last_progress_at
            now = time.time()
            if not force and now - last_progress_at < LOCAL_PROGRESS_INTERVAL_SECONDS:
                return
            last_progress_at = now
            total = discovered
            if finished_discovery:
                message = f"已处理 {scanned}/{total} 张共享图库图片；复用 {cached} 条指纹缓存"
            else:
                message = f"正在扫描共享图库：已发现 {discovered} 张，已处理 {scanned} 张；复用 {cached} 条指纹缓存"
            if cache_write_errors:
                message += f"；{cache_write_errors} 条新指纹暂时没有写入"
            if VISUAL_SEARCH_ENABLED:
                message += f"；AI 特征 {visual_cached + visual_created} 条"
            if finished_discovery:
                percent = round((scanned / total) * 100, 1) if total else 100
            else:
                percent = min(95.0, round((scanned / total) * 100, 1)) if total else 0
            set_job(
                job_id,
                stage="matching",
                message=message,
                total=total,
                scanned=scanned,
                skipped=skipped,
                match_count=len(candidates),
                cached_count=cached,
                visual_count=visual_cached + visual_created,
                cache_write_errors=cache_write_errors,
                current_file=str(current_file or current_directory or ""),
                percent=percent,
            )

        def note_directory(directory: Path) -> None:
            nonlocal current_directory
            current_directory = directory
            publish_progress(directory)

        def process_item(
            item: tuple[Path, str],
            cached_hashes: dict[str, imagehash.ImageHash],
        ) -> tuple[Path, str, imagehash.ImageHash, bool]:
            image_path, cache_key = item
            cached_hash = cached_hashes.get(cache_key)
            if cached_hash:
                return image_path, cache_key, cached_hash, True
            return image_path, cache_key, image_phash(image_path), False

        def process_batch(items: list[tuple[Path, str]]) -> None:
            nonlocal scanned, skipped, cached, visual_cached, visual_created, visual_failed
            if not items:
                return
            cached_hashes = read_cached_hashes([cache_key for _, cache_key in items])
            worker_count = max(1, min(LOCAL_SHARE_WORKERS, len(items)))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                pending = {executor.submit(process_item, item, cached_hashes) for item in items}
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        scanned += 1
                        image_path: Path | None = None
                        try:
                            image_path, cache_key, candidate_hash, from_cache = future.result()
                            if from_cache:
                                cached += 1
                            else:
                                pending_cache_records.append(
                                    cached_local_hash_record(image_path, cache_key, candidate_hash, folder)
                                )
                                if len(pending_cache_records) >= 100:
                                    flush_cache_records()
                            similarity = similarity_from_distance(query_hash - candidate_hash)
                            remember_ai_candidate(
                                ai_candidate_heap,
                                similarity,
                                image_path.resolve(),
                                scanned,
                                KIMI_CANDIDATE_COUNT * 5,
                            )
                            if similarity >= threshold:
                                candidates.append((similarity, image_path.resolve()))
                        except Exception:
                            skipped += 1
                        publish_progress(image_path)
            if VISUAL_SEARCH_ENABLED:
                set_job(job_id, message="正在建立本地 AI 视觉特征", current_file=str(items[-1][0]))
                existing_count, created_count, failed_count = visual_index.ensure_embeddings(
                    [(cache_key, image_path) for image_path, cache_key in items],
                    progress=lambda existing, created, failed: set_job(
                        job_id,
                        message=f"正在建立本地 AI 视觉特征：本批新增 {created} 条",
                        visual_count=visual_cached + visual_created + existing + created,
                    ),
                )
                visual_cached += existing_count
                visual_created += created_count
                visual_failed += failed_count

        batch: list[tuple[Path, str]] = []
        publish_progress(folder, force=True)
        for image_path in iter_share_image_files(folder, note_directory):
            discovered += 1
            try:
                batch.append((image_path, local_image_cache_key(image_path, folder)))
            except Exception:
                skipped += 1
                publish_progress(image_path)
                continue
            if len(batch) >= LOCAL_SCAN_BATCH_SIZE:
                process_batch(batch)
                batch = []
                flush_cache_records()
                publish_progress(image_path, force=True)

        if batch:
            process_batch(batch)
        finished_discovery = True

        flush_cache_records()
        publish_progress(force=True)

        phash_pool = sorted(ai_candidate_heap, key=lambda item: (-item[0], item[1]))
        phash_pool_paths = [(score, path) for score, _, path in phash_pool]
        visual_pool_paths: list[tuple[float, Path]] = []
        visual_used = False
        if VISUAL_SEARCH_ENABLED and visual_index.count() >= 100:
            set_job(job_id, message="本地 AI 正在复核补建后的搜索结果", percent=98)
            visual_candidates = visual_index.search(query_path, limit=KIMI_CANDIDATE_COUNT * 5)
            visual_pool_paths = [(item.similarity, item.path) for item in visual_candidates]
            visual_used = bool(visual_pool_paths)

        ai_pool_paths = hybrid_candidate_pool(
            phash_pool_paths,
            visual_pool_paths,
            KIMI_CANDIDATE_COUNT,
        )
        ai_reasons: dict[str, str] = {}
        ai_used = False
        if KIMI_ENABLED and KIMI_API_KEY and ai_pool_paths:
            set_job(job_id, message="Kimi 正在复核补建后的混合候选案例", percent=99)
            try:
                matches, ai_reasons = kimi_rank_candidates(query_path, ai_pool_paths)
                ai_used = True
            except Exception:
                matches = fallback_hybrid_matches(ai_pool_paths)
        else:
            candidates.sort(key=lambda item: (-item[0], str(item[1]).lower()))
            matches = [
                Match(
                    rank=index,
                    similarity=round(similarity, 2),
                    filename=path.name,
                    path=path,
                )
                for index, (similarity, path) in enumerate(candidates, start=1)
            ]
        csv_path = output_dir / "matches.csv"
        html_path = output_dir / "preview.html"
        write_csv(matches, csv_path)
        write_html(matches, query_path, html_path, threshold, scanned, skipped)

        set_job(
            job_id,
            status="done",
            stage="done",
            message=("指纹补建完成，AI + Kimi 混合搜索完成" if ai_used else "指纹补建及本地 AI 搜索完成"),
            total=scanned,
            scanned=scanned,
            skipped=skipped,
            match_count=len(matches),
            cached_count=cached,
            visual_count=visual_cached + visual_created,
            visual_failed=visual_failed,
            percent=100,
            csv_url=f"/download/{encode_path(csv_path)}",
            html_url=f"/download/{encode_path(html_path)}",
            result_dir=str(output_dir),
            query_url=f"/image/{encode_path(query_path)}",
            matches=[match_to_json(match, ai_reasons) for match in matches],
            ai_used=ai_used,
            visual_used=visual_used,
        )
    except Exception as exc:
        set_job(job_id, status="error", stage="error", message=str(exc), percent=0)


def background_synology_search(
    job_id: str,
    query_path: Path,
    server_url: str,
    username: str,
    password: str,
    remote_folder: str,
    threshold: float,
    output_dir: Path,
    force_cloud_scan: bool = False,
) -> None:
    client = SynologyClient(server_url)
    temp_dir: Path | None = None
    try:
        init_cloud_hash_cache()
        set_job(
            job_id,
            status="running",
            stage="checking",
            message="Logging in to Synology",
            scanned=0,
            total=0,
            skipped=0,
            match_count=0,
            current_file="",
            percent=0,
        )
        remote_folder = normalize_remote_path(remote_folder)
        output_dir.mkdir(parents=True, exist_ok=True)
        query_hash = image_phash(query_path)

        set_job(
            job_id,
            stage="matching",
            message="Reading the local fingerprint index",
            current_file=str(LOCAL_HASH_DB if LOCAL_HASH_DB.exists() else CLOUD_HASH_DB),
            scanned=0,
            total=0,
            skipped=0,
            match_count=0,
            cached_count=0,
            percent=0,
        )
        cached_records = read_cached_cloud_images_for_folder(remote_folder)
        if cached_records and not force_cloud_scan:
            total = len(cached_records)
            candidates: list[tuple[float, CachedCloudImage]] = []
            scanned = 0
            skipped = 0

            for record in cached_records:
                scanned += 1
                try:
                    candidate_hash = imagehash.hex_to_hash(record.hash_value)
                    similarity = similarity_from_distance(query_hash - candidate_hash)
                    if similarity >= threshold:
                        candidates.append((similarity, record))
                except Exception:
                    skipped += 1
                if scanned % 5000 == 0 or scanned == total:
                    set_job(
                        job_id,
                        stage="matching",
                        message=f"Fast cached search: compared {scanned}/{total} fingerprints",
                        scanned=scanned,
                        total=total,
                        skipped=skipped,
                        match_count=len(candidates),
                        cached_count=total,
                        percent=round((scanned / total) * 100, 1) if total else 100,
                        current_file=record.remote_path,
                    )

            candidates.sort(key=lambda item: (-item[0], item[1].remote_path.lower()))
            preview_dir = output_dir / "previews"
            preview_dir.mkdir(parents=True, exist_ok=True)
            matches: list[CloudMatch] = []

            if candidates:
                set_job(
                    job_id,
                    message="Downloading matched preview images",
                    current_file="",
                    percent=99,
                )
                client.login(username, password)

            for similarity, record in candidates:
                try:
                    stable_index = int(hashlib.sha1(record.remote_path.encode("utf-8")).hexdigest()[:8], 16)
                    preview_path = preview_dir / safe_cache_name(stable_index, record.filename)
                    if not preview_path.is_file():
                        if not record.file_id:
                            raise RuntimeError("Cached record has no cloud file id")
                        client.download(
                            SynologyFile(path=record.remote_path, name=record.filename, file_id=record.file_id),
                            preview_path,
                        )
                    matches.append(
                        CloudMatch(
                            rank=0,
                            similarity=round(similarity, 2),
                            filename=record.filename,
                            remote_path=record.remote_path,
                            local_path=preview_path,
                        )
                    )
                except Exception:
                    skipped += 1

            matches = [
                CloudMatch(
                    rank=rank,
                    similarity=match.similarity,
                    filename=match.filename,
                    remote_path=match.remote_path,
                    local_path=match.local_path,
                )
                for rank, match in enumerate(matches, start=1)
            ]

            csv_path = output_dir / "matches.csv"
            html_path = output_dir / "preview.html"
            write_cloud_csv(matches, csv_path)
            write_cloud_html(matches, query_path, html_path, threshold, scanned, skipped)

            set_job(
                job_id,
                status="done",
                stage="done",
                message="Finished with cached fingerprint index",
                scanned=scanned,
                skipped=skipped,
                match_count=len(matches),
                cached_count=total,
                total=total,
                percent=100,
                csv_url=f"/download/{encode_path(csv_path)}",
                html_url=f"/download/{encode_path(html_path)}",
                result_dir=str(output_dir),
                query_url=f"/image/{encode_path(query_path)}",
                matches=[cloud_match_to_json(match) for match in matches],
            )
            return

        if force_cloud_scan:
            set_job(
                job_id,
                stage="checking",
                message="Rebuilding missing fingerprints from Synology",
                current_file=remote_folder,
                scanned=0,
                total=0,
                skipped=0,
                match_count=0,
                cached_count=0,
                percent=0,
            )

        client.login(username, password)

        set_job(job_id, message="Reading the Synology folder list", current_file=remote_folder)
        files = client.list_images(remote_folder, SUPPORTED_EXTENSIONS)
        total = len(files)

        set_job(
            job_id,
            message="Reading the shared fingerprint cache",
            total=total,
            current_file=str(CLOUD_HASH_DB),
        )
        cache_keys = [cloud_cache_key(server_url, file) for file in files]
        try:
            cached_hashes = read_cached_hashes(cache_keys)
        except Exception:
            cached_hashes = {}

        candidates: list[tuple[float, str, str, Path]] = []
        scanned = 0
        skipped = 0
        cached = 0
        cache_write_errors = 0
        pending_cache_records: list[tuple[Any, ...]] = []

        set_job(
            job_id,
            stage="matching",
            message="Building or reusing the cloud image index",
            total=total,
            scanned=0,
            skipped=0,
            match_count=0,
            percent=0,
        )

        temp_dir = Path(tempfile.mkdtemp(prefix="cloud-hash-", dir=output_dir))
        preview_dir = output_dir / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)

        def preview_path_for(file) -> Path:
            stable_index = int(hashlib.sha1(file.path.encode("utf-8")).hexdigest()[:8], 16)
            return preview_dir / safe_cache_name(stable_index, file.name)

        def save_preview_image(file, source_path: Path | None) -> Path:
            preview_path = preview_path_for(file)
            if preview_path.is_file():
                return preview_path
            if source_path and source_path.is_file():
                shutil.copy2(source_path, preview_path)
                return preview_path
            worker = SynologyClient(server_url)
            worker.sid = client.sid
            worker.download(file, preview_path)
            return preview_path

        def process_file(file):
            cache_key = cloud_cache_key(server_url, file)
            cached_hash = cached_hashes.get(cache_key)
            if cached_hash:
                return file, None, cached_hash, True, cache_key

            local_path = temporary_cloud_image_path(temp_dir, cache_key, file.name)
            worker = SynologyClient(server_url)
            worker.sid = client.sid
            worker.download(file, local_path)
            candidate_hash = image_phash(local_path)
            return file, local_path, candidate_hash, False, cache_key

        worker_count = max(1, min(CLOUD_WORKERS, total or 1))
        max_pending = worker_count * 4

        def publish_progress(file=None, force: bool = False) -> None:
            if not force and scanned < total and scanned % 50 != 0:
                return
            percent = round((scanned / total) * 100, 1) if total else 100
            current_file = file.path if file else ""
            message = f"已处理 {scanned}/{total} 张云盘图片；复用 {cached} 条指纹缓存"
            if cache_write_errors:
                message += f"；{cache_write_errors} 条新指纹暂未写入共享库"
            set_job(
                job_id,
                current_file=current_file,
                scanned=scanned,
                total=total,
                skipped=skipped,
                match_count=len(candidates),
                cached_count=cached,
                cache_write_errors=cache_write_errors,
                percent=percent,
                message=message,
            )

        def flush_cache_records() -> None:
            nonlocal cache_write_errors
            if not pending_cache_records:
                return
            records = pending_cache_records[:]
            pending_cache_records.clear()
            try:
                save_cached_hash_batch(records)
            except Exception:
                cache_write_errors += len(records)

        def handle_future(future) -> None:
            nonlocal scanned, skipped, cached
            scanned += 1
            file = None
            try:
                file, local_path, candidate_hash, from_cache, cache_key = future.result()
                if from_cache:
                    cached += 1
                else:
                    pending_cache_records.append(cached_hash_record(server_url, file, cache_key, candidate_hash))
                    if len(pending_cache_records) >= 100:
                        flush_cache_records()
                similarity = similarity_from_distance(query_hash - candidate_hash)
                if similarity >= threshold:
                    preview_path = save_preview_image(file, local_path)
                    candidates.append((similarity, file.name, file.path, preview_path))
            except Exception:
                skipped += 1
            publish_progress(file)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            pending = set()
            file_iter = iter(files)

            while True:
                while len(pending) < max_pending:
                    try:
                        pending.add(executor.submit(process_file, next(file_iter)))
                    except StopIteration:
                        break

                if not pending:
                    break

                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    handle_future(future)

        flush_cache_records()
        publish_progress(force=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir = None

        candidates.sort(key=lambda item: (-item[0], item[2].lower()))
        matches = [
            CloudMatch(
                rank=rank,
                similarity=round(similarity, 2),
                filename=filename,
                remote_path=remote_path,
                local_path=local_path,
            )
            for rank, (similarity, filename, remote_path, local_path) in enumerate(candidates, start=1)
        ]

        csv_path = output_dir / "matches.csv"
        html_path = output_dir / "preview.html"
        write_cloud_csv(matches, csv_path)
        write_cloud_html(matches, query_path, html_path, threshold, scanned, skipped)

        set_job(
            job_id,
            status="done",
            stage="done",
            message="Finished",
            scanned=scanned,
            skipped=skipped,
            match_count=len(matches),
            percent=100,
            csv_url=f"/download/{encode_path(csv_path)}",
            html_url=f"/download/{encode_path(html_path)}",
            result_dir=str(output_dir),
            query_url=f"/image/{encode_path(query_path)}",
            matches=[cloud_match_to_json(match) for match in matches],
        )
    except Exception as exc:
        set_job(job_id, status="error", stage="error", message=str(exc), percent=0)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        try:
            client.logout()
        except Exception:
            pass


PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ app_title }}</title>
  <link rel="icon" type="image/png" sizes="64x64" href="/static/favicon.png?v=1">
  <style>
    * { box-sizing: border-box; }
    :root {
      --bg: #eef3f7;
      --panel: #ffffff;
      --line: #d8e2ea;
      --muted: #637286;
      --text: #102033;
      --accent: #0f766e;
      --accent-2: #0b5f59;
      --soft: #e8f7f5;
      --warn: #fff7ed;
      --shadow: 0 18px 50px rgba(16, 32, 51, .08);
    }
    body { margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; color: var(--text); background: radial-gradient(circle at top left, #f8fbfc 0, var(--bg) 42%, #e7eef4 100%); }
    .wrap { width: min(1240px, calc(100% - 36px)); margin: 0 auto; padding: 28px 0 46px; }
    .top { display: flex; justify-content: space-between; align-items: flex-end; gap: 18px; margin-bottom: 18px; }
    h1 { margin: 0 0 8px; font-size: 30px; letter-spacing: 0; }
    .sub { margin: 0; color: var(--muted); line-height: 1.55; }
    .statusBadge { display: inline-flex; align-items: center; min-height: 40px; padding: 0 14px; border: 1px solid #9bd8d0; border-radius: 999px; color: var(--accent); background: var(--soft); font-weight: 800; white-space: nowrap; }
    .panel { background: rgba(255,255,255,.96); border: 1px solid var(--line); border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: var(--shadow); }
    .workspaceHead { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: start; margin-bottom: 18px; }
    .workspaceHead h2 { margin: 0 0 6px; font-size: 18px; }
    .workspaceHead p { margin: 0; color: var(--muted); line-height: 1.5; }
    .cacheBox { min-width: 300px; background: #f7fafc; border: 1px solid var(--line); border-radius: 8px; padding: 12px; color: var(--muted); font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }
    .cacheBox strong { display: block; color: var(--text); font-size: 14px; margin-bottom: 4px; }
    form { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; align-items: end; }
    label, .field { display: grid; gap: 7px; font-size: 14px; font-weight: 800; }
    .span2 { grid-column: span 2; }
    .span3 { grid-column: span 3; }
    .span4 { grid-column: span 4; }
    .span6 { grid-column: span 6; }
    .span8 { grid-column: span 8; }
    .span9 { grid-column: span 9; }
    input { width: 100%; min-height: 44px; border: 1px solid #c4d0dc; border-radius: 6px; padding: 10px 11px; font: inherit; background: #fff; color: var(--text); }
    input:focus { outline: 2px solid rgba(15,118,110,.18); border-color: var(--accent); }
    input::placeholder { color: #8a98aa; }
    .imageInputRow { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
    .pasteBtn { min-width: 112px; min-height: 44px; border: 1px solid #99d5ce; border-radius: 6px; padding: 0 14px; color: var(--accent); background: #effaf8; font: inherit; font-weight: 900; cursor: pointer; }
    .pasteBtn:hover { background: #e1f6f2; border-color: var(--accent); }
    .pasteState { display: none; grid-template-columns: 44px minmax(0, 1fr); align-items: center; gap: 9px; min-height: 52px; padding: 4px 8px; border: 1px solid #b8ddd8; border-radius: 6px; background: #f2fbf9; color: var(--accent); font-size: 12px; font-weight: 800; }
    .pasteState.show { display: grid; }
    .pasteState img { width: 44px; height: 44px; object-fit: cover; border-radius: 4px; background: #e5edf2; }
    .pasteState span { overflow-wrap: anywhere; }
    .checkLabel { min-height: 44px; display: flex; align-items: center; gap: 9px; border: 1px solid #c4d0dc; border-radius: 6px; padding: 0 11px; background: #fff; font-weight: 800; }
    .checkLabel input { width: 18px; min-height: 18px; height: 18px; padding: 0; }
    .primaryBtn { width: 100%; min-height: 44px; border: 0; border-radius: 6px; padding: 0 18px; font: inherit; font-weight: 900; color: #fff; background: var(--accent); cursor: pointer; }
    .primaryBtn:hover { background: var(--accent-2); }
    .primaryBtn:disabled { background: #8aa19e; cursor: wait; }
    .advanced { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 14px; }
    .advanced summary { cursor: pointer; color: var(--accent); font-weight: 900; user-select: none; }
    .advancedGrid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; align-items: end; margin-top: 12px; }
    .quick { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
    .quick button { min-height: 34px; color: var(--accent); background: #f1fbf9; border: 1px solid #9bd8d0; border-radius: 6px; padding: 0 10px; font-weight: 800; cursor: pointer; }
    .quick button:hover { background: #e5f7f3; }
    .noteRow { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 14px; }
    .note { border: 1px solid var(--line); background: #f8fafc; border-radius: 8px; padding: 12px; color: var(--muted); line-height: 1.55; font-size: 14px; }
    .note strong { color: var(--text); }
    .error { display: none; background: #fff1f2; border: 1px solid #fecdd3; color: #9f1239; border-radius: 8px; padding: 14px 16px; margin-bottom: 16px; line-height: 1.5; }
    .progressBox { display: none; }
    .steps { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 14px; }
    .step { border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #f8fafc; }
    .step strong { display: block; margin-bottom: 4px; }
    .step span { color: var(--muted); font-size: 13px; }
    .bar { height: 12px; background: #dfe7ee; border-radius: 999px; overflow: hidden; }
    .fill { width: 0%; height: 100%; background: var(--accent); transition: width .25s ease; }
    .stats { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin: 14px 0; }
    .stat { background: #f8fafc; border: 1px solid var(--line); border-radius: 8px; padding: 12px; }
    .stat b { display: block; font-size: 22px; color: var(--accent); }
    .stat span { color: var(--muted); font-size: 13px; }
    .current { color: var(--muted); overflow-wrap: anywhere; line-height: 1.45; }
    .result { display: none; }
    .summary { display: grid; grid-template-columns: 180px 1fr; gap: 18px; align-items: start; }
    .summary img { width: 100%; height: 145px; object-fit: contain; background: #eef2f6; border-radius: 6px; border: 1px solid var(--line); }
    .summary p { margin: 5px 0; line-height: 1.55; overflow-wrap: anywhere; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
    .actions a { display: inline-flex; align-items: center; min-height: 36px; padding: 0 12px; border-radius: 6px; color: var(--accent); border: 1px solid #99d5ce; background: #effaf8; text-decoration: none; font-weight: 800; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }
    .card { background: #fff; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; box-shadow: 0 8px 24px rgba(16,32,51,.05); }
    .card img { width: 100%; height: 185px; object-fit: contain; display: block; background: #eef2f6; }
    .meta { padding: 11px; display: grid; gap: 6px; }
    .score { color: var(--accent); font-weight: 900; font-size: 16px; }
    .folder-name { color: #102033; font-weight: 900; line-height: 1.4; overflow-wrap: anywhere; }
    .folder-name span { display: block; color: var(--muted); font-size: 11px; font-weight: 700; }
    .subfolder { color: #3f556b; font-size: 12px; font-weight: 700; overflow-wrap: anywhere; }
    .name { font-weight: 800; overflow-wrap: anywhere; }
    .path { color: var(--muted); font-size: 12px; line-height: 1.35; overflow-wrap: anywhere; }
    .ai-reason { color: #0f766e; font-size: 12px; line-height: 1.45; padding-top: 6px; border-top: 1px solid var(--line); }
    .empty { background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 18px; }
    @media (max-width: 980px) { form, .advancedGrid, .workspaceHead, .noteRow, .summary, .steps, .stats { grid-template-columns: 1fr; } .span2, .span3, .span4, .span6, .span8, .span9 { grid-column: span 1; } .top { display: block; } .cacheBox { min-width: 0; } }
    @media (max-width: 560px) { .imageInputRow { grid-template-columns: 1fr; } .pasteBtn { width: 100%; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>{{ app_title }}</h1>
        <p class="sub">{{ app_subtitle }}</p>
      </div>
      <div class="statusBadge">{{ app_badge }}</div>
    </div>

    <section class="panel">
      <div class="workspaceHead">
        <div>
          <h2>&#25628;&#32034;&#24037;&#20316;&#21488;</h2>
          <p>{{ app_audience_text }}</p>
        </div>
        <div class="cacheBox">
          <strong>&#20849;&#20139;&#25351;&#32441;&#24211;&#20301;&#32622;</strong>
          {{ cloud_index_dir }}
        </div>
      </div>

      <form id="searchForm">
        <div class="field span6">
          <label for="queryInput">&#23458;&#25143;&#21442;&#32771;&#22270;</label>
          <div class="imageInputRow">
            <input id="queryInput" type="file" name="query" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" required>
            <button class="pasteBtn" id="pasteBtn" type="button">&#31896;&#36148;&#22270;&#29255;</button>
          </div>
          <div class="pasteState" id="pasteState">
            <img id="pastePreview" src="" alt="">
            <span id="pasteName"></span>
          </div>
        </div>
        <label class="span3">
          &#30456;&#20284;&#24230;&#38376;&#27099;
          <input type="number" name="threshold" min="0" max="100" step="1" value="80">
        </label>
        <label class="span3">
          &#32531;&#23384;&#27169;&#24335;
          <span class="checkLabel">
            <input type="checkbox" name="force_cloud_scan" value="1">
            &#34917;&#24314;&#32570;&#22833;&#25351;&#32441;&#21644; AI &#29305;&#24449;
          </span>
        </label>
        <input id="folderInput" type="hidden" name="folder" value="{{ default_share_folder }}">
        <input id="remoteInput" type="hidden" name="remote_folder" value="">
        <input type="hidden" name="server_url" value="">
        <input type="hidden" name="username" value="">
        <input type="hidden" name="password" value="">
        <div class="span6">
          <button class="primaryBtn" id="submitBtn" type="submit">&#24320;&#22987;&#25628;&#32034;</button>
        </div>
      </form>

      <details class="advanced">
        <summary>&#39640;&#32423;&#35774;&#32622;</summary>
        <div class="advancedGrid">
          <label class="span8">
            &#20849;&#20139;&#22270;&#24211;&#36335;&#24452;
            <input id="folderVisibleInput" type="text" value="{{ default_share_folder }}" placeholder="{{ default_share_folder }}">
          </label>
          <div class="span4 quick">
            <button type="button" data-kind="local" data-path="{{ default_share_folder }}">&#40664;&#35748;&#22270;&#24211;</button>
            <button type="button" data-kind="local" data-path="Z:\&#20135;&#21697;&#31867;&#30446;">Z &#30424;&#22791;&#29992;</button>
          </div>
        </div>
      </details>

      <div class="noteRow">
        <div class="note"><strong>&#26085;&#24120;&#20351;&#29992;</strong>&#65306;&#19978;&#20256;&#21442;&#32771;&#22270;&#21518;&#30452;&#25509;&#28857;&#8220;&#24320;&#22987;&#25628;&#32034;&#8221;&#21363;&#21487;&#12290;</div>
        <div class="note"><strong>&#25552;&#36895;&#26426;&#21046;</strong>&#65306;{% if kimi_enabled %}&#20808;&#29992;&#25351;&#32441;&#24211;&#24555;&#36895;&#26597;&#25214;&#65292;&#27809;&#26377;&#31934;&#20934;&#32467;&#26524;&#26102;&#20250;&#33258;&#21160;&#21551;&#29992; Kimi AI &#31579;&#36873;&#30456;&#20284;&#26696;&#20363;&#12290;{% else %}&#24179;&#26102;&#19981;&#21246;&#36873;&#34917;&#24314;&#65292;&#20250;&#20248;&#20808;&#29992;&#24050;&#26377;&#25351;&#32441;&#24211;&#24555;&#36895;&#25628;&#32034;&#12290;{% endif %}</div>
      </div>
    </section>

    <div id="errorBox" class="error"></div>

    <section id="progressBox" class="panel progressBox">
      <div class="steps">
        <div class="step"><strong>1. &#20445;&#23384;&#21442;&#32771;&#22270;</strong><span id="stepUpload">&#31561;&#24453;&#20013;</span></div>
        <div class="step"><strong>2. &#35835;&#21462;&#22270;&#24211;</strong><span id="stepCheck">&#31561;&#24453;&#20013;</span></div>
        <div class="step"><strong>3. &#25195;&#25551;&#24314;&#32034;&#24341;</strong><span id="stepScan">&#31561;&#24453;&#20013;</span></div>
        <div class="step"><strong>4. &#29983;&#25104;&#32467;&#26524;</strong><span id="stepDone">&#31561;&#24453;&#20013;</span></div>
      </div>
      <div class="bar"><div id="barFill" class="fill"></div></div>
      <div class="stats">
        <div class="stat"><b id="scanned">0</b><span>&#24050;&#25195;&#25551;</span></div>
        <div class="stat"><b id="total">0</b><span>&#22270;&#29255;&#24635;&#25968;</span></div>
        <div class="stat"><b id="matches">0</b><span>&#24050;&#25214;&#21040;</span></div>
        <div class="stat"><b id="skipped">0</b><span>&#36339;&#36807;&#22270;&#29255;</span></div>
        <div class="stat"><b id="cached">0</b><span>&#22797;&#29992;&#32531;&#23384;</span></div>
        <div class="stat"><b id="visual">0</b><span>AI &#29305;&#24449;</span></div>
      </div>
      <div class="current"><strong>&#24403;&#21069;&#29366;&#24577;&#65306;</strong><span id="message">&#20934;&#22791;&#20013;</span></div>
      <div class="current"><strong>&#24403;&#21069;&#25991;&#20214;&#65306;</strong><span id="currentFile">-</span></div>
    </section>

    <section id="resultBox" class="panel result">
      <div class="summary">
        <img id="queryPreview" src="" alt="">
        <div>
          <p><strong>&#32467;&#26524;&#25991;&#20214;&#22841;&#65306;</strong><span id="resultDir"></span></p>
          <p><strong>&#25628;&#32034;&#32467;&#26524;&#65306;</strong><span id="resultCount"></span></p>
          <div class="actions">
            <a id="csvLink" href="#" target="_blank">&#25171;&#24320; CSV</a>
            <a id="htmlLink" href="#" target="_blank">&#25171;&#24320; HTML &#39044;&#35272;</a>
          </div>
        </div>
      </div>
    </section>
    <section id="cards" class="grid"></section>
  </div>

  <script>
    const form = document.getElementById('searchForm');
    const submitBtn = document.getElementById('submitBtn');
    const progressBox = document.getElementById('progressBox');
    const resultBox = document.getElementById('resultBox');
    const errorBox = document.getElementById('errorBox');
    const cards = document.getElementById('cards');
    const folderInput = document.getElementById('folderInput');
    const folderVisibleInput = document.getElementById('folderVisibleInput');
    const remoteInput = document.getElementById('remoteInput');
    const queryInput = document.getElementById('queryInput');
    const pasteBtn = document.getElementById('pasteBtn');
    const pasteState = document.getElementById('pasteState');
    const pastePreview = document.getElementById('pastePreview');
    const pasteName = document.getElementById('pasteName');
    let timer = null;
    let pollErrorCount = 0;
    let pastedPreviewUrl = '';
    const POLL_INTERVAL_MS = 2000;
    const POLL_MAX_RETRIES = 30;

    const T = {
      searching: '\u641c\u7d22\u4e2d...',
      start: '\u5f00\u59cb\u641c\u7d22',
      done: '\u5df2\u5b8c\u6210',
      running: '\u8fdb\u884c\u4e2d',
      waiting: '\u7b49\u5f85\u4e2d',
      submitting: '\u6b63\u5728\u63d0\u4ea4\u4efb\u52a1',
      started: '\u4efb\u52a1\u5df2\u5f00\u59cb',
      failed: '\u641c\u7d22\u5931\u8d25',
      submitFailed: '\u63d0\u4ea4\u5931\u8d25',
      countSuffix: ' \u5f20',
      empty: '\u6ca1\u6709\u627e\u5230\u5408\u9002\u7684\u76f8\u4f3c\u56fe\u7247\u3002'
    };

    function text(id, value) { document.getElementById(id).textContent = value; }

    function useImageBlob(blob, filename) {
      if (!blob) return false;
      const mime = blob.type || 'image/png';
      if (!mime.startsWith('image/')) return false;
      const extension = mime.split('/')[1]?.replace('jpeg', 'jpg') || 'png';
      const file = new File([blob], filename || `pasted-image.${extension}`, { type: mime });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      queryInput.files = transfer.files;
      if (pastedPreviewUrl) URL.revokeObjectURL(pastedPreviewUrl);
      pastedPreviewUrl = URL.createObjectURL(file);
      pastePreview.src = pastedPreviewUrl;
      pasteName.textContent = `已选择：${file.name}`;
      pasteState.classList.add('show');
      return true;
    }

    queryInput.addEventListener('change', () => {
      const file = queryInput.files?.[0];
      if (file) useImageBlob(file, file.name);
    });

    document.addEventListener('paste', event => {
      const item = Array.from(event.clipboardData?.items || []).find(entry => entry.type.startsWith('image/'));
      if (!item) return;
      event.preventDefault();
      useImageBlob(item.getAsFile(), 'pasted-image.png');
    });

    pasteBtn.addEventListener('click', async () => {
      try {
        const items = await navigator.clipboard.read();
        for (const item of items) {
          const type = item.types.find(value => value.startsWith('image/'));
          if (type && useImageBlob(await item.getType(type), `pasted-image.${type.split('/')[1].replace('jpeg', 'jpg')}`)) return;
        }
        throw new Error('剪贴板中没有图片');
      } catch (error) {
        pasteName.textContent = error.message || '无法读取剪贴板图片';
        pastePreview.removeAttribute('src');
        pasteState.classList.add('show');
      }
    });

    document.querySelectorAll('.quick button').forEach(button => {
      button.addEventListener('click', () => {
        if (button.dataset.kind === 'remote') {
          remoteInput.value = button.dataset.path;
        } else {
          folderInput.value = button.dataset.path;
          if (folderVisibleInput) folderVisibleInput.value = button.dataset.path;
        }
      });
    });

    if (folderVisibleInput) {
      folderVisibleInput.addEventListener('input', () => {
        folderInput.value = folderVisibleInput.value;
      });
    }

    function resetPage() {
      if (folderVisibleInput) folderInput.value = folderVisibleInput.value;
      errorBox.style.display = 'none';
      resultBox.style.display = 'none';
      cards.innerHTML = '';
      progressBox.style.display = 'block';
      submitBtn.disabled = true;
      submitBtn.textContent = T.searching;
      text('stepUpload', T.done);
      text('stepCheck', T.running);
      text('stepScan', T.waiting);
      text('stepDone', T.waiting);
      document.getElementById('barFill').style.width = '0%';
      ['scanned', 'total', 'matches', 'skipped', 'cached', 'visual'].forEach(id => text(id, '0'));
      text('message', T.submitting);
      text('currentFile', '-');
    }

    function showError(message) {
      errorBox.textContent = message;
      errorBox.style.display = 'block';
      submitBtn.disabled = false;
      submitBtn.textContent = T.start;
      if (timer) clearInterval(timer);
    }

    function showWarning(message) {
      errorBox.textContent = message;
      errorBox.style.display = 'block';
    }

    function clearWarning() {
      errorBox.style.display = 'none';
    }

    function updateProgress(job) {
      text('scanned', job.scanned || 0);
      text('total', job.total || 0);
      text('matches', job.match_count || 0);
      text('skipped', job.skipped || 0);
      text('cached', job.cached_count || 0);
      text('visual', job.visual_count || 0);
      text('message', job.message || '-');
      text('currentFile', job.current_file || '-');
      document.getElementById('barFill').style.width = (job.percent || 0) + '%';

      if (job.stage === 'matching') {
        text('stepCheck', T.done);
        text('stepScan', T.running);
      }
      if (job.status === 'done') {
        text('stepScan', T.done);
        text('stepDone', T.done);
        renderResults(job);
        submitBtn.disabled = false;
        submitBtn.textContent = T.start;
        if (timer) clearInterval(timer);
      }
      if (job.status === 'error') {
        showError(job.message || T.failed);
      }
    }

    function renderResults(job) {
      resultBox.style.display = 'block';
      document.getElementById('queryPreview').src = job.query_url || '';
      text('resultDir', job.result_dir || '');
      text('resultCount', (job.match_count || 0) + T.countSuffix);
      document.getElementById('csvLink').href = job.csv_url || '#';
      document.getElementById('htmlLink').href = job.html_url || '#';
      cards.innerHTML = '';

      if (!job.matches || job.matches.length === 0) {
        cards.innerHTML = '<div class="empty">' + T.empty + '</div>';
        return;
      }

      for (const match of job.matches) {
        const card = document.createElement('article');
        card.className = 'card';
        card.innerHTML = `
          <a href="${match.image_url}" target="_blank"><img src="${match.image_url}" alt=""></a>
          <div class="meta">
            <div class="score">#${match.rank} - ${match.similarity}%</div>
            <div class="folder-name"><span>\u9879\u76ee\u6587\u4ef6\u5939</span></div>
            <div class="subfolder"></div>
            <div class="name"></div>
            <div class="path"></div>
            <div class="ai-reason" style="display:none"></div>
          </div>`;
        card.querySelector('.folder-name').append(document.createTextNode(match.project_folder || '-'));
        card.querySelector('.subfolder').textContent = '\u56fe\u7247\u6587\u4ef6\u5939\uff1a' + (match.image_folder || '-');
        card.querySelector('.name').textContent = match.filename;
        card.querySelector('.path').textContent = match.path;
        if (match.ai_reason) {
          const reason = card.querySelector('.ai-reason');
          reason.textContent = '\u0041\u0049\u5224\u65ad\uff1a' + match.ai_reason;
          reason.style.display = 'block';
        }
        cards.appendChild(card);
      }
    }

    async function poll(jobId) {
      const response = await fetch(`/api/progress/${jobId}`, { cache: 'no-store' });
      const job = await response.json();
      if (!response.ok) throw new Error(job.message || T.failed);
      pollErrorCount = 0;
      clearWarning();
      updateProgress(job);
    }

    function handlePollFailure(jobId, err) {
      pollErrorCount += 1;
      if (pollErrorCount >= POLL_MAX_RETRIES) {
        showError('\u9762\u677f\u8fde\u63a5\u8d85\u65f6\uff0c\u8bf7\u5237\u65b0\u9875\u9762\u6216\u91cd\u65b0\u6253\u5f00\u9762\u677f\u3002\u5982\u679c\u540e\u53f0\u8fd8\u5728\u626b\u63cf\uff0c\u5df2\u751f\u6210\u7684\u6307\u7eb9\u4e0d\u4f1a\u4e22\u5931\u3002');
        return;
      }
      showWarning(`\u8fde\u63a5\u77ed\u6682\u4e0d\u7a33\u5b9a\uff0c\u6b63\u5728\u81ea\u52a8\u91cd\u8bd5 ${pollErrorCount}/${POLL_MAX_RETRIES}\u2026`);
      if (!timer) {
        timer = setInterval(() => poll(jobId).catch(err => handlePollFailure(jobId, err)), POLL_INTERVAL_MS);
      }
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      resetPage();
      pollErrorCount = 0;
      try {
        const response = await fetch('/api/start', { method: 'POST', body: new FormData(form), cache: 'no-store' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || T.submitFailed);
        text('message', T.started);
        timer = setInterval(() => poll(data.job_id).catch(err => handlePollFailure(data.job_id, err)), POLL_INTERVAL_MS);
        poll(data.job_id).catch(err => handlePollFailure(data.job_id, err));
      } catch (err) {
        showError(err.message);
      }
    });
  </script>
</body>
</html>
"""


@app.route("/")
def index() -> str:
    return render_template_string(
        PAGE,
        default_share_folder=str(DEFAULT_SHARE_FOLDER),
        cloud_index_dir=str(CLOUD_INDEX_DIR),
        app_title=APP_TITLE,
        app_subtitle=APP_SUBTITLE,
        app_audience_text=APP_AUDIENCE_TEXT,
        app_badge=APP_BADGE,
        kimi_enabled=KIMI_ENABLED and bool(KIMI_API_KEY),
    )


@app.route("/api/start", methods=["POST"])
def start_job():
    uploaded_file = request.files.get("query")
    folder_value = request.form.get("folder", "").strip() or str(DEFAULT_SHARE_FOLDER)
    server_url = request.form.get("server_url", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    remote_folder = request.form.get("remote_folder", "").strip()
    threshold_value = request.form.get("threshold", "80")
    force_cloud_scan = request.form.get("force_cloud_scan") == "1"

    try:
        if not uploaded_file or not uploaded_file.filename:
            raise ValueError("Please choose a reference image.")
        if not allowed_upload(uploaded_file.filename):
            raise ValueError("Only jpg, jpeg, png and webp are supported.")
        threshold = float(threshold_value or 80)
        if not 0 <= threshold <= 100:
            raise ValueError("Similarity must be between 0 and 100.")

        use_synology = bool(server_url and username and password and remote_folder)
        if not use_synology and not folder_value.strip():
            folder_value = str(DEFAULT_SHARE_FOLDER)
        folder = clean_folder_input(folder_value) if not use_synology else None
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        job_id = uuid.uuid4().hex
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        WEB_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        safe_name = secure_filename(uploaded_file.filename) or f"query{Path(uploaded_file.filename).suffix}"
        query_path = (UPLOAD_DIR / f"{timestamp}-{job_id}-{safe_name}").resolve()
        uploaded_file.save(query_path)
        output_dir = (WEB_RESULTS_DIR / f"{timestamp}-{job_id[:8]}").resolve()

        set_job(
            job_id,
            status="queued",
            stage="queued",
            message="Task queued",
            scanned=0,
            total=0,
            skipped=0,
            match_count=0,
            percent=0,
            current_file="",
        )
        if use_synology:
            thread = threading.Thread(
                target=background_synology_search,
                args=(
                    job_id,
                    query_path,
                    server_url,
                    username,
                    password,
                    remote_folder,
                    threshold,
                    output_dir,
                    force_cloud_scan,
                ),
                daemon=True,
            )
        else:
            thread = threading.Thread(
                target=background_search,
                args=(job_id, query_path, folder, threshold, output_dir, force_cloud_scan),
                daemon=True,
            )
        thread.start()
        return jsonify({"job_id": job_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/progress/<job_id>")
def progress(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"status": "error", "message": "Task not found"}), 404
    return jsonify(job)


@app.route("/api/path-check", methods=["POST"])
def path_check():
    data = request.get_json(silent=True) or {}
    return jsonify(path_exists_label(data.get("path", "")))


@app.route("/image/<token>")
def local_image(token: str):
    path = decode_path(token)
    if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        abort(404)
    return send_file(path)


@app.route("/download/<token>")
def download_file(token: str):
    path = decode_path(token)
    if not path.is_file():
        abort(404)
    if path.suffix.lower() == ".csv":
        return send_file(path, as_attachment=False, mimetype="text/csv")
    if path.suffix.lower() == ".html":
        return send_file(path, as_attachment=False, mimetype="text/html")
    abort(404)


@app.route("/health")
def health() -> Response:
    return Response("ok", mimetype="text/plain")


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
