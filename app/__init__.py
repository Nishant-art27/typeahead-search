"""Search Typeahead — backend package.

A low-latency search-suggestion system built around four data-system ideas:

* an in-memory **Trie** index for prefix candidate generation,
* a **distributed cache** of suggestion results routed by **consistent hashing**,
* **recency-aware ranking** for trending searches, and
* a **batch writer** that absorbs write pressure on the primary store.

See ``docs/ARCHITECTURE.md`` for the full design write-up.
"""

__all__ = ["config"]
