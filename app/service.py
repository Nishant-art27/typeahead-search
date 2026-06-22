"""TypeaheadService — orchestrates store, Trie, cache, ranking and batching.

This is the seam the HTTP layer talks to. It owns the request flows:

* ``suggest``  : cache -> Trie -> (optional recency re-rank), with result caching.
* ``search``   : record recency + enqueue a batched, aggregated count update.
* ``trending`` : top queries by decayed recency score.
* ``flush``    : the batch-writer callback — durably apply counts, refresh the
                 Trie, and invalidate the now-stale cached prefixes.

Consistency model (documented on purpose):
* Recency is updated **synchronously** on every search, so Trending and the
  recency-aware ranking react instantly.
* All-time counts are updated **eventually**, when the batch writer flushes — at
  which point the Trie is refreshed and affected cache prefixes are invalidated.
"""

from __future__ import annotations

import string
import time
from typing import Any, Dict, List, Optional

from .batch_writer import BatchWriter
from .config import Settings
from .distributed_cache import DistributedCache
from .metrics import Metrics
from .ranking import RecencyTracker, blended_score
from .store import PrimaryStore
from .trie import SuggestionTrie


def normalize(text: str) -> str:
    """Lower-case and collapse whitespace so input is matched case-insensitively."""
    return " ".join((text or "").lower().split())


class TypeaheadService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = PrimaryStore(settings.db_path)
        self.trie = SuggestionTrie(node_cap=settings.trie_node_cap)
        self.cache = DistributedCache(
            node_names=[n.strip() for n in settings.cache_node_names if n.strip()],
            vnodes=settings.cache_vnodes,
            capacity_per_node=settings.cache_capacity_per_node,
        )
        self.recency = RecencyTracker(
            half_life_seconds=settings.recency_half_life_seconds,
            prune_threshold=settings.recency_prune_threshold,
        )
        self.metrics = Metrics(latency_window=settings.latency_window)
        self.batch_writer = BatchWriter(
            flush_callback=self._on_flush,
            interval_seconds=settings.batch_flush_interval_seconds,
            max_size=settings.batch_max_size,
        )
        self._started_at = time.time()

    # --- lifecycle ---------------------------------------------------------
    def build_index_from_store(self) -> None:
        """Load every (query, count) from the durable store into the Trie."""
        self.trie.bulk_insert(self.store.iter_all())

    def start(self) -> None:
        self.batch_writer.start()

    def shutdown(self) -> None:
        self.batch_writer.stop()  # final flush
        self.store.close()

    # --- cache keys --------------------------------------------------------
    @staticmethod
    def _cache_key(prefix: str, recency: bool) -> str:
        # Mode is part of the key so popularity and recency results never collide.
        return ("r1:" if recency else "r0:") + prefix

    # --- suggest -----------------------------------------------------------
    def suggest(self, raw_prefix: str, recency: bool) -> Dict[str, Any]:
        start = time.perf_counter()
        prefix = normalize(raw_prefix)

        # Graceful handling of empty / missing input.
        if not prefix:
            self.metrics.record_suggest((time.perf_counter() - start) * 1000.0, True)
            return {"prefix": prefix, "source": "empty", "suggestions": []}

        key = self._cache_key(prefix, recency)
        cached = self.cache.get(key)
        if cached is not None:
            self.metrics.record_suggest((time.perf_counter() - start) * 1000.0, True)
            return {"prefix": prefix, "source": "cache", "suggestions": cached}

        # Cache miss -> fall back to the in-memory index.
        limit = self.settings.suggest_limit
        if recency:
            suggestions = self._rank_with_recency(prefix, limit)
            ttl = self.settings.cache_ttl_recency_seconds
        else:
            suggestions = [
                {"query": q, "count": c}
                for q, c in self.trie.top_for_prefix(prefix, limit)
            ]
            ttl = self.settings.cache_ttl_seconds

        self.cache.set(key, suggestions, ttl)
        self.metrics.record_suggest((time.perf_counter() - start) * 1000.0, False)
        return {"prefix": prefix, "source": "index", "suggestions": suggestions}

    def _rank_with_recency(self, prefix: str, limit: int) -> List[Dict[str, Any]]:
        # Candidate generation by all-time popularity, then re-rank by a blend of
        # popularity + decayed recency. Pool size = trie_node_cap (>= limit).
        candidates = self.trie.candidates_for_prefix(prefix)
        if not candidates:
            return []
        now = time.time()
        recent = self.recency.current_many([q for q, _ in candidates], now=now)
        scored = []
        for q, c in candidates:
            r = recent.get(q, 0.0)
            score = blended_score(
                c, r,
                self.settings.rank_count_weight,
                self.settings.rank_recency_weight,
            )
            scored.append((score, q, c, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        out: List[Dict[str, Any]] = []
        for score, q, c, r in scored[:limit]:
            out.append({
                "query": q,
                "count": c,
                "recent_score": round(r, 3),
                "score": round(score, 3),
                "trending": r > 0.0,
            })
        return out

    # --- search ------------------------------------------------------------
    def search(self, raw_query: str) -> Dict[str, Any]:
        query = normalize(raw_query)
        if not query:
            return {"message": "Searched", "query": "", "recorded": False}
        # Recency updates immediately; the durable count update is batched.
        self.recency.record(query)
        self.batch_writer.enqueue(query)
        self.metrics.record_search()
        return {"message": "Searched", "query": query, "recorded": True}

    # --- trending ----------------------------------------------------------
    def trending(self, k: Optional[int] = None) -> List[Dict[str, Any]]:
        k = k or self.settings.trending_limit
        out: List[Dict[str, Any]] = []
        for q, score in self.recency.trending(k):
            # Best-effort all-time count from the Trie pool (may lag until flush).
            count = self._lookup_count(q)
            out.append({
                "query": q,
                "recent_score": round(score, 3),
                "count": count,
            })
        return out

    def _lookup_count(self, query: str) -> int:
        top = self.trie.top_for_prefix(query, 1)
        if top and top[0][0] == query:
            return top[0][1]
        c = self.store.get(query)
        return c if c is not None else 0

    # --- batch flush (writer callback) ------------------------------------
    def _on_flush(self, deltas: Dict[str, int]) -> None:
        # 1) Durably apply aggregated increments; get back authoritative counts.
        new_counts = self.store.apply_batch(deltas)
        # 2) Refresh the in-memory index so suggestions reflect the new counts.
        for query, count in new_counts.items():
            self.trie.insert(query, count)
        # 3) Invalidate every cached prefix of each changed query (both modes),
        #    so the next read recomputes against fresh counts.
        keys: List[str] = []
        for query in new_counts:
            for i in range(1, len(query) + 1):
                p = query[:i]
                keys.append(self._cache_key(p, False))
                keys.append(self._cache_key(p, True))
        self.cache.invalidate_keys(keys)

    # --- cache debug -------------------------------------------------------
    def cache_debug(self, raw_prefix: str, recency: bool = False) -> Dict[str, Any]:
        prefix = normalize(raw_prefix)
        key = self._cache_key(prefix, recency)
        info = self.cache.debug(key)
        info["normalized_prefix"] = prefix
        info["mode"] = "recency" if recency else "popularity"
        return info

    # --- stats -------------------------------------------------------------
    def _distribution_sample(self) -> List[str]:
        # Two-letter prefixes — a representative key sample for showing ring balance.
        letters = string.ascii_lowercase
        return [self._cache_key(a + b, False) for a in letters for b in letters]

    def stats(self) -> Dict[str, Any]:
        return {
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "index": {
                "queries_indexed": self.trie.size,
                "trie_node_cap": self.settings.trie_node_cap,
            },
            "requests_and_latency": self.metrics.snapshot(),
            "cache": self.cache.stats(),
            "cache_key_distribution": self.cache.distribution(self._distribution_sample()),
            "primary_store": self.store.stats(),
            "batch_writer": self.batch_writer.stats(),
            "recency": {
                "tracked_queries": self.recency.tracked_count(),
                "half_life_seconds": self.settings.recency_half_life_seconds,
            },
            "config": {
                "suggest_limit": self.settings.suggest_limit,
                "cache_nodes": self.cache.ring.nodes,
                "cache_vnodes_per_node": self.settings.cache_vnodes,
                "cache_ttl_seconds": self.settings.cache_ttl_seconds,
                "cache_ttl_recency_seconds": self.settings.cache_ttl_recency_seconds,
                "batch_flush_interval_seconds": self.settings.batch_flush_interval_seconds,
                "batch_max_size": self.settings.batch_max_size,
                "rank_count_weight": self.settings.rank_count_weight,
                "rank_recency_weight": self.settings.rank_recency_weight,
            },
        }
