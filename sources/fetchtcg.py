#!/usr/bin/env python3
"""
Source: fetchtcg.com, a multi-seller Riftbound marketplace.

fetchtcg.com's own frontend talks to a public, unauthenticated JSON API on
api.fetchtcg.com. None of this is documented publicly; the endpoints below
were found by inspecting the site's own network traffic.

  SEARCH    GET /v3/cards?cardName=<name>&gameIds=rift&pageSize=48&pageOffset=0
            Fuzzy name search. One row per (set, finish) printing, each with
            a stable `id`. Metadata only, no price/seller data.

  LISTINGS  GET /v3/cards/{id}/listings
                ?countryCode=NZ&currencyCode=NZD&pageSize=50&pageOffset=0&sort=PRICE_ASC
            Per-seller listings for one printing: price, seller username,
            condition, remaining quantity, region.

  SHIPPING  GET /v1/social/{sellerName}/shipping
            A seller's own shipping rate card, banded by card count:
            [{"title": "Courier Tracked", "minCards": 1, "maxCards": 50, "total": 8}, ...]
            Some rows aren't a real remote-shipping quote (local pickup,
            "combined with another order", or a manual arrangement where the
            seller put the real price in the title and left `total` at 0) --
            _NOT_REAL_SHIPPING filters those out. A seller with no genuine
            rate card left falls back to DEFAULT_SHIPPING.

RATE LIMITING
-------------
Only the search endpoint is limited: 19 requests succeed, the 20th gets
HTTP 429 with a Retry-After header, in a fixed ~40s window. Listings and
shipping have no such limit. SEARCH_REQUEST_DELAY paces search calls to
stay under that; get_json's Retry-After-aware backoff is the fallback if
it's hit anyway.

CARD NAME MATCHING
-------------------
The search API's relevance ranking is thrown off by punctuation ("B.F.
Sword" returns nothing useful; "B F Sword" does), so the query is stripped
to letters/digits before searching, and results are matched back with the
shared normalizer (sources.util.normalize_name).

Staples get reprinted across many sets ("Order Rune" exists in five+).
This pools listings across every non-collector-variant printing of a name
(both finishes unless FOIL_OK is False) and lets the optimiser pick the
cheapest. Collector variants (Alternate Art, Signature, Overnumbered,
Metal, Prize Wall, etc. -- anything with "(" in the card name) are
excluded as pricier variants of the same card.
"""

import re
import time
import urllib.parse
from typing import Dict, List, Tuple

from cart_optimiser import Listing, Seller
from . import util

NAME = "FetchTCG"

API = "https://api.fetchtcg.com"
SITE = "https://www.fetchtcg.com"
GAME_ID = "rift"
COUNTRY_CODE = "NZ"
CURRENCY_CODE = "NZD"

INCLUDE_RURAL = False
DEFAULT_SHIPPING = 8.00  # fallback only -- used when a seller has no public rate card
FOIL_OK = True           # include foil listings alongside normal (they play identically)

SEARCH_REQUEST_DELAY = 2.5  # see RATE LIMITING above

_NOT_REAL_SHIPPING = ("pickup", "pick up", "drop off", "dropoff",
                     "combined", "collect", "local", "bank transfer")


# ---------------------------------------------------------------------------
# Resolving card names -> candidate printing ids
# ---------------------------------------------------------------------------

def search_card(name: str) -> List[dict]:
    """Raises util.RateLimited on persistent 429 -- callers must not treat
    that the same as a genuine zero-result search."""
    query = urllib.parse.quote(re.sub(r"[^A-Za-z0-9 ]+", " ", name))
    url = (f"{API}/v3/cards?pageSize=48&pageOffset=0&sort=RELEVANCE_DESC"
           f"&cardName={query}&gameIds={GAME_ID}")
    data = util.get_json(url)
    if not data:
        return []
    return data.get("searchResults", {}).get("content", [])


def _search_and_filter(name: str) -> List[dict]:
    target = util.normalize_name(name)
    candidates = [c for c in search_card(name)
                  if util.normalize_name(c["cardName"]) == target]
    candidates = [c for c in candidates if "(" not in c["cardName"]]
    if not FOIL_OK:
        candidates = [c for c in candidates
                      if "_normal" in c["id"] or "standard_normal" in c["id"]]
    return candidates


def resolve_ids(card_names: List[str]) -> Dict[str, List[dict]]:
    """card name -> candidate printing dicts (id, cardName, ...), restricted
    to non-collector-variant printings, both finishes unless FOIL_OK is
    False.

    A card that stays rate-limited through every retry is excluded from the
    result (so the rest of the pipeline still runs) rather than folded into
    "not resolved" -- those are different things, and conflating them would
    silently drop a real listing from the candidate pool. It's reported via
    util.warn() instead, which fetch_and_optimise.run() reprints as a
    banner after the results.
    """
    resolved: Dict[str, List[dict]] = {}
    empty: List[str] = []
    rate_limited: List[str] = []
    for i, name in enumerate(card_names, 1):
        print(f"  [{NAME}] search [{i}/{len(card_names)}] {name}")
        try:
            candidates = _search_and_filter(name)
        except util.RateLimited:
            rate_limited.append(name)
            continue
        if candidates:
            resolved[name] = candidates
        else:
            empty.append(name)
        time.sleep(SEARCH_REQUEST_DELAY)

    # A zero-result search is often a transient hiccup rather than a genuine
    # absence -- retry before giving up. Rate-limited names are retried here
    # too, since they were never actually searched the first time.
    to_retry = empty + rate_limited
    if to_retry:
        print(f"\n  [{NAME}] retrying {len(to_retry)} name(s) that came back empty "
              f"or were rate-limited...")
        still_rate_limited: List[str] = []
        for name in to_retry:
            time.sleep(SEARCH_REQUEST_DELAY)
            try:
                candidates = _search_and_filter(name)
            except util.RateLimited:
                still_rate_limited.append(name)
                continue
            if candidates:
                resolved[name] = candidates
            else:
                print(f"      NOT RESOLVED -- no exact match on fetchtcg for {name!r}")
        if still_rate_limited:
            util.warn(f"[{NAME}] RATE LIMITED, not genuinely absent -- these cards were "
                     f"never actually searched, so they're missing from this source, "
                     f"not confirmed absent: {', '.join(still_rate_limited)}. Re-run "
                     f"before trusting the total for these.")
    return resolved


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def fetch_listings_for_id(card_id: str) -> Tuple[List[dict], bool]:
    """All listing rows for one printing id, paginated. -> (rows, complete).
    `complete` is False if persistent rate-limiting cut pagination short --
    `rows` is then a possibly-incomplete undercount, not the full picture."""
    rows: List[dict] = []
    page = 0
    while True:
        encoded_id = urllib.parse.quote(card_id, safe="")
        url = (f"{API}/v3/cards/{encoded_id}/listings"
               f"?countryCode={COUNTRY_CODE}&currencyCode={CURRENCY_CODE}"
               f"&pageSize=50&pageOffset={page}&sort=PRICE_ASC")
        try:
            data = util.get_json(url)
        except util.RateLimited:
            return rows, False
        time.sleep(util.REQUEST_DELAY)
        if not data:
            break
        sr = data.get("searchResults", {})
        content = sr.get("content", [])
        rows.extend(content)
        if sr.get("last", True) or not content:
            break
        page += 1
    return rows, True


def fetch_all_listings(resolved: Dict[str, List[dict]]) -> List[Listing]:
    out: List[Listing] = []
    total_ids = sum(len(v) for v in resolved.values())
    done = 0
    incomplete: List[str] = []
    for card_name, candidates in resolved.items():
        for cand in candidates:
            done += 1
            print(f"  [{NAME}] listings [{done}/{total_ids}] {card_name}  "
                  f"({cand['id']})")
            rows, complete = fetch_listings_for_id(cand["id"])
            if not complete:
                incomplete.append(f"{card_name} ({cand['id']})")
            for row in rows:
                if row.get("status") != "ACTIVE" or row.get("sellerOnHoliday"):
                    continue
                price = row.get("listedPriceInRequestedCurrency")
                seller = row.get("sellerProfileName")
                qty = row.get("remainingQuantity") or 0
                if not (price and seller and qty > 0):
                    continue
                out.append(Listing(card=card_name, seller=seller,
                                   price=round(float(price), 2), stock=int(qty)))
    if incomplete:
        util.warn(f"[{NAME}] RATE LIMITED partway through fetching listings for: "
                 f"{', '.join(incomplete)}. Listings captured before that point are "
                 f"still used, but cheaper ones may exist that this run never saw.")
    return out


# ---------------------------------------------------------------------------
# Shipping
# ---------------------------------------------------------------------------

def fetch_shipping_tiers(seller_name: str) -> Tuple[List[tuple], bool]:
    """A seller's rate card as [(min_cards, max_cards, cost), ...], filtered
    to genuine, non-rural, card-shipping tiers -- cart_optimiser picks the
    cheapest eligible tier for a given quantity. -> (tiers, complete);
    `complete` is False on persistent rate limiting, distinct from a seller
    genuinely having no public rate card (both fall back to
    DEFAULT_SHIPPING, but only the former is wrong rather than absent)."""
    encoded = urllib.parse.quote(seller_name, safe="")
    try:
        data = util.get_json(f"{API}/v1/social/{encoded}/shipping")
    except util.RateLimited:
        return [], False
    if not data:
        return [], True
    tiers = []
    for row in data:
        if "Cards" not in row.get("supports", []):
            continue
        title = row.get("title", "").lower()
        if not INCLUDE_RURAL and "rural" in title:
            continue
        if any(kw in title for kw in _NOT_REAL_SHIPPING):
            continue
        cost = float(row["total"])
        if cost <= 0:
            continue  # a stated $0 on a real courier line is not credible
        lo = row.get("minCards")
        hi = row.get("maxCards")
        tiers.append((lo if lo is not None else 0,
                      hi if hi is not None else 10**9,
                      cost))
    return tiers, True


def fetch_all_shipping(seller_names: List[str]) -> Dict[str, List[tuple]]:
    out: Dict[str, List[tuple]] = {}
    rate_limited: List[str] = []
    for i, name in enumerate(seller_names, 1):
        print(f"  [{NAME}] shipping [{i}/{len(seller_names)}] {name}")
        tiers, complete = fetch_shipping_tiers(name)
        out[name] = tiers
        if not complete:
            rate_limited.append(name)
        time.sleep(util.REQUEST_DELAY)
    if rate_limited:
        util.warn(f"[{NAME}] RATE LIMITED looking up shipping rates for: "
                 f"{', '.join(rate_limited)} -- the ${DEFAULT_SHIPPING:.2f} flat "
                 f"fallback for them is a guess, not a confirmed absence of a public "
                 f"rate card.")
    return out


# ---------------------------------------------------------------------------

def fetch(want: Dict[str, int]) -> Tuple[List[Listing], List[Seller]]:
    card_names = list(want)
    resolved = resolve_ids(card_names)
    missing = [c for c in card_names if c not in resolved]
    if missing:
        print(f"\n  [{NAME}] could not resolve: {', '.join(missing)}")

    listings = fetch_all_listings(resolved)
    seller_names = sorted({l.seller for l in listings})
    print(f"\n  [{NAME}] fetching shipping rates for {len(seller_names)} sellers...")
    shipping = fetch_all_shipping(seller_names)

    sellers = []
    unresolved_shipping = []
    for name in seller_names:
        tiers = shipping.get(name) or []
        if not tiers:
            unresolved_shipping.append(name)
        sellers.append(Seller(name=name, shipping=DEFAULT_SHIPPING,
                              shipping_tiers=tiers or None,
                              url=f"{SITE}/profiles/{urllib.parse.quote(name, safe='')}"))
    if unresolved_shipping:
        print(f"\n  [{NAME}] no public shipping rate for {len(unresolved_shipping)} "
              f"seller(s) (pickup-only or lookup failed) -- using "
              f"${DEFAULT_SHIPPING:.2f} flat fallback: "
              f"{', '.join(unresolved_shipping[:10])}"
              f"{' ...' if len(unresolved_shipping) > 10 else ''}")

    return listings, sellers
