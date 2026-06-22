"""Batch writer — absorbs search write-pressure off the primary store.

Every ``POST /search`` must update a query's count, but writing to SQLite on the
request path means one disk transaction per keystroke-search. Instead, searches
are *buffered in memory and aggregated*: repeated queries collapse into a single
``query -> delta`` entry, and a background thread flushes the whole batch in one
transaction. The flush fires whichever comes first:

* a time trigger (every ``interval`` seconds), or
* a size trigger (the buffer reaches ``max_size`` distinct queries).

Because repeated searches for the same query coalesce, N search requests become
far fewer than N DB writes — the writer reports the exact ratio so the
performance write-up can quantify the reduction.

**Failure trade-off (called out in the brief):** the buffer is in memory. If the
process crashes between flushes, un-flushed search increments are lost — at most
one window's worth (``interval`` seconds, or ``max_size`` queries). That is the
deliberate price for not doing a synchronous durable write per request. The
counts being approximate for a few seconds is acceptable for a suggestion
ranking; a production system wanting durability would front this with an
append-only log / Kafka and replay it, at the cost of latency and complexity.
``stop()`` performs a final flush so a *graceful* shutdown loses nothing.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict


class BatchWriter:
    def __init__(
        self,
        flush_callback: Callable[[Dict[str, int]], None],
        interval_seconds: float = 2.0,
        max_size: int = 500,
    ) -> None:
        self._flush_callback = flush_callback
        self.interval = interval_seconds
        self.max_size = max_size

        self._buffer: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._flush_now = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread = None  # type: ignore[assignment]

        # Observability.
        self.total_received = 0      # search events buffered
        self.total_rows_written = 0  # distinct queries actually flushed to DB
        self.flush_count = 0
        self.last_flush_rows = 0

    # --- producer side -----------------------------------------------------
    def enqueue(self, query: str) -> None:
        """Buffer one search; coalesces with repeats of the same query."""
        with self._lock:
            self._buffer[query] = self._buffer.get(query, 0) + 1
            self.total_received += 1
            should_flush = len(self._buffer) >= self.max_size
        if should_flush:
            self._flush_now.set()  # trip the size trigger

    # --- consumer side -----------------------------------------------------
    def _drain(self) -> Dict[str, int]:
        with self._lock:
            if not self._buffer:
                return {}
            batch = self._buffer
            self._buffer = {}
            return batch

    def _flush(self) -> None:
        batch = self._drain()
        if not batch:
            return
        self._flush_callback(batch)  # store.apply_batch + index update + invalidation
        self.flush_count += 1
        self.last_flush_rows = len(batch)
        self.total_rows_written += len(batch)

    def _run(self) -> None:
        while not self._stop.is_set():
            # Wake on the time trigger OR an early size trigger, whichever first.
            self._flush_now.wait(timeout=self.interval)
            self._flush_now.clear()
            self._flush()
        self._flush()  # final flush on shutdown so a graceful stop loses nothing

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="batch-writer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._flush_now.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # --- stats -------------------------------------------------------------
    def stats(self) -> Dict[str, object]:
        with self._lock:
            buffered = len(self._buffer)
        rows = self.total_rows_written
        received = self.total_received
        return {
            "search_events_received": received,
            "db_rows_written": rows,
            "writes_saved": max(0, received - rows),
            "write_reduction_ratio": round(received / rows, 2) if rows else 0.0,
            "flush_count": self.flush_count,
            "last_flush_rows": self.last_flush_rows,
            "current_buffer_size": buffered,
            "flush_interval_seconds": self.interval,
            "max_batch_size": self.max_size,
        }
