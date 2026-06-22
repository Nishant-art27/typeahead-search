# API Reference

Base URL (local): `http://127.0.0.1:8000`. All responses are JSON.

---

## `GET /suggest`

Return up to 10 suggestions whose query starts with the given prefix.

**Query params**

| Param | Type | Default | Description |
| --- | --- | --- | --- |
| `q` | string | `""` | The prefix typed by the user. Matched case-insensitively; whitespace is collapsed. |
| `recency` | bool | `true` | `true` = recency-aware (trending) ranking; `false` = all-time popularity. |

**Behaviour**
- At most 10 results, each starting with `q`, sorted by score descending.
- Empty / whitespace `q` → `{"source":"empty","suggestions":[]}`.
- No matches → `suggestions: []`.
- `source` indicates where the result came from: `cache`, `index`, or `empty`.

**Example**

```bash
curl 'http://127.0.0.1:8000/suggest?q=iph&recency=false'
```

```json
{
  "prefix": "iph",
  "source": "index",
  "suggestions": [
    { "query": "iphone 11", "count": 62736 },
    { "query": "iphone 11 price", "count": 47197 }
  ]
}
```

In `recency=true` mode each item additionally carries `recent_score`, the blended
`score`, and `trending` (boolean):

```json
{ "query": "iphone 11 tutorial", "count": 26937, "recent_score": 40.569, "score": 111.7, "trending": true }
```

---

## `POST /search`

The dummy search API. Returns `"Searched"` and records the query (the count
update is batched and applied asynchronously).

**Body**

```json
{ "query": "iphone 11 tutorial" }
```

**Response**

```json
{ "message": "Searched", "query": "iphone 11 tutorial", "recorded": true }
```

- The query is normalized (lower-cased, whitespace-collapsed) before recording.
- Empty queries return `"recorded": false` and are not recorded.

```bash
curl -X POST http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"iphone 11 tutorial"}'
```

---

## `GET /cache/debug`

Show how a prefix key routes through the consistent-hash ring and its current
cache state — the requirement's "which cache node is responsible, and hit/miss".

**Query params**

| Param | Type | Default | Description |
| --- | --- | --- | --- |
| `prefix` | string | `""` | Prefix key to inspect. |
| `recency` | bool | `false` | Inspect the recency-mode key (`r1:`) vs popularity (`r0:`). |

**Example**

```bash
curl 'http://127.0.0.1:8000/cache/debug?prefix=iph'
```

```json
{
  "key": "r0:iph",
  "normalized_prefix": "iph",
  "mode": "popularity",
  "owner_node": "cache-b",
  "status": "hit",
  "ring_position_pct": 63.2673,
  "owning_vnode_position_pct": 64.10,
  "key_hash": "...",
  "nodes": ["cache-a", "cache-b", "cache-c", "cache-d"],
  "vnodes_per_node": 200
}
```

`status` is one of `hit`, `miss`, or `expired` (probed without mutating the cache).

---

## `GET /trending`

Top queries by time-decayed recency score.

**Query params**

| Param | Type | Default | Description |
| --- | --- | --- | --- |
| `k` | int | `10` | How many trending queries to return. |

```bash
curl 'http://127.0.0.1:8000/trending?k=5'
```

```json
{
  "trending": [
    { "query": "iphone 11 tutorial", "recent_score": 40.569, "count": 26937 },
    { "query": "python tutorial", "recent_score": 1.979, "count": 3989 }
  ]
}
```

---

## `GET /stats`

Everything the performance report needs: latency percentiles, cache hit rate,
DB write counts, write-reduction ratio, ring balance, and current config.

```bash
curl http://127.0.0.1:8000/stats
```

Abridged shape:

```json
{
  "uptime_seconds": 312.4,
  "index": { "queries_indexed": 120160, "trie_node_cap": 25 },
  "requests_and_latency": {
    "suggest_requests": 10000, "cache_hits": 8110, "cache_misses": 1890,
    "cache_hit_rate": 0.811,
    "latency_ms": { "samples": 2000, "p50": 0.005, "p95": 0.006, "p99": 0.009, "max": 6.35 }
  },
  "cache": {
    "nodes": [ { "name": "cache-a", "size": 41, "hits": 900, "misses": 120, "hit_rate": 0.88 } ],
    "global_hit_rate": 0.81, "total_entries": 160
  },
  "cache_key_distribution": { "cache-a": 164, "cache-b": 162, "cache-c": 156, "cache-d": 194 },
  "primary_store": { "rows_written": 495, "write_batches": 3, "reads": 2 },
  "batch_writer": {
    "search_events_received": 3045, "db_rows_written": 495,
    "writes_saved": 2550, "write_reduction_ratio": 6.15, "flush_count": 3,
    "current_buffer_size": 0
  },
  "recency": { "tracked_queries": 7, "half_life_seconds": 120.0 },
  "config": { "...": "..." }
}
```

---

## `GET /health`

```json
{ "status": "ok" }
```
