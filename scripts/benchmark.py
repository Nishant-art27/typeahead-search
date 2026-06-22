#!/usr/bin/env python3
"""Performance benchmark for the running typeahead server.

Measures the two things the assignment's performance report asks for:

1.  **Suggestion latency** (p50/p95/p99) and cache hit rate under a realistic
    keystroke workload — many requests concentrated on a small set of hot
    prefixes (which is what makes a cache worthwhile), plus a cold tail.
2.  **Write reduction** from batching — fire many searches and compare search
    requests received against rows actually written to the DB.

Usage (server must already be running):
    python scripts/benchmark.py --base http://127.0.0.1:8000 --suggests 5000 --searches 3000
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request
from typing import List


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path) as r:
        return json.load(r)


def _post(base: str, path: str, body: dict):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def _percentile(vals: List[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    i = max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))
    return s[i]


HOT_PREFIXES = ["ip", "iph", "ipho", "sam", "gal", "pyt", "pyth", "rea", "best",
                "lap", "doc", "kub", "java", "nik", "sus", "piz", "new", "lon"]
COLD_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def bench_suggest(base: str, n: int, recency: bool) -> None:
    rng = random.Random(7)
    latencies: List[float] = []
    for _ in range(n):
        # 80% hot prefixes (cache-friendly), 20% random cold prefixes.
        if rng.random() < 0.8:
            q = rng.choice(HOT_PREFIXES)
        else:
            q = "".join(rng.choice(COLD_LETTERS) for _ in range(rng.randint(2, 4)))
        t0 = time.perf_counter()
        _get(base, f"/suggest?q={q}&recency={'true' if recency else 'false'}")
        latencies.append((time.perf_counter() - t0) * 1000.0)

    print(f"\n[suggest] {n} requests  (recency={recency})")
    print(f"  client-observed latency: p50={_percentile(latencies,50):.3f}ms  "
          f"p95={_percentile(latencies,95):.3f}ms  p99={_percentile(latencies,99):.3f}ms  "
          f"max={max(latencies):.3f}ms")
    snap = _get(base, "/stats")["requests_and_latency"]
    print(f"  server cache hit rate: {snap['cache_hit_rate']*100:.1f}%   "
          f"server p95: {snap['latency_ms']['p95']}ms")


def bench_writes(base: str, n: int) -> None:
    rng = random.Random(11)
    # A skewed search stream: a few queries searched a lot (coalesce well).
    pool = HOT_PREFIXES + ["iphone 15", "python tutorial", "best laptop",
                           "sushi near me", "react vs angular"]
    for _ in range(n):
        q = rng.choice(pool) if rng.random() < 0.7 else \
            "query " + str(rng.randint(0, 400))
        _post(base, "/search", {"query": q})
    # Allow the time-based flush to drain the buffer.
    time.sleep(2.5)
    bw = _get(base, "/stats")["batch_writer"]
    print(f"\n[writes] {n} searches submitted")
    print(f"  search events received: {bw['search_events_received']}")
    print(f"  DB rows written:        {bw['db_rows_written']}")
    print(f"  writes saved:           {bw['writes_saved']}")
    print(f"  write reduction:        {bw['write_reduction_ratio']}x")
    print(f"  flushes:                {bw['flush_count']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--suggests", type=int, default=5000)
    ap.add_argument("--searches", type=int, default=3000)
    args = ap.parse_args()

    print("Search Typeahead — benchmark")
    print("=" * 40)
    bench_suggest(args.base, args.suggests, recency=False)
    bench_suggest(args.base, args.suggests, recency=True)
    bench_writes(args.base, args.searches)


if __name__ == "__main__":
    main()
