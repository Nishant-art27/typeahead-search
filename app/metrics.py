"""Lightweight metrics: suggestion latency percentiles + request counters.

The assignment's non-functional section asks for measured latency (ideally p95),
cache hit rate, and DB read/write counts. Cache and DB counters live in their own
modules; this collector owns request counts and a rolling latency window from
which it computes p50/p95/p99 on demand.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict


class Metrics:
    def __init__(self, latency_window: int = 2000) -> None:
        self._lock = threading.Lock()
        self._suggest_latencies_ms: Deque[float] = deque(maxlen=latency_window)
        self.suggest_requests = 0
        self.suggest_cache_hits = 0
        self.suggest_cache_misses = 0
        self.search_requests = 0

    def record_suggest(self, latency_ms: float, cache_hit: bool) -> None:
        with self._lock:
            self._suggest_latencies_ms.append(latency_ms)
            self.suggest_requests += 1
            if cache_hit:
                self.suggest_cache_hits += 1
            else:
                self.suggest_cache_misses += 1

    def record_search(self) -> None:
        with self._lock:
            self.search_requests += 1

    @staticmethod
    def _percentile(sorted_vals, pct: float) -> float:
        if not sorted_vals:
            return 0.0
        if len(sorted_vals) == 1:
            return round(sorted_vals[0], 3)
        # Nearest-rank percentile.
        rank = max(0, min(len(sorted_vals) - 1, int(round(pct / 100.0 * (len(sorted_vals) - 1)))))
        return round(sorted_vals[rank], 3)

    def snapshot(self) -> Dict:
        with self._lock:
            vals = sorted(self._suggest_latencies_ms)
            total = self.suggest_cache_hits + self.suggest_cache_misses
            return {
                "suggest_requests": self.suggest_requests,
                "search_requests": self.search_requests,
                "cache_hits": self.suggest_cache_hits,
                "cache_misses": self.suggest_cache_misses,
                "cache_hit_rate": round(self.suggest_cache_hits / total, 4) if total else 0.0,
                "latency_ms": {
                    "samples": len(vals),
                    "p50": self._percentile(vals, 50),
                    "p95": self._percentile(vals, 95),
                    "p99": self._percentile(vals, 99),
                    "max": round(vals[-1], 3) if vals else 0.0,
                },
            }
