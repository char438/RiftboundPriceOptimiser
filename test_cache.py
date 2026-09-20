#!/usr/bin/env python3
"""Self-check for fetch_and_optimise.merge_cache. Run: python3 test_cache.py"""

from cart_optimiser import Listing, Seller
from fetch_and_optimise import merge_cache

cached_listings = [Listing(card="Defy", seller="A", price=1.0),
                   Listing(card="Arise!", seller="B", price=30.0)]
cached_sellers = [Seller(name="A", shipping=5), Seller(name="B", shipping=8)]
fetched_at = {"Defy": 100.0, "Arise!": 100.0}

# Re-fetch only "Arise!" -- "Defy" should survive untouched.
fresh_listings = [Listing(card="Arise!", seller="C", price=25.0)]
fresh_sellers = [Seller(name="C", shipping=6)]

listings, sellers, new_fetched_at = merge_cache(
    cached_listings, cached_sellers, fetched_at, {"Arise!"},
    fresh_listings, fresh_sellers, now=200.0)

assert {l.card for l in listings} == {"Defy", "Arise!"}
assert [l for l in listings if l.card == "Arise!"] == fresh_listings, "stale card not replaced"
assert [l for l in listings if l.card == "Defy"] == cached_listings[:1], "fresh card lost cached data"
assert {s.name for s in sellers} == {"A", "B", "C"}
assert new_fetched_at["Arise!"] == 200.0, "stale card's timestamp not refreshed"
assert new_fetched_at["Defy"] == 100.0, "untouched card's timestamp changed"

print("ok")
