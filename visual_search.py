"""Persistent local CLIP image embeddings for semantic image search."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class VisualCandidate:
    similarity: float
    path: Path


ProgressCallback = Callable[[int, int, int], None]


class VisualSearchIndex:
    def __init__(
        self,
        index_dir: Path,
        share_id: str,
        model_id: str = "openai/clip-vit-base-patch32",
        batch_size: int = 8,
        torch_threads: int = 6,
    ) -> None:
        self.index_dir = Path(index_dir)
        self.db_path = self.index_dir / "visual_cache.sqlite3"
        self.model_cache_dir = self.index_dir / "visual_models"
        self.share_id = share_id
        self.model_id = model_id
        self.batch_size = max(1, batch_size)
        self.torch_threads = max(1, torch_threads)
        self._model = None
        self._processor = None
        self._torch = None
        self._model_lock = threading.Lock()
        self._matrix_lock = threading.Lock()
        self._matrix_signature: tuple[int, int] | None = None
        self._matrix: np.ndarray | None = None
        self._paths: list[str] = []

    def initialize(self) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=60) as conn:
            conn.execute("PRAGMA busy_timeout = 60000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS visual_embeddings (
                    cache_key TEXT NOT NULL,
                    share_id TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (cache_key, share_id, model_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_visual_share_model "
                "ON visual_embeddings (share_id, model_id)"
            )

    def count(self) -> int:
        if not self.db_path.exists():
            return 0
        with sqlite3.connect(self.db_path, timeout=60) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM visual_embeddings WHERE share_id = ? AND model_id = ?",
                (self.share_id, self.model_id),
            ).fetchone()
        return int(row[0] if row else 0)

    def _load_model(self):
        with self._model_lock:
            if self._model is not None:
                return self._torch, self._model, self._processor
            self.model_cache_dir.mkdir(parents=True, exist_ok=True)
            import torch
            from transformers import CLIPModel, CLIPProcessor

            torch.set_num_threads(self.torch_threads)
            processor = CLIPProcessor.from_pretrained(self.model_id, cache_dir=self.model_cache_dir)
            model = CLIPModel.from_pretrained(self.model_id, cache_dir=self.model_cache_dir)
            model.eval()
            self._torch = torch
            self._model = model
            self._processor = processor
            return torch, model, processor

    def _embed_paths(self, paths: list[Path]) -> tuple[list[Path], np.ndarray, int]:
        torch, model, processor = self._load_model()
        valid_paths: list[Path] = []
        images: list[Image.Image] = []
        failed = 0
        for path in paths:
            try:
                with Image.open(path) as source:
                    images.append(source.convert("RGB"))
                valid_paths.append(path)
            except Exception:
                failed += 1
        if not images:
            return [], np.empty((0, 512), dtype=np.float16), failed
        try:
            inputs = processor(images=images, return_tensors="pt")
            with torch.inference_mode():
                features = model.get_image_features(**inputs)
                if not isinstance(features, torch.Tensor):
                    if getattr(features, "image_embeds", None) is not None:
                        features = features.image_embeds
                    elif getattr(features, "pooler_output", None) is not None:
                        features = features.pooler_output
                    else:
                        features = features[0]
                features = features / features.norm(dim=-1, keepdim=True).clamp(min=1e-12)
            matrix = features.detach().cpu().numpy().astype(np.float16)
            return valid_paths, matrix, failed
        finally:
            for image in images:
                image.close()

    def embed_query(self, path: Path) -> np.ndarray:
        paths, matrix, failed = self._embed_paths([path])
        if failed or not paths:
            raise ValueError("无法读取参考图并生成 AI 视觉特征")
        return matrix[0].astype(np.float32)

    def _existing_keys(self, cache_keys: list[str]) -> set[str]:
        existing: set[str] = set()
        if not cache_keys or not self.db_path.exists():
            return existing
        with sqlite3.connect(self.db_path, timeout=60) as conn:
            conn.execute("PRAGMA busy_timeout = 60000")
            for start in range(0, len(cache_keys), 400):
                chunk = cache_keys[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT cache_key FROM visual_embeddings "
                    f"WHERE share_id = ? AND model_id = ? AND cache_key IN ({placeholders})",
                    [self.share_id, self.model_id, *chunk],
                ).fetchall()
                existing.update(str(row[0]) for row in rows)
        return existing

    def ensure_embeddings(
        self,
        items: Iterable[tuple[str, Path]],
        progress: ProgressCallback | None = None,
    ) -> tuple[int, int, int]:
        self.initialize()
        unique: dict[str, Path] = {}
        for cache_key, path in items:
            unique.setdefault(cache_key, path)
        existing = self._existing_keys(list(unique))
        missing = [(key, path) for key, path in unique.items() if key not in existing]
        created = 0
        failed = 0
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start : start + self.batch_size]
            batch_paths = [path for _, path in batch]
            valid_paths, matrix, read_failed = self._embed_paths(batch_paths)
            failed += read_failed
            key_by_path = {str(path): key for key, path in batch}
            records = []
            for path, embedding in zip(valid_paths, matrix):
                cache_key = key_by_path[str(path)]
                records.append(
                    (
                        cache_key,
                        self.share_id,
                        str(path),
                        path.name,
                        self.model_id,
                        int(embedding.shape[0]),
                        embedding.tobytes(),
                        time.time(),
                    )
                )
            if records:
                with sqlite3.connect(self.db_path, timeout=60) as conn:
                    conn.execute("PRAGMA busy_timeout = 60000")
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO visual_embeddings
                        (cache_key, share_id, remote_path, filename, model_id, dimensions, embedding, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        records,
                    )
                created += len(records)
            if progress:
                progress(len(existing), created, failed)
        if created:
            with self._matrix_lock:
                self._matrix_signature = None
                self._matrix = None
                self._paths = []
        return len(existing), created, failed

    def _database_signature(self) -> tuple[int, int]:
        stat = self.db_path.stat()
        return stat.st_size, stat.st_mtime_ns

    def _load_matrix(self) -> tuple[np.ndarray, list[str]]:
        self.initialize()
        signature = self._database_signature()
        with self._matrix_lock:
            if self._matrix is not None and self._matrix_signature == signature:
                return self._matrix, self._paths
            with sqlite3.connect(self.db_path, timeout=120) as conn:
                conn.execute("PRAGMA busy_timeout = 120000")
                rows = conn.execute(
                    "SELECT remote_path, dimensions, embedding FROM visual_embeddings "
                    "WHERE share_id = ? AND model_id = ? ORDER BY cache_key",
                    (self.share_id, self.model_id),
                ).fetchall()
            paths: list[str] = []
            vectors: list[np.ndarray] = []
            for remote_path, dimensions, blob in rows:
                vector = np.frombuffer(blob, dtype=np.float16, count=int(dimensions)).astype(np.float32)
                if vector.size:
                    paths.append(str(remote_path))
                    vectors.append(vector)
            matrix = np.vstack(vectors) if vectors else np.empty((0, 512), dtype=np.float32)
            self._matrix_signature = signature
            self._matrix = matrix
            self._paths = paths
            return matrix, paths

    def search(self, query_path: Path, limit: int = 50) -> list[VisualCandidate]:
        query = self.embed_query(query_path)
        matrix, paths = self._load_matrix()
        if matrix.size == 0 or not paths:
            return []
        scores = matrix @ query
        count = min(max(1, limit), len(paths))
        indexes = np.argpartition(scores, -count)[-count:]
        indexes = indexes[np.argsort(scores[indexes])[::-1]]
        return [
            VisualCandidate(similarity=round(float(scores[index]) * 100, 2), path=Path(paths[index]))
            for index in indexes
            if Path(paths[index]).is_file()
        ]
