"""Consistent hashing ring used to route a prefix key to a cache node.

A plain ``hash(key) % N`` would remap almost every key whenever the number of
cache nodes changes. Consistent hashing places both nodes and keys on a fixed
ring (here, the 128-bit MD5 space); a key is owned by the first node found
clockwise. Adding or removing a node only remaps the keys in that node's arc —
roughly ``1/N`` of them — which is exactly the property we want from a
distributed cache that may scale up or down.

**Virtual nodes**: each physical node is placed at ``vnodes`` points around the
ring (``"cache-a#0"``, ``"cache-a#1"``, …). Without them, a 4-node ring with one
hash each gives wildly uneven arc sizes; with ~200 points per node the load
spreads evenly. ``debug()`` exposes the chosen node, the key's ring position,
and the actual key-distribution so the consistent-hashing behaviour is visible.

We use MD5 (not Python's built-in ``hash()``) on purpose: ``hash()`` is salted
per process, so it would not be stable or reproducible across restarts — useless
for a routing scheme you want to reason about.
"""

from __future__ import annotations

import bisect
import hashlib
from typing import Dict, List, Optional, Tuple


def _hash(key: str) -> int:
    return int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)


HASH_SPACE = 1 << 128  # MD5 output size, for reporting ring positions as %


class ConsistentHashRing:
    def __init__(self, nodes: Optional[List[str]] = None, vnodes: int = 200) -> None:
        self.vnodes = vnodes
        self._ring: Dict[int, str] = {}      # ring point -> node name
        self._sorted_points: List[int] = []  # sorted ring points for bisect
        self._nodes: List[str] = []
        for n in nodes or []:
            self.add_node(n)

    def _vnode_key(self, node: str, i: int) -> str:
        return f"{node}#{i}"

    def add_node(self, node: str) -> None:
        if node in self._nodes:
            return
        self._nodes.append(node)
        for i in range(self.vnodes):
            point = _hash(self._vnode_key(node, i))
            self._ring[point] = node
        self._sorted_points = sorted(self._ring.keys())

    def remove_node(self, node: str) -> None:
        if node not in self._nodes:
            return
        self._nodes.remove(node)
        for i in range(self.vnodes):
            self._ring.pop(_hash(self._vnode_key(node, i)), None)
        self._sorted_points = sorted(self._ring.keys())

    def get_node(self, key: str) -> Optional[str]:
        """Return the node that owns ``key`` (first node clockwise)."""
        if not self._sorted_points:
            return None
        h = _hash(key)
        idx = bisect.bisect(self._sorted_points, h)
        if idx == len(self._sorted_points):
            idx = 0  # wrap around the ring
        return self._ring[self._sorted_points[idx]]

    @property
    def nodes(self) -> List[str]:
        return list(self._nodes)

    def locate(self, key: str) -> Tuple[Optional[str], int, int]:
        """Return (owning_node, key_hash, owning_vnode_point) for debugging."""
        if not self._sorted_points:
            return None, _hash(key), 0
        h = _hash(key)
        idx = bisect.bisect(self._sorted_points, h)
        if idx == len(self._sorted_points):
            idx = 0
        point = self._sorted_points[idx]
        return self._ring[point], h, point

    def key_distribution(self, sample_keys: List[str]) -> Dict[str, int]:
        """Count how many of ``sample_keys`` land on each node (load balance)."""
        dist: Dict[str, int] = {n: 0 for n in self._nodes}
        for k in sample_keys:
            node = self.get_node(k)
            if node is not None:
                dist[node] = dist.get(node, 0) + 1
        return dist
