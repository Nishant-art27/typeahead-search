# Architecture & Design

This document explains *why* the system is built the way it is — the design
choices, their trade-offs, and the failure modes. (The assignment requires being
able to defend every one of these in a viva, so each section ends with the
short answer to "why this and not the obvious alternative?")

## 1. The big picture

Three layers serve a suggestion, fastest first:

```
request ─▶ Distributed Cache ──miss──▶ Trie index ──(counts from)──▶ SQLite store
              (result cache)            (candidate gen)               (source of truth)
```

- **SQLite** is the durable source of truth for `query → count`. It is written
  to **only in batches**.
- The **Trie** is an in-memory index *derived* from SQLite at startup. It turns
  "suggestions for prefix p" into an O(len(p)) lookup.
- The **distributed cache** stores finished suggestion lists so repeated
  keystrokes never touch the Trie at all.

Two background concerns sit alongside:

- the **recency tracker** (in-memory, time-decayed counters) powers trending and
  recency-aware ranking;
- the **batch writer** drains buffered searches into SQLite and then refreshes
  the Trie and invalidates affected cache entries.

> **Why three layers?** Each solves a different cost. The Trie removes the O(N)
> scan/sort of 120k rows. The cache removes even the O(K) Trie work for hot
> prefixes (the long-tail of keystrokes is extremely repetitive). SQLite gives
> durability without making reads pay for it.

## 2. Data storage — SQLite

Schema is intentionally minimal:

```sql
queries(query TEXT PRIMARY KEY, count INTEGER, last_searched REAL)
```

- **Why SQLite?** The brief demands "easy to run locally" with no external
  services. SQLite is a real transactional, on-disk store with zero setup. WAL
  mode keeps reads non-blocking while the batch writer commits.
- **Why is it not on the read path?** Suggestions must be sub-millisecond; a SQL
  query + sort per keystroke is the wrong tool. SQLite is the *write* path and
  the cold-start *seed* for the in-memory index, not the serving path.
- **Trade-off:** the in-memory index can be briefly ahead of or behind a "true"
  count, which is fine for ranking (counts are statistical, not financial). The
  authoritative value always lives in SQLite.

## 3. The suggestion index — Trie with cached top-K

A plain Trie answers "which queries start with p?" but you would still sort
potentially tens of thousands of matches on every keystroke (prefix `"a"`).
Instead, **every Trie node caches the top-K completions in its subtree**, ordered
by count. A lookup walks to the prefix node and returns its list directly.

- **Why does cached top-K stay correct?** Counts are **monotonically
  increasing** — a search can only add to a count. So maintenance is simple: when
  query `q`'s count rises, walk `q`'s path and update its entry in each node's
  top-K. A query already in a node's list just gets bumped; one that wasn't can
  only newly *enter* the list (when it passes the current minimum). Nothing in
  the list ever needs to be demoted because of someone else shrinking.
- **K = `trie_node_cap` (default 25), larger than the 10 we return.** The extra
  candidates are the pool the recency re-ranker reorders (§5).
- **Complexity:** lookup is `O(len(prefix))`; an update is
  `O(len(query) · K)`. Build is `O(total characters · K)` once at startup
  (~120k queries in a couple of seconds).

> **Why not maintain exact top-K with arbitrary score changes?** Because then a
> node's list could need to demote an entry when another entry's score drops —
> which forces a subtree rescan. Monotonic counts buy us cheap, correct
> incremental maintenance. Time-decayed *recency* scores are **not** monotonic,
> which is exactly why recency is handled by re-ranking a candidate pool rather
> than by storing decayed scores in the Trie (see the trade-off in §5).

**Alternatives considered:** a sorted array + binary search for the prefix range
then a top-K heap (simpler, but short prefixes still scan a huge range on a cold
cache); precomputed completion tables per prefix (huge memory, stale on writes).
The cached-top-K Trie is the best fit for "fast reads + cheap monotonic updates".

## 4. Caching + consistent hashing

The cache stores **finished suggestion lists** keyed by `mode:prefix` (e.g.
`r1:iph`). It is *distributed* across several independent **logical cache nodes**,
and a **consistent hash ring** decides which node owns each prefix key — exactly
how a Redis/Memcached cluster shards keys, but in-process so the demo needs no
setup. Each node is an isolated LRU + per-entry TTL store with its own counters.

### Why consistent hashing (not `hash(key) % N`)?
Modulo hashing remaps almost every key when `N` changes (scaling the cache up or
down, or a node failing). Consistent hashing places nodes and keys on a fixed
128-bit ring (MD5); a key is owned by the first node clockwise. Adding/removing a
node only remaps the keys in that node's arc — about `1/N` of them. The unit
tests assert this remap-stability property directly.

### Why virtual nodes?
With one ring point per node, 4 nodes carve the ring into 4 arbitrary arcs — wildly
uneven load. Each physical node is instead hashed to `vnodes` (default 200)
points, so arcs interleave and load evens out. Measured distribution over 676
two-letter prefix keys: ~23–29% per node across 4 nodes.

### Why MD5, not Python's `hash()`?
`hash()` is salted per process, so it is neither stable across restarts nor
reproducible — useless for a routing scheme you want to reason about and debug.
`GET /cache/debug?prefix=` exposes the chosen node, the key's ring position, and
hit/miss so the routing is observable.

### Freshness: TTL + invalidation
- **TTL** — every entry expires (popularity 60 s, recency 15 s by default), so
  stale rankings cannot live forever.
- **Targeted invalidation** — when the batch writer changes a query's count,
  every cached prefix of that query (both modes) is evicted, so the next read
  recomputes against fresh counts. This is what keeps recency-mode results fresh
  within one flush interval rather than waiting out the TTL.
- **LRU capacity** — each node caps entries and evicts least-recently-used, so
  memory is bounded regardless of how many distinct prefixes are probed.

> **Trade-off:** caching trending (time-dependent) results means a result can be
> up to one TTL / one flush-interval stale. We accept a few seconds of staleness
> in exchange for serving the hot path from memory. Shorten `CACHE_TTL_RECENCY_SECONDS`
> to trade latency for freshness.

## 5. Trending — recency-aware ranking

**Basic ranking (the 60% requirement):** sort prefix matches by all-time
`count`. The Trie already returns candidates in that order, so this is free.

**Enhanced ranking (the +20% requirement):** recently searched queries should
outrank merely historically-popular ones, *without* a brief spike dominating
forever. We give each query an **exponentially time-decayed hit counter**:

```
on search at time t:   score = score · 0.5^((t − last_t)/half_life) + 1 ;  last_t = t
read at time r:        value = score · 0.5^((r − last_t)/half_life)
```

The final suggestion score blends popularity and recency on comparable scales:

```
score = count_weight · log1p(count)  +  recency_weight · decayed_recent_hits
```

`log1p(count)` compresses million-scale counts into ~0–14 so the two terms are
comparable and the weights are meaningful.

This directly answers the brief's five required explanations:

1. **How recent searches are tracked** — an in-memory decayed counter per query,
   updated synchronously on every `POST /search`.
2. **How recency affects ranking** — it is added to the blended score; with the
   default weight a burst can lift a mid-ranked query above the all-time leaders.
3. **How short-lived spikes don't dominate forever** — the decay *forgets*: after
   one half-life an old burst's contribution halves, and after a few half-lives
   it is ~0, so ranking relaxes back to all-time popularity. The tracker even
   prunes queries once they decay below a threshold.
4. **How the cache is kept consistent when rankings change** — recency-mode
   entries use a short TTL **and** are invalidated per-prefix on each batch
   flush, so a re-rank shows up within a flush interval.
5. **The freshness / latency / complexity trade-off** — see below.

> **Documented trade-off (candidate generation vs. recency):** prefix suggestions
> re-rank the Trie's top-`K` *by count*. A cold, low-count query that suddenly
> spikes may not be in that pool for a short prefix, so it won't appear deep in
> prefix suggestions until its all-time count grows — but it **does** surface
> immediately in the global **Trending** board (which ranks purely by recency).
> This keeps suggestion latency O(K) instead of scanning every recently-active
> query under a prefix. Raising `TRIE_NODE_CAP` widens the pool at some CPU cost.

## 6. Batch writes

Writing to SQLite on every `POST /search` means one disk transaction per
search — the write path becomes the bottleneck. Instead searches are **buffered
in memory and aggregated** (`query → delta`), and a background thread flushes the
whole batch in one transaction. The flush fires on whichever trigger comes first:

- **time** — every `batch_flush_interval_seconds` (default 2 s), or
- **size** — when the buffer reaches `batch_max_size` distinct queries (default 500).

Because repeated searches for the same query coalesce, *N* search requests become
far fewer than *N* DB writes. The benchmark shows ~6× reduction on a moderately
skewed stream; a production-Zipfian stream coalesces much more.

On flush the service: (1) applies the aggregated increments to SQLite in one
transaction, (2) refreshes the Trie with the new authoritative counts, and (3)
invalidates the affected cache prefixes.

> **Failure trade-off (explicitly required):** the buffer is in memory. A crash
> between flushes loses at most one window of increments (≤ `interval` seconds or
> ≤ `max_size` queries). That is the deliberate price for not doing a synchronous
> durable write per request, and it is acceptable for a *suggestion ranking*
> (approximate counts for a few seconds don't hurt). A system needing durability
> would front the buffer with an append-only log / Kafka and replay it on
> restart — at the cost of added latency and operational complexity. `stop()`
> performs a final flush, so a **graceful** shutdown loses nothing.

## 7. Concurrency model

FastAPI runs the synchronous endpoints in a thread pool, and the batch writer is
its own daemon thread, so shared state is accessed concurrently. Each component
owns its locking: the Trie has one `RLock`; each cache node its own `Lock`; the
store a `Lock` over a `check_same_thread=False` connection; the recency tracker
and metrics their own locks. Critical sections are tiny (a read copies out ≤ K
items). There is no global lock, so reads don't block each other — the cost is
*eventual* consistency during a flush (some queries updated before others), which
is acceptable here.

## 8. What a production version would add

This is a single-process teaching implementation. The same design scales by
swapping implementations behind the same seams:

- cache nodes → real Redis/Memcached shards (the ring code is unchanged);
- the Trie → a sharded suggestion service or an FST/compressed index;
- the in-memory buffer → Kafka + a stream consumer for durable, replayable
  batching;
- SQLite → Postgres/Cassandra for the authoritative counts;
- add per-prefix request coalescing and a CDN edge cache for the hottest prefixes.
