"""SQLite primary data store — the durable source of truth for query counts.

Why SQLite? The assignment must be "easy to run locally" with no external
services, and SQLite gives us a real, on-disk, transactional store with zero
setup. The schema is deliberately tiny:

    queries(query TEXT PRIMARY KEY, count INTEGER, last_searched REAL)

Everything that serves traffic fast (the Trie index and the distributed cache)
is *derived* from this table and rebuilt on startup. Durable writes only ever
happen here, and they only happen in **batches** (see ``batch_writer.py``), which
is the whole point of the write-reduction design.

The store also counts physical reads/writes so the performance report can show
how many DB writes the batch layer eliminated.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import threading
import time
from typing import Dict, Iterator, List, Optional, Tuple


class PrimaryStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # check_same_thread=False: the connection is shared between the request
        # threadpool and the batch-writer thread, guarded by our own lock.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # Pragmas: WAL keeps reads non-blocking during the batch writer's commits.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        # Observability counters.
        self.rows_written = 0   # total rows physically upserted
        self.write_batches = 0  # number of flush transactions
        self.reads = 0          # point/range reads served from the DB

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queries (
                    query         TEXT PRIMARY KEY,
                    count         INTEGER NOT NULL DEFAULT 0,
                    last_searched REAL
                )
                """
            )
            self._conn.commit()

    # --- Ingestion ---------------------------------------------------------
    def is_empty(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM queries LIMIT 1").fetchone()
        return row is None

    def row_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM queries").fetchone()
        return int(row["c"])

    def bulk_load_csv(self, csv_path: str, batch: int = 10_000) -> int:
        """Load a ``query,count`` CSV into the table. Returns rows loaded."""
        loaded = 0
        rows: List[Tuple[str, int, float]] = []
        now = time.time()
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                q = (r.get("query") or "").strip()
                if not q:
                    continue
                try:
                    c = int(r.get("count") or 0)
                except ValueError:
                    c = 0
                rows.append((q, c, now))
                if len(rows) >= batch:
                    loaded += self._insert_many(rows)
                    rows.clear()
        if rows:
            loaded += self._insert_many(rows)
        return loaded

    def _insert_many(self, rows: List[Tuple[str, int, float]]) -> int:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO queries(query, count, last_searched) "
                "VALUES (?, ?, ?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def iter_all(self) -> Iterator[Tuple[str, int]]:
        """Stream every (query, count) — used to build the in-memory Trie."""
        with self._lock:
            cur = self._conn.execute("SELECT query, count FROM queries")
            while True:
                chunk = cur.fetchmany(10_000)
                if not chunk:
                    break
                for row in chunk:
                    yield row["query"], int(row["count"])

    # --- Reads -------------------------------------------------------------
    def get(self, query: str) -> Optional[int]:
        with self._lock:
            self.reads += 1
            row = self._conn.execute(
                "SELECT count FROM queries WHERE query = ?", (query,)
            ).fetchone()
        return int(row["count"]) if row else None

    # --- Batched writes ----------------------------------------------------
    def apply_batch(self, deltas: Dict[str, int]) -> Dict[str, int]:
        """Apply an aggregated batch of count increments in one transaction.

        ``deltas`` maps query -> number of times it was searched since the last
        flush. Returns query -> new authoritative count, so callers can refresh
        their in-memory index. This is the *only* write path during normal
        operation, and it writes one row per distinct query rather than one row
        per search request — that is the write reduction.
        """
        if not deltas:
            return {}
        now = time.time()
        items = list(deltas.items())
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO queries(query, count, last_searched)
                VALUES (:q, :d, :ts)
                ON CONFLICT(query) DO UPDATE SET
                    count = count + excluded.count,
                    last_searched = excluded.last_searched
                """,
                [{"q": q, "d": d, "ts": now} for q, d in items],
            )
            self._conn.commit()
            self.rows_written += len(items)
            self.write_batches += 1
            # Read back the authoritative counts for the affected queries.
            placeholders = ",".join("?" for _ in items)
            cur = self._conn.execute(
                f"SELECT query, count FROM queries WHERE query IN ({placeholders})",
                [q for q, _ in items],
            )
            return {row["query"]: int(row["count"]) for row in cur.fetchall()}

    def stats(self) -> Dict[str, int]:
        return {
            "rows_written": self.rows_written,
            "write_batches": self.write_batches,
            "reads": self.reads,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
