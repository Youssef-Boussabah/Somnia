"""Explicit runtime configuration. Values come from the environment, optionally seeded from .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal KEY=VALUE loader; never overrides variables already set in the environment."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    database_url: str
    artifact_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        url = os.environ.get("SOMNIA_DATABASE_URL")
        if not url:
            raise RuntimeError("SOMNIA_DATABASE_URL is not set (see .env.example)")
        return cls(database_url=url, artifact_dir=Path(os.environ.get("SOMNIA_ARTIFACT_DIR", "var/artifacts")))
