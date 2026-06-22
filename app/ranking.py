"""Ranking: all-time popularity blended with recency for trending searches.

Basic ranking (60% of the assignment) = sort matching queries by all-time
``count``. The Trie already returns candidates in that order.

Enhanced ranking (the +20% trending feature) must let *recently* searched
queries rise above merely historically-popular ones, **without** letting a brief
spike dominate forever. The standard way to get both properties is an
**exponentially time-decayed counter** per query:

    on each search at time t:
        score = score * 0.5 ** ((t - last_t) / half_life) + 1
        last_t = t

    decayed value at read time r:
        score * 0.5 ** ((r - last_t) / half_life)

* It rewards recent activity (each search adds 1).
* It *forgets*: after one half-life the contribution of old searches halves, so a
  query that was hot for a minute decays back to nothing — solving the
  "permanently over-ranked spike" problem the brief explicitly calls out.

Final suggestion score blends the two on comparable scales:

    score = count_weight * log1p(count) + recency_weight * decayed_recent_hits

``log1p(count)`` compresses million-scale counts into ~0–14, putting them in the
same range as the recency term so the weights are meaningful and tunable.

The tracker only holds queries that have been searched *this session* and prunes
entries once they decay below a threshold, so "trending" stays small and cheap
to scan even though the full dataset is 120k+ rows.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Dict, List, Optional, Tuple


class RecencyTracker:
    def __init__(self, half_life_seconds: float, prune_threshold: float = 0.01) -> None:
        self.half_life = max(1e-6, half_life_seconds)
        self._lambda = math.log(2) / self.half_life
        self.prune_threshold = prune_threshold
        # query -> (score_at_last_update, last_update_ts)
        self._scores: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()

    def _decay(self, score: float, last_ts: float, now: float) -> float:
        dt = max(0.0, now - last_ts)
        return score * math.exp(-self._lambda * dt)

    def record(self, query: str, now: Optional[float] = None) -> None:
        """Register one search of ``query`` at time ``now``."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._scores.get(query)
            if cur is None:
                self._scores[query] = (1.0, now)
            else:
                decayed = self._decay(cur[0], cur[1], now)
                self._scores[query] = (decayed + 1.0, now)

    def current(self, query: str, now: Optional[float] = None) -> float:
        """Decayed recency score for one query right now (0 if untracked)."""
        now = time.time() if now is None else now
        with self._lock:
            cur = self._scores.get(query)
            if cur is None:
                return 0.0
            return self._decay(cur[0], cur[1], now)

    def current_many(self, queries: List[str], now: Optional[float] = None) -> Dict[str, float]:
        now = time.time() if now is None else now
        out: Dict[str, float] = {}
        with self._lock:
            for q in queries:
                cur = self._scores.get(q)
                out[q] = self._decay(cur[0], cur[1], now) if cur else 0.0
        return out

    def trending(self, k: int, now: Optional[float] = None) -> List[Tuple[str, float]]:
        """Top ``k`` queries by decayed recency score; prunes faded entries."""
        now = time.time() if now is None else now
        with self._lock:
            decayed: List[Tuple[str, float]] = []
            faded: List[str] = []
            for q, (score, ts) in self._scores.items():
                d = self._decay(score, ts, now)
                if d < self.prune_threshold:
                    faded.append(q)
                else:
                    decayed.append((q, d))
            for q in faded:  # keep the tracker small
                del self._scores[q]
            decayed.sort(key=lambda t: t[1], reverse=True)
            return decayed[:k]

    def tracked_count(self) -> int:
        with self._lock:
            return len(self._scores)


def blended_score(
    count: int,
    recent_hits: float,
    count_weight: float,
    recency_weight: float,
) -> float:
    """Combine all-time popularity and decayed recency into one ranking score."""
    return count_weight * math.log1p(max(0, count)) + recency_weight * recent_hits
