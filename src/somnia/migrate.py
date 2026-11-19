"""Forward-only SQL migrations with checksum verification.

Each file ``NNNN_name.sql`` in ``somnia/migrations`` is applied once, inside a transaction,
under an advisory lock. Applied checksums are verified on every run so an edited migration
is detected instead of silently diverging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

import psycopg

from .hashing import sha256_text

_LOCK_KEY = 0x50_4D_4E_49_41  # arbitrary constant advisory-lock key for Somnia migrations
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    sha256: str


def available_migrations() -> list[Migration]:
    out: list[Migration] = []
    for entry in resources.files("somnia.migrations").iterdir():
        m = _NAME.match(entry.name)
        if not m:
            continue
        sql = entry.read_text(encoding="utf-8").replace("\r\n", "\n")
        out.append(Migration(int(m.group(1)), m.group(2), sql, sha256_text(sql)))
    out.sort(key=lambda x: x.version)
    versions = [m.version for m in out]
    if versions != list(range(1, len(out) + 1)):
        raise RuntimeError(f"migration versions must be contiguous from 1: {versions}")
    return out


def _ensure_table(conn: psycopg.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS public.somnia_schema_migrations ("
        " version integer PRIMARY KEY, name text NOT NULL, sha256 text NOT NULL,"
        " applied_at timestamptz NOT NULL DEFAULT clock_timestamp())"
    )


def migrate(conn: psycopg.Connection, *, target: int | None = None) -> list[int]:
    """Apply pending migrations (up to ``target`` if given). Returns the versions applied by this call."""
    applied_now: list[int] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        _ensure_table(conn)
        rows = conn.execute("SELECT version, sha256 FROM public.somnia_schema_migrations").fetchall()
        applied = {r["version"]: r["sha256"] for r in rows}
        for mig in available_migrations():
            if target is not None and mig.version > target:
                break
            if mig.version in applied:
                if applied[mig.version] != mig.sha256:
                    raise RuntimeError(
                        f"migration {mig.version:04d}_{mig.name} was modified after being applied"
                    )
                continue
            conn.execute(mig.sql)
            conn.execute(
                "INSERT INTO public.somnia_schema_migrations (version, name, sha256) VALUES (%s, %s, %s)",
                (mig.version, mig.name, mig.sha256),
            )
            applied_now.append(mig.version)
        conn.execute("SET search_path = somnia, public")
    return applied_now


def status(conn: psycopg.Connection) -> list[dict]:
    with conn.transaction():
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT version, name, sha256, applied_at FROM public.somnia_schema_migrations ORDER BY version"
        ).fetchall()
    applied = {r["version"]: r for r in rows}
    report = []
    for mig in available_migrations():
        row = applied.get(mig.version)
        report.append(
            {
                "version": mig.version,
                "name": mig.name,
                "state": "pending" if row is None else ("ok" if row["sha256"] == mig.sha256 else "MODIFIED"),
                "applied_at": None if row is None else row["applied_at"].isoformat(),
            }
        )
    return report
