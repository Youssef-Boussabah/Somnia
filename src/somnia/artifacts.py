"""Content-addressed artifact store: bytes on disk, metadata row in PostgreSQL."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from .errors import IntegrityFailure
from .hashing import sha256_bytes


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def put(self, conn: psycopg.Connection, data: bytes, *, media_type: str, origin: str,
            sensitivity: str = "internal") -> str:
        """Store bytes (idempotent). The file is written before the row, so a rolled-back
        transaction can leave only a harmless unreferenced content-addressed file."""
        sha = sha256_bytes(data)
        path = self._path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        conn.execute(
            "INSERT INTO artifacts (sha256, media_type, byte_size, storage_uri, sensitivity, origin)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING",
            (sha, media_type, len(data), f"{sha[:2]}/{sha}", sensitivity, origin),
        )
        return sha

    def get(self, sha: str) -> bytes:
        path = self._path(sha)
        if not path.is_file():
            raise IntegrityFailure(f"artifact {sha} missing from store")
        data = path.read_bytes()
        if sha256_bytes(data) != sha:
            raise IntegrityFailure(f"artifact {sha} content does not match its hash")
        return data

    def verify(self, sha: str) -> bool:
        try:
            self.get(sha)
            return True
        except IntegrityFailure:
            return False
