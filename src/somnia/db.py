"""PostgreSQL connections. Connections are autocommit; atomic units use explicit transactions."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

SEARCH_PATH = "somnia,public"


def connect(url: str) -> psycopg.Connection:
    """Open an autocommit connection; wrap each atomic unit in ``with conn.transaction():``."""
    return psycopg.connect(
        url,
        autocommit=True,
        row_factory=dict_row,
        options=f"-c search_path={SEARCH_PATH}",
    )
