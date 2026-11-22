#!/usr/bin/env python3
"""
Source: NZ single-store TCG retailers -- Card Merchant, Card Masters,
Calico Keep, Game Roost, etc. Unlike Fetch TCG these are each ONE store
(one inventory, one shipping policy), not a marketplace of many sellers --
so every store here becomes exactly one Seller.

DATA SOURCE
-----------
Every store in STORES runs Shopify, which exposes a public,
unauthenticated JSON mirror of any collection:

    GET https://{store}/collections/{handle}/products.json?limit=250&page=N

No API key, no login required. Each store's `handle` in STORES is its own
"all Riftbound singles, every set" collection -- naming isn't consistent
across stores ("riftbound-singles", "riftbound-all-singles",
"riftbound-single-in-stock", ...), so it's a per-store config value rather
than something derived. To add a new Shopify-based store: confirm
`{base_url}/products.json` responds, find its Riftbound-singles collection
handle, check its shipping policy page for a flat rate, and add a
StoreConfig entry below.

TITLE PARSING
-------------
Card name, finish, and collector-variant status are all encoded into the
product title, and the convention differs store to store:

    "Zed - Master of Shadows (Overnumbered) (191/166) - Rare"      (Card Merchant)
    "Forecaster [065/221] Common -FOIL"                            (Card Merchant)
    "Zhonya's Hourglass [OGN - 077/298]"                           (Iron Knight)
    "Renekton - Butcher of the Sands (Signature) (190*/166) - Vendetta Foil"  (Calico Keep)
    "Valley of Idols (218/219) (Unleashed)"                        (Card Masters)

The one constant is a card-number token -- "191/166", "065/221",
"OGN - 077/298", "190*/166" -- inside a single [] or () group, right after
the real card name. So: find that bracket group (by searching for a
digit/digit pattern anywhere inside a [...] or (...) -- not anchored to
the start, since Iron Knight prefixes it with a set code), cut the name
off there, then strip a standalone "FOIL" word (it can appear as bare text
before the number, not just as a suffix after it) to get is_foil. If what
remains still has a "(" in it, that's a collector-variant tag (e.g.
"(Overnumbered)", "(Signature)") -- excluded, same convention as the
Fetch TCG source, since those are pricier variants of the same card, not
what you want when you just typed the card's plain name.

Some stores also tag products with "Printing_Foil" / "Printing_Normal" --
when present, that's used as a more reliable override for is_foil than the
title regex.

FILTERING TO RIFTBOUND
------------------------
Every store's products, regardless of collection, carry a `product_type`
field containing "Riftbound" somewhere (e.g. "Riftbound Singles",
"Riftbound: League of Legends Trading Card Game Single") -- used as a
belt-and-suspenders filter in case a collection ever turns out to contain
stray non-Riftbound items.

STOCK
-----
Shopify's public product JSON only exposes `available: true/false`, not an
exact quantity -- so stock here is a flat guess (DEFAULT_STOCK) whenever a
variant is available. Could overstate availability for a rare card if a
store's true stock is 1-2; no way to tell from this endpoint.

SHIPPING
--------
Read by hand off each store's own /policies/shipping-policy page. Where a
store states a flat NZD rate for a normal (non-rural) parcel, that's used
directly (shipping_confirmed=True). Where a store instead uses Shopify's
dynamic, carrier-calculated shipping (no flat rate published -- common for
larger/heavier stores), DEFAULT_SHIPPING_ESTIMATE is used as a rough stand-
in and shipping_confirmed=False, so fetch() can flag which totals are real
vs. estimated. The user's address is assumed Auckland / North Island /
non-rural throughout -- re-check rates if that's not accurate for you.

Danireon Cards & Games (found in the same search results as these) was
checked and excluded: they only ship to Canada, the US, and the UK.
"""

import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from cart_optimiser import Listing, Seller
from . import util

NAME = "NZ Shopify Stores"

PAGE_LIMIT = 250
DEFAULT_STOCK = 20             # Shopify's public API doesn't expose an exact count
DEFAULT_SHIPPING_ESTIMATE = 7.00  # used only when a store's real rate isn't published


@dataclass
class StoreConfig:
    name: str
    base_url: str               # e.g. "https://cardmerchant.co.nz", no trailing slash
    collection: str              # Shopify collection handle for "all Riftbound singles"
    shipping: float = DEFAULT_SHIPPING_ESTIMATE
    shipping_confirmed: bool = True   # False => shipping is a rough guess, not this store's stated rate
    free_shipping_over: Optional[float] = None
    minimum_order: float = 0.0
    foil_ok: bool = True


STORES: List[StoreConfig] = [
    StoreConfig("CardMerchant", "https://cardmerchant.co.nz",
                "riftbound-singles", shipping=6.50),
    StoreConfig("CardMerchantTakapuna", "https://www.cardmerchanttakapuna.co.nz",
                "riftbound-singles", shipping=0.00),  # local pickup
    StoreConfig("CalicoKeep", "https://www.calicokeep.co.nz",
                "riftbound-single-in-stock", shipping=5.00),
    StoreConfig("GameRoost", "https://www.gameroost.co.nz",
                "riftbound-singles", shipping=8.00),  # Auckland-based; North Island non-rural rate
    StoreConfig("TCGCollectorNZ", "https://tcgcollectornz.com",
                "riftbound-all-singles", shipping=7.50,
                free_shipping_over=250.0, minimum_order=2.50),
    StoreConfig("CardMasters", "https://cardmasters.co.nz",
                "riftbound-league-of-legends-singles", shipping=7.00),  # "Courier Post (Singles Only)"
    StoreConfig("IronKnightGaming", "https://ironknightgaming.co.nz",
                "riftbound-singles-in-stock", shipping=7.49, minimum_order=4.99),
    StoreConfig("BeaGames", "https://www.beadndgames.co.nz",
                "riftbound-league-of-legends-singles", shipping=9.00),  # "Flat Rate Shipping Non Rural"
    StoreConfig("ShuffleAndCut", "https://www.shuffleandcutgames.co.nz",
                "riftbound", shipping=9.00),  # Auckland (Newmarket); no singles in stock currently
]

_CARD_NUMBER = re.compile(
    r"[\[\(][^\[\]\(\)]*\d{1,4}\*?[a-zA-Z]?\s*/\s*\d{1,4}\*?[a-zA-Z]?[^\[\]\(\)]*[\]\)]")
_FOIL_WORD = re.compile(r"(?i)\bfoil\b")
_TRAILING_BRACKET = re.compile(r"[\[\(][^\[\]\(\)]*[\]\)]\s*$")
_VARIANT_WORDS = re.compile(
    r"(?i)overnumbered|signature|alternate art|\baa\b|showcase|metal|prize wall|extended art|full art")


def _parse_title(title: str) -> Tuple[str, bool, bool]:
    """-> (card_name, is_foil, is_collector_variant)

    Most stores put a card-number token ("191/166", "OGN - 077/298", ...)
    in a single [] or () group right after the name -- _CARD_NUMBER finds
    that and everything from there on is discarded. But at least one store
    (Card Merchant Takapuna) skips numbers entirely and just appends a
    trailing "[SetName]" tag instead, e.g. "Arise! [Spiritforged]" -- so
    after the number-based cut (or if there was none to make), any
    trailing bracket group(s) still left at the END of the string are
    stripped too, repeatedly, since a real card name never itself ends in
    a bracket. Each stripped group is checked against known
    collector-variant wording (Overnumbered, Signature, Alternate Art,
    ...) to decide is_variant -- a trailing "[Spiritforged]" set tag
    doesn't match that and is treated as ordinary metadata, not a variant.
    """
    m = _CARD_NUMBER.search(title)
    name_part = title[:m.start()] if m else title
    is_foil = bool(_FOIL_WORD.search(title))
    name_part = _FOIL_WORD.sub("", name_part)

    is_variant = False
    while True:
        m2 = _TRAILING_BRACKET.search(name_part)
        if not m2:
            break
        if _VARIANT_WORDS.search(m2.group()):
            is_variant = True
        name_part = name_part[:m2.start()]

    return re.sub(r"\s+", " ", name_part).strip(), is_foil, is_variant


def _fetch_collection(base_url: str, collection: str) -> Tuple[List[dict], bool]:
    """-> (products, complete). `complete` is False if pagination had to be
    abandoned partway through due to persistent rate-limiting -- distinct
    from a clean end-of-catalog, since it means this store's results here
    are an undercount, not "that's everything they stock"."""
    products: List[dict] = []
    page = 1
    while True:
        url = (f"{base_url}/collections/{collection}/products.json"
               f"?limit={PAGE_LIMIT}&page={page}")
        try:
            data = util.get_json(url)
        except util.RateLimited:
            return products, False
        time.sleep(util.REQUEST_DELAY)
        if not data:
            break
        batch = data.get("products", [])
        if not batch:
            break
        products.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        page += 1
    return products, True


def _seller_for(store: StoreConfig) -> Seller:
    return Seller(name=store.name, shipping=store.shipping,
                  free_shipping_over=store.free_shipping_over,
                  minimum_order=store.minimum_order,
                  url=f"{store.base_url}/collections/{store.collection}")


def _fetch_one_store(store: StoreConfig, want_by_norm: Dict[str, str]) -> Tuple[List[Listing], Optional[Seller]]:
    print(f"  [{store.name}] pulling catalog...")
    products, complete = _fetch_collection(store.base_url, store.collection)
    if not complete:
        util.warn(f"[{store.name}] RATE LIMITED partway through pulling their catalog -- "
                 f"only {len(products)} product(s) fetched before giving up, so cards "
                 f"this store DOES stock may be missing from this run entirely. "
                 f"Re-run before trusting an absence from this store.")
    riftbound = [p for p in products
                if "riftbound" in (p.get("product_type") or "").lower()]
    print(f"  [{store.name}] {len(products)} products fetched "
          f"({len(riftbound)} tagged Riftbound)")

    listings: List[Listing] = []
    for p in riftbound:
        name, is_foil, is_variant = _parse_title(p.get("title", ""))
        tags_lower = " ".join(p.get("tags", [])).lower()
        if "printing_foil" in tags_lower:
            is_foil = True
        elif "printing_normal" in tags_lower:
            is_foil = False
        if is_variant or (is_foil and not store.foil_ok):
            continue
        card = want_by_norm.get(util.normalize_name(name))
        if not card:
            continue
        for v in p.get("variants", []):
            if not v.get("available"):
                continue
            try:
                price = float(v["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 0:
                continue
            listings.append(Listing(card=card, seller=store.name,
                                    price=round(price, 2), stock=DEFAULT_STOCK))

    if not listings:
        return [], None
    return listings, _seller_for(store)


def fetch(want: Dict[str, int]) -> Tuple[List[Listing], List[Seller]]:
    want_by_norm = {util.normalize_name(w): w for w in want}
    all_listings: List[Listing] = []
    all_sellers: List[Seller] = []
    estimated: List[str] = []

    for store in STORES:
        listings, seller = _fetch_one_store(store, want_by_norm)
        if listings and seller:
            all_listings.extend(listings)
            all_sellers.append(seller)
            if not store.shipping_confirmed:
                estimated.append(store.name)

    if estimated:
        print(f"\n  [{NAME}] shipping is an ESTIMATE (${DEFAULT_SHIPPING_ESTIMATE:.2f}, "
              f"not a confirmed flat rate) for: {', '.join(estimated)}")

    return all_listings, all_sellers


# ---------------------------------------------------------------------------
# Preconstructed champion decks (see precons.py)
# ---------------------------------------------------------------------------
#
# Sealed decks aren't in any store's singles collection, so they're found
# via Shopify's public predictive-search endpoint instead:
#
#     GET {store}/search/suggest.json?q=<set> champion deck&resources[type]=product
#
# then each hit's /products/{handle}.js for its variants -- several stores
# sell one "Origins Champion Decks" product with a Jinx/Lee Sin/Viktor
# variant each, rather than one product per deck.

PRECON_STOCK_GUESS = 5  # .js only says available or not, same limitation as singles
_NOT_A_DECK = re.compile(r"(?i)custom|sleeve|playmat|display|case|booster|bundle|chinese|japanese|korean")


def _match_precon(product_title: str, variant_title: str, all_precons: list):
    """The one precon this product/variant is, or None if ambiguous/unknown.
    Champion is read from the variant title first (e.g. "Vi - Unleashed
    Champion Deck" under a Vi/Vex product), falling back to the product
    title for single-deck products ("Default Title" variant). A set name
    in the text must agree with the precon's, if one is present."""
    text = f"{product_title} {variant_title}".lower()

    def champs_in(s: str):
        return [p for p in all_precons
                if re.search(rf"\b{re.escape(p.champion.lower())}\b", s.lower())]

    hits = champs_in(variant_title) if variant_title.lower() != "default title" else []
    if not hits:
        hits = champs_in(product_title)
    named_sets = {p.set_name.lower() for p in all_precons if p.set_name.lower() in text}
    hits = [p for p in hits if not named_sets or p.set_name.lower() in named_sets]
    return hits[0] if len(hits) == 1 else None


def fetch_precon_offers(wanted: list, all_precons: list) -> Tuple[List[Listing], List[Seller]]:
    """Every store's in-stock price for each deck in `wanted` (precons.Precon
    objects), as Listings of that deck's pseudo-card, plus the Seller for
    any store that has one -- so a deck and singles from the same store
    share its shipping. `all_precons` is used only to rule out ambiguous
    matches (passed in rather than imported, to keep sources/ free of a
    dependency on the top-level precons module)."""
    wanted_cards = {p.pseudo_card for p in wanted}
    sets = sorted({p.set_name for p in wanted})
    all_listings: List[Listing] = []
    all_sellers: List[Seller] = []

    for store in STORES:
        print(f"  [{store.name}] looking for champion decks...")
        found: List[Listing] = []
        seen_handles = set()
        for set_name in sets:
            q = urllib.parse.quote(f"{set_name} champion deck")
            try:
                data = util.get_json(f"{store.base_url}/search/suggest.json?q={q}"
                                     f"&resources[type]=product&resources[limit]=10")
            except util.RateLimited:
                util.warn(f"[{store.name}] RATE LIMITED searching for {set_name} champion "
                          f"decks -- that store's deck prices are missing from this run.")
                continue
            time.sleep(util.REQUEST_DELAY)
            products = ((data or {}).get("resources", {}).get("results", {}).get("products", []))
            for prod in products:
                title = prod.get("title") or ""
                handle = prod.get("handle")
                if (not handle or handle in seen_handles or "champion deck" not in title.lower()
                        or _NOT_A_DECK.search(title)):
                    continue
                seen_handles.add(handle)
                try:
                    detail = util.get_json(f"{store.base_url}/products/{handle}.js")
                except util.RateLimited:
                    continue
                time.sleep(util.REQUEST_DELAY)
                for v in (detail or {}).get("variants", []):
                    if not v.get("available"):
                        continue
                    precon = _match_precon(title, v.get("title") or "", all_precons)
                    if precon is None or precon.pseudo_card not in wanted_cards:
                        continue
                    try:
                        price = float(v["price"]) / 100  # .js prices are integer cents
                    except (KeyError, TypeError, ValueError):
                        continue
                    if price > 0:
                        found.append(Listing(card=precon.pseudo_card, seller=store.name,
                                             price=round(price, 2), stock=PRECON_STOCK_GUESS,
                                             url=f"{store.base_url}/products/{handle}"))
        if found:
            all_listings.extend(found)
            all_sellers.append(_seller_for(store))
    return all_listings, all_sellers
