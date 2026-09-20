#!/usr/bin/env python3
"""
End-to-end: fetch Riftbound singles listings for a decklist from every
enabled source, then compute the cheapest multi-seller split. One command,
no manual data entry, no browser.

    python3 fetch_and_optimise.py --decklist examples/azir_emperor_of_the_sands.txt

MULTI-SOURCE DESIGN
--------------------
Each site lives in its own module under sources/ (see sources/__init__.py
for the interface both implement):

  * sources/fetchtcg.py   -- fetchtcg.com, a many-sellers marketplace.
                              Real per-listing prices AND real per-seller
                              shipping, both from public JSON APIs.
  * sources/nz_shopify.py -- a growing list of single-store NZ/Auckland
                              TCG retailers (Card Merchant, Card Masters,
                              Calico Keep, Game Roost, TCG Collector NZ,
                              Iron Knight Gaming, Bea Games, Shuffle n
                              Cut), each its own Seller with its own
                              inventory/shipping -- see that module's
                              docstring for how to add another store to
                              its STORES list.

This file just calls `fetch(want)` on each entry in ENABLED_SOURCES,
concatenates the Listings and Sellers it gets back, and runs the same
cart_optimiser over the combined pool -- the optimiser has no idea (or
need to know) which site any given listing came from. To add an entirely
new *kind* of source (not a Shopify store): write a new sources/<site>.py
with a `fetch(want) -> (listings, sellers)` function, import it in
sources/__init__.py, and add it to ALL_SOURCES there.

Seller names are assumed unique ACROSS sources (Fetch TCG usernames vs. a
store's own name, e.g. "CardMerchant") -- if two sources ever returned the
same seller name for genuinely different entities, their listings would
incorrectly get pooled as one "seller" by the optimiser. Not a real risk
with the sources here, but worth knowing if you add a site whose seller
names could collide.

DECKLISTS
---------
One of these is required:

    --decklist path/to/exported.txt   # a riftdecks.com "Export TXT" file
    --decklist-url https://riftdecks.com/decks/<slug>   # best-effort direct
                                                          # fetch, may be
                                                          # blocked by
                                                          # Cloudflare -- see
                                                          # decklist.py
    --player NAME:FILE  (repeated)    # group buy -- see below

See examples/azir_emperor_of_the_sands.txt for a sample, and decklist.py
for the exact .txt format and how section quantities are summed when a
card appears in more than one section (e.g. maindeck AND sideboard).

USAGE
-----
    python3 fetch_and_optimise.py --decklist my_deck.txt
    python3 fetch_and_optimise.py --offline listings_cache.json   # re-parse
    python3 fetch_and_optimise.py --decklist my_deck.txt --max-sellers 5   # ALSO compare a 5-seller cap

    # Group buy: pool N people's decks into one optimised purchase (more
    # combined volume = cheaper), then split cost + shipping back out
    # fairly per person -- see group_buy.py for how the split works.
    python3 fetch_and_optimise.py --player Alice:alice.txt --player Bob:bob.txt --player You:my_deck.txt

The main answer -- CHEAPEST OVERALL -- is picked automatically from the
cost-vs-orders sweep (see cart_optimiser.best_from_sweep): no need to guess
a seller cap yourself. --max-sellers only adds a side-by-side comparison
against a specific cap, if you want one.

Be polite: this is personal use. There's a small delay between requests
within each source (see sources/util.py).
"""

import argparse
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from cart_optimiser import (Listing, Seller, optimise, naive_cheapest_per_card,
                            report, sweep, report_sweep, best_from_sweep)
from sources import ALL_SOURCES
from sources import util as sources_util
import decklist
import group_buy

# Every source in sources/ALL_SOURCES is on by default -- comment one out
# to disable it (e.g. if a site is down or you don't want it included).
ENABLED_SOURCES = list(ALL_SOURCES)


# ---------------------------------------------------------------------------

def fetch_everything(want: Dict[str, int]) -> tuple:
    listings: List[Listing] = []
    sellers: List[Seller] = []
    for source in ENABLED_SOURCES:
        print(f"\n--- {source.NAME} ---")
        src_listings, src_sellers = source.fetch(want)
        print(f"  [{source.NAME}] {len(src_listings)} listings, "
              f"{len(src_sellers)} seller(s)")
        listings.extend(src_listings)
        sellers.extend(src_sellers)
    return listings, sellers


CACHE_TTL = 3600  # seconds a card's cached listings are trusted before re-fetching


def _dump(path: str, listings: List[Listing], sellers: List[Seller],
          fetched_at: Dict[str, float]) -> None:
    payload = {
        "listings": [l.__dict__ for l in listings],
        "sellers": [
            {"name": s.name, "shipping": s.shipping,
             "free_shipping_over": s.free_shipping_over,
             "minimum_order": s.minimum_order,
             "shipping_tiers": s.shipping_tiers,
             "url": s.url}
            for s in sellers
        ],
        "fetched_at": fetched_at,
    }
    Path(path).write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {path}")


def _load_offline(path: str) -> tuple:
    raw = json.loads(Path(path).read_text())
    listings = [Listing(**l) for l in raw["listings"]]
    sellers = []
    for s in raw["sellers"]:
        tiers = s.get("shipping_tiers")
        if tiers:
            tiers = [tuple(t) for t in tiers]
        sellers.append(Seller(name=s["name"], shipping=s["shipping"],
                              free_shipping_over=s.get("free_shipping_over"),
                              minimum_order=s.get("minimum_order", 0.0),
                              shipping_tiers=tiers,
                              url=s.get("url")))
    return listings, sellers, raw.get("fetched_at", {})


def merge_cache(cached_listings: List[Listing], cached_sellers: List[Seller],
                fetched_at: Dict[str, float], stale_cards: set,
                fresh_listings: List[Listing], fresh_sellers: List[Seller],
                now: float) -> tuple:
    """Drop cached listings for cards we just re-fetched, add the fresh
    ones, and union sellers (by name, fresh data wins). Pure function so
    it's testable without hitting the network -- see test_cache.py."""
    listings = [l for l in cached_listings if l.card not in stale_cards] + fresh_listings
    seller_map = {s.name: s for s in cached_sellers}
    seller_map.update({s.name: s for s in fresh_sellers})
    fetched_at = dict(fetched_at)
    fetched_at.update({card: now for card in stale_cards})
    return listings, list(seller_map.values()), fetched_at


def _resolve_want(args) -> Dict[str, int]:
    if args.decklist and args.decklist_url:
        raise SystemExit("pass only one of --decklist / --decklist-url")

    if args.decklist:
        want = decklist.parse_decklist_file(args.decklist)
        print(f"loaded decklist from {args.decklist}: "
              f"{len(want)} unique cards, {sum(want.values())} total copies")
        return want

    if args.decklist_url:
        try:
            want = decklist.fetch_from_url(args.decklist_url)
        except RuntimeError as e:
            raise SystemExit(f"error: {e}")
        print(f"loaded decklist from {args.decklist_url}: "
              f"{len(want)} unique cards, {sum(want.values())} total copies")
        return want

    raise SystemExit("no decklist given -- pass --decklist FILE, --decklist-url URL, "
                     "or --player NAME:FILE (repeated, for a group buy). "
                     "See examples/azir_emperor_of_the_sands.txt for the expected format.")


def _resolve_players(args) -> Optional[Dict[str, Dict[str, int]]]:
    """--player NAME:FILE, repeated once per person in a group buy. Returns
    None (not group-buy mode) if --player wasn't used at all."""
    if not args.player:
        return None
    if args.decklist or args.decklist_url:
        raise SystemExit("--player can't be combined with --decklist/--decklist-url "
                          "-- give every player their own --player NAME:FILE instead")

    players: Dict[str, Dict[str, int]] = {}
    for spec in args.player:
        if ":" not in spec:
            raise SystemExit(f"--player expects NAME:FILE, got {spec!r}")
        name, path = spec.split(":", 1)
        name = name.strip()
        if not name:
            raise SystemExit(f"--player {spec!r} has no name before the ':'")
        if name in players:
            raise SystemExit(f"duplicate player name: {name!r}")
        try:
            players[name] = decklist.parse_decklist_file(path)
        except FileNotFoundError:
            raise SystemExit(f"--player {name}: no such file {path!r}")
        print(f"loaded {name}'s decklist from {path}: "
              f"{len(players[name])} unique cards, {sum(players[name].values())} total copies")
    return players


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decklist", metavar="FILE",
                    help="path to a riftdecks.com-exported decklist .txt "
                         "(overrides the built-in default decklist)")
    ap.add_argument("--decklist-url", metavar="URL",
                    help="a riftdecks.com deck page URL to fetch directly -- "
                         "best-effort, may be blocked by Cloudflare; use "
                         "--decklist with a manually-exported .txt if so")
    ap.add_argument("--player", metavar="NAME:FILE", action="append",
                    help="add a person to a GROUP BUY -- repeat once per "
                         "person, e.g. --player Alice:alice.txt --player "
                         "Bob:bob.txt. Everyone's cards get pooled into one "
                         "optimised purchase (more combined volume = "
                         "cheaper), then costs and shipping are split back "
                         "out fairly per person at the end. Can't be "
                         "combined with --decklist/--decklist-url.")
    ap.add_argument("--dump", metavar="FILE", default="listings_cache.json",
                    help="save fetched listings here (default: listings_cache.json)")
    ap.add_argument("--max-sellers", type=int, metavar="N",
                    help="ALSO show the best plan if capped at exactly N "
                         "sellers, for comparison against the auto-picked "
                         "cheapest-overall plan (which isn't capped)")
    ap.add_argument("--offline", metavar="FILE",
                    help="skip fetching, re-parse a previous --dump file")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the cache, re-fetch every card")
    ap.add_argument("--no-sweep", action="store_true",
                    help="skip printing the cost-vs-orders tradeoff table "
                         "(the cheapest-overall plan is still found from it)")
    ap.add_argument("--sweep-to", type=int, default=15,
                    help="max seller count to search up to (default 15) -- "
                         "raise this if the cheapest plan might use more "
                         "sellers than that (the sweep table, or a missing "
                         "'cheapest' marker at the last row, will tell you)")
    args = ap.parse_args()

    players = _resolve_players(args)
    want = None if players else _resolve_want(args)

    run(want=want, players=players, offline=args.offline, dump=args.dump,
        max_sellers=args.max_sellers, sweep_to=args.sweep_to, no_sweep=args.no_sweep,
        refresh=args.refresh)


def run(want: Optional[Dict[str, int]] = None,
        players: Optional[Dict[str, Dict[str, int]]] = None,
        offline: Optional[str] = None,
        dump: str = "listings_cache.json",
        max_sellers: Optional[int] = None,
        sweep_to: int = 15,
        no_sweep: bool = False,
        refresh: bool = False) -> None:
    """The actual pipeline, factored out of main() so a GUI (see ui.py) or
    any other caller can drive it directly with plain Python values instead
    of going through argparse. Pass exactly one of `want` (a single
    card->qty dict) or `players` (name -> their own card->qty dict, for a
    group buy). Everything prints as it goes, same as the CLI.

    Cards fetched within CACHE_TTL of `dump` are reused instead of
    re-fetched -- prices don't move fast enough within a session to justify
    re-hitting every source on every run, and it means adding/removing a
    couple of cards from a decklist only fetches those cards, not the whole
    thing. --offline skips the network entirely regardless of age; --refresh
    ignores the cache and treats everything as stale."""
    if not want and not players:
        raise ValueError("run() needs either `want` or `players`")
    sources_util.WARNINGS.clear()  # drop anything left over from a previous run() call

    if players:
        resolved_want = group_buy.pool_players(players)
        print(f"\npooled {len(players)} players: {len(resolved_want)} unique cards, "
              f"{sum(resolved_want.values())} total copies needed")
    else:
        resolved_want = want

    if offline:
        listings, sellers, _ = _load_offline(offline)
    else:
        cached_listings, cached_sellers, fetched_at = [], [], {}
        if Path(dump).exists() and not refresh:
            cached_listings, cached_sellers, fetched_at = _load_offline(dump)

        now = time.time()
        stale = {card for card in resolved_want
                 if now - fetched_at.get(card, 0) > CACHE_TTL}
        if stale:
            fresh_want = {card: resolved_want[card] for card in stale}
            print(f"\n{len(resolved_want) - len(stale)} of {len(resolved_want)} "
                  f"card(s) still fresh (cached within {CACHE_TTL // 60} min) -- "
                  f"fetching {len(stale)}...")
            fresh_listings, fresh_sellers = fetch_everything(fresh_want)
            listings, sellers, fetched_at = merge_cache(
                cached_listings, cached_sellers, fetched_at, stale,
                fresh_listings, fresh_sellers, now)
            _dump(dump, listings, sellers, fetched_at)
        else:
            print(f"\nall {len(resolved_want)} card(s) already cached within "
                  f"{CACHE_TTL // 60} min -- skipping fetch (--refresh to force)")
            listings, sellers = cached_listings, cached_sellers

    print(f"\nfetched {len(listings)} active listings across "
          f"{len(sellers)} seller(s)/store(s)")

    if not listings:
        return

    seller_map = {s.name: s for s in sellers}

    report(naive_cheapest_per_card(resolved_want, listings, sellers),
           "NAIVE: cheapest listing for every card", seller_map)

    results = sweep(resolved_want, listings, sellers, max_k=sweep_to)
    best_k, best_plan = best_from_sweep(results)
    if best_plan is None:
        print("\nno feasible plan found at any seller count")
    else:
        n = len(best_plan.orders)
        report(best_plan, f"CHEAPEST OVERALL ({n} parcel{'s' if n != 1 else ''})",
               seller_map)

    if not no_sweep:
        report_sweep(results)
        if best_k == sweep_to:
            print(f"\n  note: the cheapest plan uses every seller count up "
                  f"to --sweep-to ({sweep_to}) -- rerun with a higher "
                  f"--sweep-to to check whether more sellers helps further.")

    if max_sellers and max_sellers != best_k:
        capped = optimise(resolved_want, listings, sellers,
                          max_sellers=max_sellers, allow_unfilled=True)
        report(capped, f"IF CAPPED AT {max_sellers} sellers", seller_map)

    if players and best_plan is not None:
        bills = group_buy.attribute_costs(best_plan, players, seller_map)
        group_buy.report_bills(bills)

    if sources_util.WARNINGS:
        print(f"\n{'!' * 62}\nDATA QUALITY WARNINGS -- prices above may be INCOMPLETE\n{'!' * 62}")
        for w in sources_util.WARNINGS:
            print(f"  - {w}")
        print(f"\n  These are cards/sellers this run could NOT actually check (usually "
              f"rate-limiting), not confirmed absences -- treat the totals above as a "
              f"lower bound until you re-run and this section is empty.")
        sources_util.WARNINGS.clear()


if __name__ == "__main__":
    main()
