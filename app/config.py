"""Central configuration.

Every tunable lives here and can be overridden with an environment variable so
the system can be reconfigured for a demo without touching code (e.g.
``CACHE_TTL_SECONDS=5 RECENCY_HALF_LIFE_SECONDS=30 ./run.sh``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw not in (None, "") else default


# Project paths -------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")


@dataclass
class Settings:
    # --- HTTP server -------------------------------------------------------
    host: str = _env_str("HOST", "127.0.0.1")
    port: int = _env_int("PORT", 8000)

    # --- Data / storage ----------------------------------------------------
    dataset_path: str = _env_str("DATASET_PATH", os.path.join(DATA_DIR, "queries.csv"))
    db_path: str = _env_str("DB_PATH", os.path.join(DATA_DIR, "typeahead.db"))
    # Minimum dataset size auto-generated on first run if no CSV is present.
    dataset_min_rows: int = _env_int("DATASET_MIN_ROWS", 120_000)
    initial_count: int = _env_int("INITIAL_COUNT", 1)  # for brand-new queries

    # --- Suggestions / Trie ------------------------------------------------
    suggest_limit: int = _env_int("SUGGEST_LIMIT", 10)
    # How many candidates each Trie node remembers. Re-ranking by recency picks
    # the final 10 from this pool, so it must be >= suggest_limit.
    trie_node_cap: int = _env_int("TRIE_NODE_CAP", 25)

    # --- Distributed cache + consistent hashing ----------------------------
    cache_node_names: List[str] = field(
        default_factory=lambda: _env_str(
            "CACHE_NODES", "cache-a,cache-b,cache-c,cache-d"
        ).split(",")
    )
    cache_vnodes: int = _env_int("CACHE_VNODES", 200)  # virtual nodes per real node
    cache_capacity_per_node: int = _env_int("CACHE_CAPACITY_PER_NODE", 5_000)
    cache_ttl_seconds: float = _env_float("CACHE_TTL_SECONDS", 60.0)
    # Recency-mode results go stale faster, so they get a shorter TTL.
    cache_ttl_recency_seconds: float = _env_float("CACHE_TTL_RECENCY_SECONDS", 15.0)

    # --- Recency-aware ranking / trending ----------------------------------
    recency_half_life_seconds: float = _env_float("RECENCY_HALF_LIFE_SECONDS", 120.0)
    # Final score = count_weight*log1p(count) + recency_weight*decayed_recent_hits
    rank_count_weight: float = _env_float("RANK_COUNT_WEIGHT", 1.0)
    rank_recency_weight: float = _env_float("RANK_RECENCY_WEIGHT", 2.5)
    trending_limit: int = _env_int("TRENDING_LIMIT", 10)
    # Drop a query from the recency tracker once its decayed score falls below this.
    recency_prune_threshold: float = _env_float("RECENCY_PRUNE_THRESHOLD", 0.01)

    # --- Batch writes ------------------------------------------------------
    batch_flush_interval_seconds: float = _env_float("BATCH_FLUSH_INTERVAL_SECONDS", 2.0)
    batch_max_size: int = _env_int("BATCH_MAX_SIZE", 500)  # flush early if buffer hits this

    # --- Metrics -----------------------------------------------------------
    latency_window: int = _env_int("LATENCY_WINDOW", 2000)  # samples kept for p95


settings = Settings()
