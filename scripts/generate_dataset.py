#!/usr/bin/env python3
"""Generate a realistic search-query dataset for the typeahead system.

The assignment requires a dataset of at least 100,000 queries, each with a
``count`` (search frequency). Rather than depend on a network download, this
script *synthesizes* a dataset that has the two properties that actually matter
for a typeahead demo:

1.  **Realistic surface form** — queries look like real searches ("iphone 15
    price", "best running shoes", "python list comprehension") and share long
    common prefixes, which is exactly what stresses a prefix index.
2.  **A Zipfian / long-tail count distribution** — a handful of head queries are
    searched millions of times while the long tail is searched once or twice.
    Real query logs (AOL, web search, e-commerce) all follow this shape, so the
    cache hit-rate and ranking behaviour you observe here mirror production.

Counts are derived as ``head_popularity * modifier_weight``, where
``head_popularity`` follows a Zipf curve over the head terms. This makes the
ranking meaningful (popular heads dominate) while still producing a smooth tail.

The generator is fully deterministic (fixed seed) so the dataset is
reproducible. To use a *real* open-source dataset instead, just drop a CSV with
``query,count`` columns at ``data/queries.csv`` — the app reads any such file.

Usage:
    python scripts/generate_dataset.py --rows 120000 --out data/queries.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from typing import Dict, List, Tuple

SEED = 42

# --- Vocabulary -------------------------------------------------------------
BRANDS = [
    "apple", "samsung", "sony", "lg", "dell", "hp", "lenovo", "asus", "acer",
    "microsoft", "google", "nike", "adidas", "puma", "reebok", "canon", "nikon",
    "bose", "jbl", "xiaomi", "oneplus", "realme", "oppo", "vivo", "huawei",
    "nokia", "motorola", "amazon", "logitech", "razer", "corsair", "intel",
    "amd", "nvidia", "anker", "sandisk", "seagate", "western digital", "philips",
    "panasonic", "dyson", "gopro", "fitbit", "garmin", "tcl",
]
PRODUCTS = [
    "phone", "laptop", "tablet", "smartwatch", "headphones", "earbuds",
    "monitor", "keyboard", "mouse", "camera", "tv", "speaker", "charger",
    "case", "cable", "adapter", "router", "printer", "ssd", "hard drive",
    "graphics card", "processor", "power bank", "webcam", "microphone",
    "gaming chair", "desk", "smart bulb", "vacuum", "air fryer",
]
TOPICS = [
    "python", "javascript", "java", "typescript", "golang", "rust", "react",
    "angular", "vue", "django", "flask", "fastapi", "spring boot", "kubernetes",
    "docker", "redis", "postgres", "mongodb", "kafka", "system design",
    "machine learning", "data structures", "algorithms", "leetcode", "git",
    "linux", "aws", "terraform", "graphql", "tailwind", "nextjs", "node",
]
TECH_SUB = [
    "tutorial", "interview questions", "list comprehension", "decorators",
    "async await", "generics", "memory leak", "best practices", "cheat sheet",
    "design patterns", "rest api", "unit testing", "performance tuning",
    "error handling", "deployment", "authentication", "pagination", "caching",
    "rate limiting", "consistent hashing", "load balancing",
]
PLACES = [
    "new york", "san francisco", "london", "paris", "tokyo", "mumbai", "delhi",
    "bangalore", "berlin", "toronto", "sydney", "singapore", "dubai", "seattle",
    "austin", "chicago", "boston", "amsterdam",
]
FOOD = [
    "pizza", "sushi", "burger", "pasta", "biryani", "tacos", "ramen", "coffee",
    "chocolate cake", "pancakes", "smoothie", "salad", "butter chicken",
    "ice cream", "dumplings", "lasagna", "paneer tikka", "cheesecake",
]
RECIPE_SUB = [
    "recipe", "near me", "delivery", "restaurant", "calories", "easy recipe",
    "homemade", "best in town", "ingredients",
]
GENERIC = [
    "weather", "news", "stock price", "movie", "song lyrics", "hotel", "flight",
    "recipe", "workout", "meditation", "resume template", "interview questions",
    "salary", "tutorial", "cheat sheet", "wallpaper", "online course", "vpn",
    "password manager", "budget tracker", "to do app",
]

# Modifiers (suffixes) with a relative weight. Lower-weight suffixes are rarer
# searches, which spreads counts across the tail.
MODIFIERS: List[Tuple[str, float]] = [
    ("", 1.00),
    ("price", 0.62), ("review", 0.55), ("near me", 0.50), ("vs", 0.30),
    ("best", 0.48), ("buy", 0.40), ("specs", 0.28), ("deals", 0.33),
    ("tutorial", 0.45), ("for beginners", 0.30), ("cheap", 0.26),
    ("2024", 0.35), ("2025", 0.42), ("2026", 0.30), ("alternatives", 0.20),
    ("how to use", 0.38), ("setup", 0.24), ("not working", 0.22), ("guide", 0.34),
    ("comparison", 0.18), ("offers", 0.21), ("discount", 0.19), ("warranty", 0.12),
    ("manual", 0.10), ("size chart", 0.11), ("colors", 0.14), ("battery life", 0.16),
    ("release date", 0.23), ("rumors", 0.13), ("pro max", 0.27), ("mini", 0.17),
    ("refurbished", 0.09), ("trade in", 0.08), ("accessories", 0.15),
    ("in india", 0.31), ("in usa", 0.20), ("amazon", 0.29), ("flipkart", 0.18),
    ("vs iphone", 0.16), ("for students", 0.22), ("for gaming", 0.25),
    ("under 500", 0.19), ("under 1000", 0.21), ("black friday", 0.24),
    ("free shipping", 0.14), ("with case", 0.10), ("second hand", 0.12),
    ("online", 0.27), ("today", 0.15),
]

PREFIX_MODS = ["best ", "cheap ", "buy ", "top ", "used "]

HEAD_MAX = 2_500_000  # search count of the single most popular head
ZIPF_EXPONENT = 0.92  # popularity ~ 1 / rank**exponent


def _build_heads() -> List[str]:
    """Construct the universe of head terms (the words a user starts typing)."""
    heads: List[str] = []
    for b in BRANDS:
        for p in PRODUCTS:
            heads.append(f"{b} {p}")          # ~1350 product heads
    for t in TOPICS:
        heads.append(t)
        for s in TECH_SUB:
            heads.append(f"{t} {s}")          # ~700 tech heads
    for fd in FOOD:
        heads.append(fd)
        for s in RECIPE_SUB:
            heads.append(f"{fd} {s}")         # ~170 food heads
    heads.extend(PLACES)
    heads.extend(GENERIC)
    heads.extend(BRANDS)                       # bare brand searches
    # Brand model lines, e.g. "iphone 11".."iphone 16", "galaxy s20".."s24".
    for base, lo, hi, sep in [
        ("iphone", 11, 18, " "), ("galaxy s", 20, 26, ""), ("pixel", 6, 11, " "),
        ("macbook pro", 14, 17, " m"), ("xps", 13, 18, " "), ("thinkpad x", 1, 12, ""),
    ]:
        for n in range(lo, hi):
            heads.append(f"{base}{sep}{n}".strip())
    # De-duplicate while preserving order.
    seen = set()
    unique = []
    for h in heads:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return unique


def generate(rows: int, out_path: str, seed: int = SEED) -> int:
    """Generate ``>= rows`` unique queries and write them to ``out_path``.

    Returns the number of rows actually written.
    """
    rng = random.Random(seed)
    heads = _build_heads()
    rng.shuffle(heads)  # mix categories so popularity isn't grouped by type

    # Assign each head a Zipfian popularity by rank.
    head_pop: Dict[str, float] = {}
    for rank, head in enumerate(heads, start=1):
        head_pop[head] = HEAD_MAX / (rank ** ZIPF_EXPONENT)

    queries: Dict[str, int] = {}

    def add(text: str, count: int) -> None:
        text = " ".join(text.split())  # normalise whitespace
        if not text:
            return
        if count > queries.get(text, 0):  # keep highest count on collision
            queries[text] = count

    for head in heads:
        pop = head_pop[head]
        for suffix, weight in MODIFIERS:
            text = f"{head} {suffix}" if suffix else head
            jitter = rng.uniform(0.75, 1.25)  # deterministic per-seed jitter
            add(text, max(1, int(pop * weight * jitter)))
        if pop > HEAD_MAX / 300:  # "best <head>" variants for popular heads only
            for pm in PREFIX_MODS:
                add(f"{pm}{head}", max(1, int(pop * 0.2 * rng.uniform(0.7, 1.3))))

    # Top up with related-head pairs ("react vs angular") if we are still short.
    if len(queries) < rows:
        pool = TOPICS + BRANDS + FOOD
        for a in pool:
            for b in pool:
                if a != b:
                    add(f"{a} vs {b}", max(1, int(rng.uniform(50, 5000))))
            if len(queries) >= rows:
                break

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    ordered = sorted(queries.items(), key=lambda kv: kv[1], reverse=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["query", "count"])
        writer.writerows(ordered)
    return len(ordered)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate typeahead dataset")
    parser.add_argument("--rows", type=int, default=120_000,
                        help="minimum number of unique queries to generate")
    parser.add_argument("--out", default="data/queries.csv", help="output CSV path")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    n = generate(args.rows, args.out, args.seed)
    print(f"Wrote {n:,} queries to {args.out}")


if __name__ == "__main__":
    main()
