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

LOCAL PICKUP
------------
Each listing carries the seller's own `regionAddress` (city/region,
country, and an `allowPickups` flag), and a seller's shipping rate card
often has a genuine $0 "pickup" line alongside their paid courier tiers
-- e.g. "AUCKLAND PICKUP. CHECK BIO FOR UPCOMING AUCKLAND DATE", $0, no
card-count limit. fetch_shipping_tiers() normally excludes pickup-ish
rows via _NOT_REAL_SHIPPING (rightly -- it isn't a *remote* shipping
option, you can't have $0 delivered to your door), but that meant this
tool never told you pickup existed at all, even though a large fraction
of the NZ seller pool turns out to be Auckland-based with
allowPickups=True. Since the rest of this codebase already assumes an
Auckland buyer (see nz_shopify.py), sellers whose own regionAddress says
Auckland AND who publish a $0 pickup tier get that tier added back in
(as an unrestricted (0, inf, 0.0) shipping_tiers entry) and the resulting
Seller gets a `pickup_note` cart_optimiser.report() surfaces whenever a
plan actually lands on $0 shipping for them -- so "$0 shipping" always
says up front that it means meeting the seller, not a free courier.
Skipping courier entirely on 3-4 sellers already in a typical plan is a
real double-digit-dollar saving, not a rounding error.

This only ever records the FACT that a seller offers genuine local
pickup (Seller.pickup_cost/pickup_note) -- whether a plan actually uses
it, for which sellers, and how many pickups at once is a policy decision
made by cart_optimiser.PickupPolicy at solve time, not decided here.

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

import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

BUYER_REGION = "auckland"  # see LOCAL PICKUP above -- matches nz_shopify.py's assumption

# A seller's shipping rate card (and whether they offer pickup) changes far
# less often than card prices/stock do -- there's no reason to re-hit
# /v1/social/{seller}/shipping for a seller we already have a recent answer
# for just because some OTHER card's listing went stale and triggered a
# re-fetch. So this gets its own on-disk cache, decoupled from (and much
# longer-lived than) fetch_and_optimise.py's per-card CACHE_TTL (1 hour) --
# see fetch_all_shipping().
SHIPPING_CACHE_FILE = Path(__file__).resolve().parent.parent / "seller_shipping_cache.json"
SHIPPING_CACHE_TTL = 24 * 3600  # 1 day

_NOT_REAL_SHIPPING = ("pickup", "pick up", "drop off", "dropoff",
                     "combined", "collect", "local", "bank transfer")
_PICKUP_WORDS = ("pickup", "pick up", "collect")


def _load_shipping_cache() -> Dict[str, dict]:
    try:
        return json.loads(SHIPPING_CACHE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_shipping_cache(cache: Dict[str, dict]) -> None:
    SHIPPING_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _seller_is_local(region_address: Optional[dict]) -> bool:
    """True if a listing's seller region_address puts them in BUYER_REGION
    and they've said they're open to pickups at all."""
    if not region_address or not region_address.get("allowPickups"):
        return False
    where = f"{region_address.get('city') or ''} {region_address.get('suburb') or ''}".lower()
    return BUYER_REGION in where


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


def fetch_all_listings(resolved: Dict[str, List[dict]]) -> Tuple[List[Listing], Dict[str, dict]]:
    """-> (listings, seller_region). seller_region maps sellerProfileName ->
    that listing's regionAddress dict (same for every listing from one
    seller), used later to decide who's close enough to offer pickup to --
    see LOCAL PICKUP above."""
    out: List[Listing] = []
    seller_region: Dict[str, dict] = {}
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
                if seller not in seller_region and row.get("regionAddress"):
                    seller_region[seller] = row["regionAddress"]
    if incomplete:
        util.warn(f"[{NAME}] RATE LIMITED partway through fetching listings for: "
                 f"{', '.join(incomplete)}. Listings captured before that point are "
                 f"still used, but cheaper ones may exist that this run never saw.")
    return out, seller_region


# ---------------------------------------------------------------------------
# Shipping
# ---------------------------------------------------------------------------

def fetch_shipping_tiers(seller_name: str) -> Tuple[List[tuple], Optional[str], bool]:
    """A seller's rate card as [(min_cards, max_cards, cost), ...], filtered
    to genuine, non-rural, card-shipping tiers -- cart_optimiser picks the
    cheapest eligible tier for a given quantity. -> (tiers, pickup_title,
    complete). `pickup_title` is that seller's own title for a real $0
    pickup row if they publish one (e.g. "AUCKLAND PICKUP..."), independent
    of whether the caller ends up using it -- see LOCAL PICKUP above for why
    that's kept separate from `tiers` rather than folded in unconditionally.
    `complete` is False on persistent rate limiting, distinct from a seller
    genuinely having no public rate card (both fall back to
    DEFAULT_SHIPPING, but only the former is wrong rather than absent)."""
    encoded = urllib.parse.quote(seller_name, safe="")
    try:
        data = util.get_json(f"{API}/v1/social/{encoded}/shipping")
    except util.RateLimited:
        return [], None, False
    if not data:
        return [], None, True
    tiers = []
    pickup_title = None
    for row in data:
        if "Cards" not in row.get("supports", []):
            continue
        title = row.get("title", "")
        title_lower = title.lower()
        cost = float(row["total"])
        if pickup_title is None and cost == 0 and any(kw in title_lower for kw in _PICKUP_WORDS):
            pickup_title = title
        if not INCLUDE_RURAL and "rural" in title_lower:
            continue
        if any(kw in title_lower for kw in _NOT_REAL_SHIPPING):
            continue
        if cost <= 0:
            continue  # a stated $0 on a real courier line is not credible
        lo = row.get("minCards")
        hi = row.get("maxCards")
        tiers.append((lo if lo is not None else 0,
                      hi if hi is not None else 10**9,
                      cost))
    return tiers, pickup_title, True


def fetch_all_shipping(seller_names: List[str],
                       seller_region: Dict[str, dict]) -> Tuple[Dict[str, List[tuple]], Dict[str, str]]:
    """-> (tiers_by_seller, pickup_title_by_seller). The latter only
    includes sellers who are both local (see _seller_is_local) AND publish
    a real $0 pickup row -- it's kept out of tiers_by_seller entirely
    (rather than injected as a tier) so pickup stays a distinct, optional
    fact about the seller that callers can choose to use or not, instead
    of being unconditionally baked into their shipping cost.

    Backed by SHIPPING_CACHE_FILE (TTL: SHIPPING_CACHE_TTL, a day by
    default) -- a seller whose rate card was fetched recently is served
    straight from that file, no network call at all, since a shipping
    policy doesn't change nearly as often as a card's price/stock does.
    _seller_is_local() is still evaluated fresh every call against the
    CURRENT seller_region (which comes from this run's own listings, not
    the cache) since pickup eligibility depends on your want-list's actual
    matched sellers, not on anything worth caching itself."""
    cache = _load_shipping_cache()
    now = time.time()

    out: Dict[str, List[tuple]] = {}
    pickup: Dict[str, str] = {}
    cache_titles: Dict[str, Optional[str]] = {}
    to_fetch: List[str] = []
    for name in seller_names:
        entry = cache.get(name)
        if entry and now - entry.get("fetched_at", 0) <= SHIPPING_CACHE_TTL:
            out[name] = [tuple(t) for t in entry["tiers"]]
            cache_titles[name] = entry.get("pickup_title")
        else:
            to_fetch.append(name)

    rate_limited: List[str] = []
    for i, name in enumerate(to_fetch, 1):
        print(f"  [{NAME}] shipping [{i}/{len(to_fetch)}] {name}")
        tiers, pickup_title, complete = fetch_shipping_tiers(name)
        out[name] = tiers
        cache_titles[name] = pickup_title
        if complete:
            cache[name] = {"tiers": tiers, "pickup_title": pickup_title, "fetched_at": now}
        else:
            rate_limited.append(name)
        time.sleep(util.REQUEST_DELAY)
    if to_fetch:
        _save_shipping_cache(cache)

    cached_hits = len(seller_names) - len(to_fetch)
    if cached_hits:
        print(f"  [{NAME}] {cached_hits} seller(s)' shipping rates reused from cache "
              f"(within {SHIPPING_CACHE_TTL // 3600}h), {len(to_fetch)} fetched fresh")

    for name, title in cache_titles.items():
        if title and _seller_is_local(seller_region.get(name)):
            pickup[name] = title

    if rate_limited:
        util.warn(f"[{NAME}] RATE LIMITED looking up shipping rates for: "
                 f"{', '.join(rate_limited)} -- the ${DEFAULT_SHIPPING:.2f} flat "
                 f"fallback for them is a guess, not a confirmed absence of a public "
                 f"rate card.")
    if pickup:
        print(f"\n  [{NAME}] {len(pickup)} Auckland-based seller(s) offer real $0 "
              f"pickup, factored in as an option: {', '.join(sorted(pickup))}")
    return out, pickup


# ---------------------------------------------------------------------------

def fetch(want: Dict[str, int]) -> Tuple[List[Listing], List[Seller]]:
    card_names = list(want)
    resolved = resolve_ids(card_names)
    missing = [c for c in card_names if c not in resolved]
    if missing:
        print(f"\n  [{NAME}] could not resolve: {', '.join(missing)}")

    listings, seller_region = fetch_all_listings(resolved)
    seller_names = sorted({l.seller for l in listings})
    print(f"\n  [{NAME}] fetching shipping rates for {len(seller_names)} sellers...")
    shipping, pickup = fetch_all_shipping(seller_names, seller_region)

    sellers = []
    unresolved_shipping = []
    for name in seller_names:
        tiers = shipping.get(name) or []
        if not tiers:
            unresolved_shipping.append(name)
        pickup_cost = None
        pickup_note = None
        if name in pickup:
            region = seller_region.get(name) or {}
            where = region.get("city") or BUYER_REGION.title()
            pickup_cost = 0.0
            pickup_note = (f"{name} is based in {where} and offers pickup "
                           f"(\"{pickup[name]}\") -- meet them in person to skip "
                           f"courier shipping entirely.")
        sellers.append(Seller(name=name, shipping=DEFAULT_SHIPPING,
                              shipping_tiers=tiers or None,
                              url=f"{SITE}/profiles/{urllib.parse.quote(name, safe='')}",
                              pickup_cost=pickup_cost,
                              pickup_note=pickup_note))
    if unresolved_shipping:
        print(f"\n  [{NAME}] no public shipping rate for {len(unresolved_shipping)} "
              f"seller(s) (pickup-only or lookup failed) -- using "
              f"${DEFAULT_SHIPPING:.2f} flat fallback: "
              f"{', '.join(unresolved_shipping[:10])}"
              f"{' ...' if len(unresolved_shipping) > 10 else ''}")

    return listings, sellers
