"""Unit tests for the core data structures.

Run from the project root:  python -m unittest discover -s tests -v
(No third-party test deps — uses the standard library ``unittest``.)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.batch_writer import BatchWriter
from app.consistent_hash import ConsistentHashRing
from app.distributed_cache import DistributedCache
from app.ranking import RecencyTracker, blended_score
from app.trie import SuggestionTrie


class TestConsistentHashRing(unittest.TestCase):
    def test_balanced_distribution(self):
        ring = ConsistentHashRing(["a", "b", "c", "d"], vnodes=200)
        keys = [f"prefix-{i}" for i in range(8000)]
        dist = ring.key_distribution(keys)
        share = [v / len(keys) for v in dist.values()]
        # With 200 vnodes each node should be within ~7pp of the ideal 25%.
        for s in share:
            self.assertGreater(s, 0.18)
            self.assertLess(s, 0.32)

    def test_remap_stability_on_node_removal(self):
        nodes = ["a", "b", "c", "d"]
        ring = ConsistentHashRing(nodes, vnodes=200)
        keys = [f"k{i}" for i in range(5000)]
        before = {k: ring.get_node(k) for k in keys}
        ring.remove_node("c")
        after = {k: ring.get_node(k) for k in keys}
        moved = sum(1 for k in keys if before[k] != after[k])
        # Removing 1 of 4 nodes should remap roughly 1/4 of keys, not most of them.
        self.assertLess(moved / len(keys), 0.40)
        # Keys that did NOT live on 'c' must not move.
        for k in keys:
            if before[k] != "c":
                self.assertEqual(before[k], after[k])

    def test_deterministic_across_instances(self):
        r1 = ConsistentHashRing(["a", "b", "c"], vnodes=50)
        r2 = ConsistentHashRing(["a", "b", "c"], vnodes=50)
        for i in range(500):
            self.assertEqual(r1.get_node(f"x{i}"), r2.get_node(f"x{i}"))


class TestTrie(unittest.TestCase):
    def test_prefix_topk_sorted_by_count(self):
        t = SuggestionTrie(node_cap=10)
        t.bulk_insert([("apple", 100), ("app", 500), ("application", 50),
                       ("apply", 300), ("banana", 999)])
        top = t.top_for_prefix("app", 10)
        self.assertEqual([q for q, _ in top], ["app", "apply", "apple", "application"])
        self.assertNotIn("banana", [q for q, _ in top])

    def test_limit_respected(self):
        t = SuggestionTrie(node_cap=25)
        for i in range(50):
            t.insert(f"car{i:02d}", i)
        self.assertEqual(len(t.top_for_prefix("car", 10)), 10)

    def test_monotonic_update_promotes_query(self):
        t = SuggestionTrie(node_cap=10)
        t.bulk_insert([("cat", 10), ("car", 20), ("can", 30)])
        self.assertEqual(t.top_for_prefix("ca", 1)[0][0], "can")
        t.update("cat", 1000)  # cat is searched a lot
        self.assertEqual(t.top_for_prefix("ca", 1)[0][0], "cat")

    def test_no_match(self):
        t = SuggestionTrie()
        t.insert("hello", 1)
        self.assertEqual(t.top_for_prefix("zzz", 10), [])


class TestDistributedCache(unittest.TestCase):
    def _cache(self):
        return DistributedCache(["n1", "n2", "n3"], vnodes=50, capacity_per_node=3)

    def test_set_get_roundtrip(self):
        c = self._cache()
        c.set("r0:iph", [{"query": "iphone"}], ttl=60)
        self.assertEqual(c.get("r0:iph"), [{"query": "iphone"}])

    def test_ttl_expiry(self):
        c = self._cache()
        c.set("r0:x", [1], ttl=-1)  # already expired
        self.assertIsNone(c.get("r0:x"))
        self.assertEqual(c.debug("r0:x")["status"], "miss")

    def test_lru_eviction(self):
        c = self._cache()
        # All keys land on whatever node owns them; force capacity on one node.
        node = list(c.nodes.values())[0]
        for i in range(5):
            node.set(f"k{i}", i, expiry=1e18)
        self.assertLessEqual(node.size(), 3)  # capacity_per_node = 3

    def test_invalidation(self):
        c = self._cache()
        c.set("r0:iph", ["v"], ttl=60)
        self.assertTrue(c.invalidate("r0:iph"))
        self.assertIsNone(c.get("r0:iph"))

    def test_debug_reports_owner(self):
        c = self._cache()
        d = c.debug("r0:iph")
        self.assertIn(d["owner_node"], ["n1", "n2", "n3"])
        self.assertEqual(d["status"], "miss")


class TestRecency(unittest.TestCase):
    def test_decay_halves_over_half_life(self):
        rt = RecencyTracker(half_life_seconds=10.0)
        rt.record("q", now=0.0)
        self.assertAlmostEqual(rt.current("q", now=0.0), 1.0, places=3)
        self.assertAlmostEqual(rt.current("q", now=10.0), 0.5, places=2)
        self.assertAlmostEqual(rt.current("q", now=20.0), 0.25, places=2)

    def test_trending_orders_by_recent_activity(self):
        rt = RecencyTracker(half_life_seconds=60.0)
        for _ in range(5):
            rt.record("hot", now=100.0)
        rt.record("cold", now=0.0)
        top = rt.trending(2, now=100.0)
        self.assertEqual(top[0][0], "hot")

    def test_spike_fades(self):
        rt = RecencyTracker(half_life_seconds=5.0, prune_threshold=0.01)
        for _ in range(10):
            rt.record("spike", now=0.0)
        early = rt.current("spike", now=0.0)
        late = rt.current("spike", now=60.0)  # 12 half-lives later
        self.assertGreater(early, 5.0)
        self.assertLess(late, 0.01)

    def test_blended_score_combines_terms(self):
        # Higher recency must be able to outrank higher all-time count.
        popular = blended_score(1_000_000, 0.0, 1.0, 2.5)
        trending = blended_score(1000, 30.0, 1.0, 2.5)
        self.assertGreater(trending, popular)


class TestBatchWriter(unittest.TestCase):
    def test_aggregates_and_flushes_on_stop(self):
        flushed = {}

        def cb(deltas):
            for q, d in deltas.items():
                flushed[q] = flushed.get(q, 0) + d

        bw = BatchWriter(cb, interval_seconds=60.0, max_size=10_000)
        bw.start()
        for _ in range(7):
            bw.enqueue("apple")
        for _ in range(3):
            bw.enqueue("banana")
        bw.stop()  # triggers a final flush

        self.assertEqual(flushed.get("apple"), 7)  # 7 searches -> 1 row, delta 7
        self.assertEqual(flushed.get("banana"), 3)
        stats = bw.stats()
        self.assertEqual(stats["search_events_received"], 10)
        self.assertEqual(stats["db_rows_written"], 2)  # 10 searches -> 2 rows

    def test_size_trigger_flushes_early(self):
        flushed = []
        bw = BatchWriter(lambda d: flushed.append(dict(d)),
                         interval_seconds=60.0, max_size=3)
        bw.start()
        for q in ["a", "b", "c", "d"]:  # 3 distinct trips the size trigger
            bw.enqueue(q)
        bw.stop()
        self.assertGreaterEqual(bw.flush_count, 1)
        total = sum(sum(b.values()) for b in flushed)
        self.assertEqual(total, 4)


if __name__ == "__main__":
    unittest.main()
