"""Distributed suggestion cache.

The cache stores *finished* suggestion lists keyed by prefix, so a repeated
keystroke ("ip", "iph", "ipho", …) is answered without touching the Trie or
re-ranking. It is "distributed" in the sense the assignment asks for: results
are spread across several **independent logical cache nodes**, and a
``ConsistentHashRing`` decides which node owns each prefix key. Each node is an
isolated LRU+TTL store with its own counters — exactly how a real Redis/Memcached
cluster shard would behave, but in-process so the demo runs with zero setup.

Layering for a suggestion request:  distributed cache  ->  Trie index  ->  (DB).
The cache is checked first; only on a miss do we fall back to recomputing from
the in-memory index.

Three correctness features the assignment calls out:

* **Expiry (TTL)** — every entry has an expiry; stale rankings cannot live
  forever. Recency-mode entries get a shorter TTL since they age faster.
* **Invalidation** — when the batch writer changes a query's count, every cached
  prefix of that query is evicted so the next read recomputes fresh.
* **Even distribution** — virtual nodes in the ring keep keys spread across nodes.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from .consistent_hash import HASH_SPACE, ConsistentHashRing


class CacheNode:
    """A single logical cache shard: LRU eviction + per-entry TTL."""

    def __init__(self, name: str, capacity: int) -> None:
        self.name = name
        self.capacity = capacity
        self._store: "OrderedDict[str, tuple]" = OrderedDict()  # key -> (value, expiry)
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.expirations = 0

    def get(self, key: str, now: float) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            value, expiry = entry
            if expiry <= now:
                del self._store[key]
                self.expirations += 1
                self.misses += 1
                return None
            self._store.move_to_end(key)  # mark as most-recently-used
            self.hits += 1
            return value

    def set(self, key: str, value: Any, expiry: float) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, expiry)
            while len(self._store) > self.capacity:
                self._store.popitem(last=False)  # evict least-recently-used
                self.evictions += 1

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._store.pop(key, None) is not None

    def peek_status(self, key: str, now: float) -> str:
        """Non-mutating probe used by /cache/debug: 'hit' | 'miss' | 'expired'."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return "miss"
            return "hit" if entry[1] > now else "expired"

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "name": self.name,
            "size": self.size(),
            "capacity": self.capacity,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "expirations": self.expirations,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }


class DistributedCache:
    def __init__(self, node_names: List[str], vnodes: int, capacity_per_node: int) -> None:
        self.ring = ConsistentHashRing(node_names, vnodes=vnodes)
        self.nodes: Dict[str, CacheNode] = {
            name: CacheNode(name, capacity_per_node) for name in node_names
        }

    def _node_for(self, key: str) -> CacheNode:
        name = self.ring.get_node(key)
        return self.nodes[name]

    def get(self, key: str) -> Optional[Any]:
        return self._node_for(key).get(key, time.time())

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._node_for(key).set(key, value, time.time() + ttl)

    def invalidate(self, key: str) -> bool:
        return self._node_for(key).delete(key)

    def invalidate_keys(self, keys: List[str]) -> int:
        removed = 0
        for k in keys:
            if self._node_for(k).delete(k):
                removed += 1
        return removed

    # --- debug / observability --------------------------------------------
    def debug(self, key: str) -> Dict[str, Any]:
        """Explain how ``key`` routes through the ring and its current state."""
        now = time.time()
        node_name, key_hash, vnode_point = self.ring.locate(key)
        status = self.nodes[node_name].peek_status(key, now) if node_name else "no-nodes"
        return {
            "key": key,
            "owner_node": node_name,
            "status": status,  # hit | miss | expired
            "ring_position_pct": round(100.0 * key_hash / HASH_SPACE, 4),
            "owning_vnode_position_pct": round(100.0 * vnode_point / HASH_SPACE, 4),
            "key_hash": str(key_hash),
            "nodes": self.ring.nodes,
            "vnodes_per_node": self.ring.vnodes,
        }

    def stats(self) -> Dict[str, Any]:
        node_stats = [n.stats() for n in self.nodes.values()]
        hits = sum(s["hits"] for s in node_stats)
        misses = sum(s["misses"] for s in node_stats)
        total = hits + misses
        return {
            "nodes": node_stats,
            "total_hits": hits,
            "total_misses": misses,
            "global_hit_rate": round(hits / total, 4) if total else 0.0,
            "total_entries": sum(s["size"] for s in node_stats),
        }

    def distribution(self, sample_keys: List[str]) -> Dict[str, int]:
        return self.ring.key_distribution(sample_keys)
