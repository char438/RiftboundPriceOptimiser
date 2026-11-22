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

LOCAL PICKUP
------------
Some fetchtcg.com sellers turn out to be Auckland-based with a genuine $0
pickup option (see sources/fetchtcg.py) -- used automatically wherever it
saves money. Tune or disable this:

    --no-pickup                    # never use pickup, always pay courier
    --pickup-only Alice,Bob         # only allow pickup from named sellers
    --pickup-exclude Carl           # allow pickup from everyone except Carl
    --max-pickups 2                 # at most 2 meetups; the rest ship

CHAMPION DECKS
--------------
If a want-list overlaps a preconstructed champion deck enough, buying the
whole deck from a store can beat buying those cards as singles (mostly by
saving a shipping fee). This is checked automatically -- see precons.py.
--no-precons turns it off (and skips fetching deck prices).

Be polite: this is personal use. There's a small delay between requests
within each source (see sources/util.py).
"""

import argparse
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from cart_optimiser import (Listing, Seller, Plan, PickupPolicy, optimise, naive_cheapest_per_card,
                            report, sweep, report_sweep, best_from_sweep)
from sources import ALL_SOURCES, nz_shopify
from sources import util as sources_util
import decklist
import group_buy
import precons

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
             "url": s.url,
             "pickup_cost": s.pickup_cost,
             "pickup_note": s.pickup_note}
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
                              url=s.get("url"),
                              pickup_cost=s.get("pickup_cost"),
                              pickup_note=s.get("pickup_note")))
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
    ap.add_argument("--no-pickup", action="store_true",
                    help="never use a seller's free local pickup, even when "
                         "one is available -- always pay for courier instead")
    ap.add_argument("--pickup-only", metavar="NAME,NAME,...",
                    help="only allow pickup from these specific sellers "
                         "(comma-separated), even if others are eligible")
    ap.add_argument("--pickup-exclude", metavar="NAME,NAME,...",
                    help="allow pickup from every eligible seller EXCEPT "
                         "these (comma-separated)")
    ap.add_argument("--no-precons", action="store_true",
                    help="don't consider buying whole preconstructed champion "
                         "decks instead of some singles (see precons.py)")
    ap.add_argument("--max-pickups", type=int, metavar="N",
                    help="cap how many sellers in the chosen plan may use "
                         "pickup -- the sellers where it saves the most win "
                         "the slots, the rest ship by courier as normal")
    args = ap.parse_args()

    if args.pickup_only and args.pickup_exclude:
        raise SystemExit("pass only one of --pickup-only / --pickup-exclude")

    pickup = PickupPolicy(
        enabled=not args.no_pickup,
        only_sellers=frozenset(s.strip() for s in args.pickup_only.split(","))
                     if args.pickup_only else None,
        exclude_sellers=frozenset(s.strip() for s in args.pickup_exclude.split(","))
                        if args.pickup_exclude else frozenset(),
        max_pickups=args.max_pickups,
    )

    players = _resolve_players(args)
    want = None if players else _resolve_want(args)

    run(want=want, players=players, offline=args.offline, dump=args.dump,
        max_sellers=args.max_sellers, sweep_to=args.sweep_to, no_sweep=args.no_sweep,
        refresh=args.refresh, pickup=pickup, use_precons=not args.no_precons)


def load_listings(resolved_want: Dict[str, int],
                  offline: Optional[str] = None,
                  dump: str = "listings_cache.json",
                  refresh: bool = False,
                  fetch_precons: bool = True) -> tuple:
    """The network/cache half of run(), split out so a caller that wants to
    re-solve the SAME listings under a different PickupPolicy (or a
    different --max-sellers) can do that without hitting the network or
    even re-reading the cache file again -- see ui.py's "Re-optimise"
    button, which just calls solve_and_report() again on whatever this
    returned last time.

    Cards fetched within CACHE_TTL of `dump` are reused instead of
    re-fetched -- prices don't move fast enough within a session to justify
    re-hitting every source on every run, and it means adding/removing a
    couple of cards from a decklist only fetches those cards, not the whole
    thing. `offline` skips the network entirely regardless of age;
    `refresh` ignores the cache and treats everything as stale.

    -> (listings, sellers)."""
    if offline:
        listings, sellers, _ = _load_offline(offline)
        return listings, sellers

    cached_listings, cached_sellers, fetched_at = [], [], {}
    if Path(dump).exists() and not refresh:
        cached_listings, cached_sellers, fetched_at = _load_offline(dump)

    # Deck prices are cached exactly like a card's listings, keyed by the
    # deck's pseudo-card name -- see precons.py.
    relevant_decks = precons.relevant_precons(resolved_want) if fetch_precons else []
    tracked = list(resolved_want) + [p.pseudo_card for p in relevant_decks]

    now = time.time()
    stale = {card for card in tracked if now - fetched_at.get(card, 0) > CACHE_TTL}
    if stale:
        stale_cards = {c for c in stale if not precons.is_pseudo(c)}
        stale_decks = [p for p in relevant_decks if p.pseudo_card in stale]
        print(f"\n{len(resolved_want) - len(stale_cards)} of {len(resolved_want)} "
              f"card(s) still fresh (cached within {CACHE_TTL // 60} min) -- "
              f"fetching {len(stale_cards)}...")
        fresh_listings, fresh_sellers = [], []
        if stale_cards:
            fresh_listings, fresh_sellers = fetch_everything(
                {card: resolved_want[card] for card in stale_cards})
        if stale_decks:
            print(f"\n--- Champion deck prices ({len(stale_decks)} relevant deck(s)) ---")
            deck_listings, deck_sellers = nz_shopify.fetch_precon_offers(stale_decks, precons.PRECONS)
            fresh_listings += deck_listings
            fresh_sellers += deck_sellers
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
    return listings, sellers


def solve_and_report(resolved_want: Dict[str, int],
                     players: Optional[Dict[str, Dict[str, int]]],
                     listings: List[Listing],
                     sellers: List[Seller],
                     max_sellers: Optional[int] = None,
                     sweep_to: int = 15,
                     no_sweep: bool = False,
                     pickup: PickupPolicy = PickupPolicy(),
                     use_precons: bool = True) -> Optional[Plan]:
    """The optimise/report half of run() -- pure computation over an
    already-fetched (listings, sellers), no network or disk I/O at all.
    Safe (and fast, typically well under a second) to call repeatedly with
    just `pickup` or `max_sellers` changed, e.g. to let a GUI apply a new
    shipping/pickup toggle instantly. Returns the winning Plan (or None) so
    a caller can inspect it without re-parsing the printed report.

    `pickup` controls whether/how a plan may use a seller's free local
    pickup instead of courier -- see cart_optimiser.PickupPolicy. Defaults
    to "use it wherever available".

    `use_precons` also checks whether buying whole champion decks beats
    singles (see precons.py) -- only possible if load_listings() fetched
    deck prices. That check adds up to a few seconds per deck worth trying.

    Does NOT clear sources_util.WARNINGS on entry (unlike the old combined
    run()) -- those are populated during fetching, in load_listings(), so
    clearing here would wipe out real warnings from THAT call before this
    one gets a chance to print them. Whoever calls load_listings() is
    responsible for clearing WARNINGS first; see run() and ui.py."""
    if not listings:
        return None

    seller_map = {s.name: s for s in sellers}

    report(naive_cheapest_per_card(resolved_want, listings, sellers, pickup=pickup),
           "NAIVE: cheapest listing for every card", seller_map)

    results = sweep(resolved_want, listings, sellers, max_k=sweep_to, pickup=pickup)
    best_k, singles_plan = best_from_sweep(results)

    deck_plan = None
    if use_precons:
        deck_plan, combos = precons.improve_with_precons(
            resolved_want, listings, sellers, singles_plan, max_sellers=sweep_to, pickup=pickup)
        precons.report_combos(combos, singles_plan)
    best_plan = deck_plan or singles_plan

    if best_plan is None:
        print("\nno feasible plan found at any seller count")
    else:
        n = len(best_plan.orders)
        label = f"CHEAPEST OVERALL ({n} parcel{'s' if n != 1 else ''})"
        if deck_plan is not None:
            label += f" -- saves ${singles_plan.total - deck_plan.total:.2f} by buying a deck" \
                if singles_plan else " -- includes a champion deck"
        report(best_plan, label, seller_map)

    if not no_sweep:
        report_sweep(results)
        if deck_plan is not None:
            print("  (table above is singles-only -- the cheapest overall plan uses a deck)")
        if best_k == sweep_to:
            print(f"\n  note: the cheapest plan uses every seller count up "
                  f"to --sweep-to ({sweep_to}) -- rerun with a higher "
                  f"--sweep-to to check whether more sellers helps further.")

    if max_sellers and max_sellers != best_k:
        capped = optimise(resolved_want, listings, sellers,
                          max_sellers=max_sellers, allow_unfilled=True, pickup=pickup)
        report(capped, f"IF CAPPED AT {max_sellers} sellers", seller_map)

    if players and best_plan is not None:
        # Nobody asked for "[PRECON] ..." itself -- bill the real cards it covers.
        billable = precons.expand_bundles(best_plan, listings)
        bills = group_buy.attribute_costs(billable, players, seller_map)
        solo_costs = group_buy.solo_plan_costs(players, listings, sellers,
                                               sweep_to=sweep_to, pickup=pickup)
        fairness_applied = group_buy.apply_individual_rationality(bills, solo_costs)
        group_buy.sanity_check(billable, bills)
        group_buy.report_bills(bills, fairness_applied=fairness_applied)
        if not fairness_applied:
            print(f"\n  ! Could not guarantee nobody pays more than solo -- pooling "
                  f"this exact group came out MORE expensive in total than everyone's "
                  f"solo prices summed (unusual; often means stock contention on a "
                  f"scarce card). Bills above are the plain proportional split, not "
                  f"fairness-capped.")

    if sources_util.WARNINGS:
        print(f"\n{'!' * 62}\nDATA QUALITY WARNINGS -- prices above may be INCOMPLETE\n{'!' * 62}")
        for w in sources_util.WARNINGS:
            print(f"  - {w}")
        print(f"\n  These are cards/sellers this run could NOT actually check (usually "
              f"rate-limiting), not confirmed absences -- treat the totals above as a "
              f"lower bound until you re-run and this section is empty.")
        sources_util.WARNINGS.clear()

    return best_plan


def run(want: Optional[Dict[str, int]] = None,
        players: Optional[Dict[str, Dict[str, int]]] = None,
        offline: Optional[str] = None,
        dump: str = "listings_cache.json",
        max_sellers: Optional[int] = None,
        sweep_to: int = 15,
        no_sweep: bool = False,
        refresh: bool = False,
        pickup: PickupPolicy = PickupPolicy(),
        use_precons: bool = True) -> None:
    """The actual pipeline, factored out of main() so a GUI (see ui.py) or
    any other caller can drive it directly with plain Python values instead
    of going through argparse. Pass exactly one of `want` (a single
    card->qty dict) or `players` (name -> their own card->qty dict, for a
    group buy). Everything prints as it goes, same as the CLI.

    Just load_listings() + solve_and_report() back to back -- call those
    two separately instead if you want to change `pickup` or `max_sellers`
    afterwards without re-fetching (see their docstrings)."""
    if not want and not players:
        raise ValueError("run() needs either `want` or `players`")
    sources_util.WARNINGS.clear()  # drop anything left over from a previous call

    if players:
        resolved_want = group_buy.pool_players(players)
        print(f"\npooled {len(players)} players: {len(resolved_want)} unique cards, "
              f"{sum(resolved_want.values())} total copies needed")
    else:
        resolved_want = want

    listings, sellers = load_listings(resolved_want, offline=offline, dump=dump, refresh=refresh,
                                      fetch_precons=use_precons)
    solve_and_report(resolved_want, players, listings, sellers,
                     max_sellers=max_sellers, sweep_to=sweep_to,
                     no_sweep=no_sweep, pickup=pickup, use_precons=use_precons)


if __name__ == "__main__":
    main()
