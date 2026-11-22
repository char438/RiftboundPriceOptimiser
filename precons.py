#!/usr/bin/env python3
"""
Preconstructed champion decks as an alternative to buying singles.

WHY
---
A champion deck is 56 fixed cards for ~NZ$25-50. If a want-list needs a
lot of the commons/runes that happen to be in one, buying the whole deck
can beat buying those cards as singles -- not usually on card price alone
(the contents are only worth ~US$9-25 at market), but because every extra
singles seller costs another shipping fee, and a deck from a store you're
already ordering from adds none.

DATA
----
PRECONS below is hardcoded: each deck's contents never change once
printed. The lists come from Piltover Archive's official "System" precon
entries (piltoverarchive.com, author "Piltover Archive (System)"), each
exactly 56 cards (legend + champion + main deck + 3 battlefields + 12
runes). The booster pack, playmat and deckbox that also come in the box
are ignored -- random/non-card value, not something a want-list needs.

Deliberately NOT included:
  * Proving Grounds (Annie/Garen/Lux/Master Yi) -- sold as one 4-deck box
    (~NZ$100-150) and no verified card list was available.
  * Showdown decks (Zed vs Shen, Evelynn vs Seraphine) -- same reason.
  * Pre-Rift kits -- prerelease kits built around random boosters, so
    there is no fixed card list to price against.
Adding one later is just another Precon(...) entry below, as long as its
card list is exact.

Prices are NOT hardcoded -- they're fetched live from the same NZ stores
we already buy singles from (see sources/nz_shopify.fetch_precon_offers),
as ordinary Listings of a pseudo-card named "[PRECON] <set> Champion Deck:
<champion>". Modelling a deck as a listing of a pseudo-card is what lets
the existing optimiser handle it without special cases: it picks which
store to buy it from, and a deck from a store you're already buying singles
from shares that store's one shipping fee.

HOW THE SEARCH WORKS
--------------------
For each candidate "combo" of decks (every single relevant deck, then
every pair, repeats allowed so 2x the same deck can cover a playset):
  1. subtract the deck contents from the want-list (clamped at 0 -- cards
     you don't need are just extras),
  2. add one pseudo-card demand per deck in the combo,
  3. run the normal optimiser on that and compare to the singles-only plan.
Step 3 is the expensive part (~a second each), so combos are lower-bounded
first: every remaining card at its cheapest listed price, zero shipping,
plus each deck at its cheapest offer. If even that can't beat the best plan
found so far, the combo is skipped without optimising it. That bound is
safe (no real plan can be cheaper), so pruning never throws away a winner.
Unfilled cards are compared first, same as everywhere else in the
optimiser (see cart_optimiser._plan_key).
"""

from dataclasses import dataclass, replace
from itertools import combinations_with_replacement
from typing import Dict, List, Optional, Tuple

from cart_optimiser import Listing, PickupPolicy, Plan, Seller, _plan_key, optimise
from sources.util import normalize_name

PSEUDO_PREFIX = "[PRECON] "
MAX_PRECON_COPIES = 2   # decks per combo -- 2 already covers "two copies for a playset"
MAX_EVALS = 30          # hard cap on full optimiser runs, in case pruning is weak


@dataclass
class Precon:
    set_name: str
    champion: str
    cards: Dict[str, int]

    @property
    def key(self) -> str:
        return f"{self.set_name} Champion Deck: {self.champion}"

    @property
    def pseudo_card(self) -> str:
        return PSEUDO_PREFIX + self.key


PRECONS: List[Precon] = [
    Precon("Origins", "Jinx", {
        "Blazing Scorcher": 2,
        "Brazen Buccaneer": 3,
        "Cemetery Attendant": 2,
        "Chaos Rune": 6,
        "Chemtech Enforcer": 3,
        "Fading Memories": 2,
        "Fight or Flight": 3,
        "Flame Chompers": 3,
        "Fury Rune": 6,
        "Get Excited!": 2,
        "Gust": 3,
        "Jinx, Demolitionist": 1,
        "Jinx, Loose Cannon": 1,
        "Magma Wurm": 1,
        "Raging Soul": 3,
        "Reaver's Row": 1,
        "Rhasa the Sunderer": 1,
        "Scrapheap": 3,
        "Targon's Peak": 1,
        "Traveling Merchant": 3,
        "Undercover Agent": 2,
        "Vi, Destructive": 1,
        "Void Seeker": 2,
        "Zaun Warrens": 1,
    }),
    Precon("Origins", "Viktor", {
        "Altar to Unity": 1,
        "Back to Back": 2,
        "Consult the Past": 2,
        "Cruel Patron": 3,
        "Cull the Weak": 3,
        "Eager Apprentice": 3,
        "Grand Strategem": 1,
        "Heimerdinger, Inventor": 1,
        "Hidden Blade": 2,
        "Jeweled Colossus": 2,
        "Mind Rune": 6,
        "Mushroom Pouch": 2,
        "Noxian Drummer": 3,
        "Orb of Regret": 2,
        "Order Rune": 6,
        "Ravenbloom Student": 3,
        "Smoke Screen": 2,
        "Soaring Scout": 3,
        "Sprite Call": 2,
        "Stupefy": 2,
        "The Grand Plaza": 1,
        "Trifarian War Camp": 1,
        "Viktor, Herald of the Arcane": 1,
        "Viktor, Innovator": 1,
        "Wraith of Echoes": 1,
    }),
    Precon("Origins", "Lee Sin", {
        "Bilgewater Bully": 2,
        "Body Rune": 6,
        "Calm Rune": 6,
        "Challenge": 3,
        "Charm": 2,
        "Discipline": 3,
        "First Mate": 3,
        "Grove of the God-Willow": 1,
        "Lee Sin, Blind Monk": 1,
        "Lee Sin, Centered": 1,
        "Mask of Foresight": 1,
        "Mistfall": 1,
        "Monastery of Hirana": 1,
        "Mountain Drake": 2,
        "Pakaa Cub": 2,
        "Pit Rookie": 3,
        "Stalwart Poro": 3,
        "Stand United": 2,
        "Stormclaw Ursine": 2,
        "Targon's Peak": 1,
        "Udyr, Wildman": 1,
        "Wielder of Water": 3,
        "Wildclaw Shaman": 3,
        "Wizened Elder": 3,
    }),
    Precon("Spiritforged", "Fiora", {
        "B.F. Sword": 3,
        "Body Rune": 6,
        "Dauntless Vanguard": 2,
        "Doran's Blade": 3,
        "Fiora, Grand Duelist": 1,
        "Fiora, Peerless": 2,
        "Fiora, Worthy": 1,
        "Jaull-Fish": 2,
        "Laurent Duelist": 2,
        "Lucian, Merciless": 2,
        "Order Rune": 6,
        "Ornn's Forge": 1,
        "Pit Rookie": 2,
        "Punch First": 2,
        "Riposte": 1,
        "Royal Guard": 2,
        "Sacred Shears": 1,
        "Show of Strength": 2,
        "Strike Down": 3,
        "Sunken Temple": 1,
        "Unsung Hero": 2,
        "Veiled Temple": 1,
        "Vengeance": 2,
        "Veteran poro": 2,
        "Warmog's Armor": 3,
        "Yone, Blademaster": 1,
    }),
    Precon("Spiritforged", "Rumble", {
        "Assembly Rig": 1,
        "Breakneck Mech": 2,
        "Bubble Bot": 3,
        "Consult the Past": 2,
        "Danger Zone": 1,
        "Dangerous Duo": 3,
        "Ferrous Forerunner": 2,
        "Forecaster": 3,
        "Frigid Touch": 2,
        "Fury Rune": 6,
        "Gem Jammer": 3,
        "Long Sword": 1,
        "Mind Rune": 6,
        "Minefield": 1,
        "Plundering Poro": 3,
        "Production Surge": 3,
        "Ravenbloom Conservatory": 1,
        "Rumble, Hotheaded": 2,
        "Rumble, Mechanized Menace": 1,
        "Rumble, Scrapper": 1,
        "Singularity": 1,
        "Stupefy": 2,
        "Treasure Hoard": 1,
        "Void Seeker": 3,
        "Wages of Pain": 2,
    }),
    Precon("Unleashed", "Vi", {
        "Arena Kingpin": 1,
        "Carrion Dredger": 2,
        "Crimson Pigeons": 2,
        "Divining Shells": 2,
        "Fury Rune": 6,
        "Hextech Gauntlets": 1,
        "Inferna": 3,
        "Lord Broadmane": 2,
        "Loyal Poro": 2,
        "Mageseeker Investigator": 2,
        "Order Rune": 6,
        "Rengar, Unseen": 1,
        "Right of Conquest": 2,
        "Serrated Dirk": 2,
        "Sharkling": 2,
        "Soul Harvest": 1,
        "Square Up": 2,
        "Star Spring": 1,
        "Tactical Retreat": 1,
        "Towering Pairofant": 2,
        "Trapping Grounds": 1,
        "Upstage Comedy": 1,
        "Valley of Idols": 1,
        "Vault Breaker": 2,
        "Vi, Destructive": 1,
        "Vi, Hotheaded": 1,
        "Vi, Peacekeeper": 2,
        "Vi, Piltover Enforcer": 1,
        "Xerath, Freed": 1,
        "Yeti Brawler": 2,
    }),
    Precon("Unleashed", "Vex", {
        "Allay, Eager Admirer": 2,
        "Amateur Recital": 1,
        "Back Off": 3,
        "Blast Cone": 2,
        "Calm Rune": 6,
        "Chaos Rune": 6,
        "Combat Experience": 2,
        "Enthusiastic Promoter": 1,
        "Evelynn, Entrancing": 1,
        "Existential Dread": 2,
        "Gardens of Becoming": 1,
        "Herald of Spring": 2,
        "Iascylla": 1,
        "Megatusk": 1,
        "Mister Root": 2,
        "Monch": 1,
        "Mosstomper": 2,
        "Mutated Mouser": 2,
        "Nami, Headstrong": 1,
        "Ripper's Bay": 1,
        "Scryer's Bloom": 1,
        "Shadow": 1,
        "Skyward Strike": 2,
        "Soul Sword": 2,
        "Trevor Snoozebottom": 2,
        "Vex, Apathetic": 1,
        "Vex, Cheerless": 1,
        "Vex, Gloomist": 1,
        "Vex, Mocking": 2,
        "Wuju Apprentice": 3,
    }),
]


def is_pseudo(card: str) -> bool:
    return card.startswith(PSEUDO_PREFIX)


def _contents_in_want_terms(precon: Precon, want: Dict[str, int]) -> Dict[str, int]:
    """The deck's cards that appear in `want`, keyed by want's own spelling
    (a decklist might write "Veteran Poro", the deck list "Veteran poro")."""
    by_norm = {normalize_name(c): c for c in want if not is_pseudo(c)}
    out: Dict[str, int] = {}
    for card, qty in precon.cards.items():
        w = by_norm.get(normalize_name(card))
        if w:
            out[w] = out.get(w, 0) + qty
    return out


def relevant_precons(want: Dict[str, int]) -> List[Precon]:
    """Decks containing at least one card from `want` -- the only ones worth
    fetching prices for."""
    return [p for p in PRECONS if _contents_in_want_terms(p, want)]


def _reduce(want: Dict[str, int], combo: Tuple[Precon, ...]):
    """-> (remaining want, per-copy coverage in combo order), or None if some
    copy would cover nothing (a strictly worse combo than without it)."""
    remaining = dict(want)
    per_copy: List[Dict[str, int]] = []
    for p in combo:
        cov: Dict[str, int] = {}
        for card, qty in _contents_in_want_terms(p, want).items():
            take = min(qty, remaining.get(card, 0))
            if take > 0:
                cov[card] = take
                remaining[card] -= take
        if not cov:
            return None
        per_copy.append(cov)
    return {c: q for c, q in remaining.items() if q > 0}, per_copy


def _lower_bound(demand: Dict[str, int], index: Dict[str, List[Listing]]) -> Tuple[int, float]:
    """(min possible unfilled, min possible cost) for `demand`: every unit at
    its cheapest listed price, stock-limited, no shipping. No real plan can
    beat this, which is what makes pruning on it safe."""
    unfilled, cost = 0, 0.0
    for card, need in demand.items():
        for l in sorted(index.get(card, []), key=lambda l: l.price):
            if need <= 0:
                break
            take = min(need, l.stock)
            cost += take * l.price
            need -= take
        unfilled += max(need, 0)
    return unfilled, cost


@dataclass
class ComboResult:
    label: str
    covered_units: int
    cheapest_decks: float        # sum of each deck's cheapest offer
    status: str                  # "evaluated" | "pruned" | "unavailable" | "skipped"
    total: Optional[float] = None
    used: bool = False


def _label(combo: Tuple[Precon, ...]) -> str:
    counts: Dict[str, int] = {}
    for p in combo:
        counts[p.key] = counts.get(p.key, 0) + 1
    return " + ".join(f"{n}x {k}" if n > 1 else k for k, n in counts.items())


def _annotate(plan: Plan, combo: Tuple[Precon, ...], per_copy: List[Dict[str, int]],
              index: Dict[str, List[Listing]]) -> Plan:
    """Attach what each bought deck actually covers -- report() prints it
    under the deck line, and expand_bundles() needs it for group-buy splits."""
    cover: Dict[str, List[Dict[str, int]]] = {}
    deck_cards: Dict[str, int] = {}
    for p, cov in zip(combo, per_copy):
        cover.setdefault(p.pseudo_card, []).append(cov)
        deck_cards[p.pseudo_card] = deck_cards.get(p.pseudo_card, 0) + sum(p.cards.values())

    notes: Dict[Tuple[str, str], str] = {}
    for seller, lines in plan.orders.items():
        for card, qty, _ in lines:
            if not is_pseudo(card):
                continue
            covered: Dict[str, int] = {}
            for cov in cover[card]:
                for c, q in cov.items():
                    covered[c] = covered.get(c, 0) + q
            n_decks = len(cover[card])
            needed = sum(covered.values())
            extras = deck_cards[card] - needed
            listing_url = next((l.url for l in index.get(card, []) if l.seller == seller and l.url), None)
            text = ", ".join(f"{q}x {c}" for c, q in sorted(covered.items()))
            note = (f"{'these decks cover' if n_decks > 1 else 'covers'} {needed} card(s) you need: "
                    f"{text} (+{extras} other card(s) you don't need)")
            if listing_url:
                note += f"\n            {listing_url}"
            notes[(seller, card)] = note
    return replace(plan, bundle_notes=notes, bundle_cover=cover)


def improve_with_precons(want: Dict[str, int],
                         listings: List[Listing],
                         sellers: List[Seller],
                         baseline: Optional[Plan],
                         max_sellers: int,
                         pickup: PickupPolicy = PickupPolicy()
                         ) -> Tuple[Optional[Plan], List[ComboResult]]:
    """-> (a plan that uses one or more decks and beats `baseline`, or None if
    none does; one row per combo considered, for report_combos())."""
    index: Dict[str, List[Listing]] = {}
    for l in listings:
        index.setdefault(l.card, []).append(l)

    offered = [p for p in relevant_precons(want) if index.get(p.pseudo_card)]
    if not offered:
        return None, []

    candidates = []
    for n in range(1, MAX_PRECON_COPIES + 1):
        for combo in combinations_with_replacement(offered, n):
            reduced = _reduce(want, combo)
            if reduced is None:
                continue
            remaining, per_copy = reduced
            demand = dict(remaining)
            for p in combo:
                demand[p.pseudo_card] = demand.get(p.pseudo_card, 0) + 1
            candidates.append((_lower_bound(demand, index), combo, demand, per_copy))
    candidates.sort(key=lambda c: c[0])

    best: Optional[Plan] = baseline
    best_key = _plan_key(baseline) if baseline else (float("inf"), float("inf"))
    rows: List[ComboResult] = []
    evals = 0
    for (lb_unfilled, lb_cost), combo, demand, per_copy in candidates:
        deck_cost = sum(min(l.price for l in index[p.pseudo_card]) for p in combo)
        row = ComboResult(_label(combo), sum(sum(c.values()) for c in per_copy), deck_cost, "pruned")
        rows.append(row)
        if lb_unfilled > best_key[0] or (lb_unfilled == best_key[0] and lb_cost >= best_key[1] - 0.005):
            continue
        if evals >= MAX_EVALS:
            row.status = "skipped"
            continue
        evals += 1
        plan = optimise(demand, listings, sellers, max_sellers=max_sellers,
                        allow_unfilled=True, pickup=pickup)
        if plan is None or any(is_pseudo(c) for c in plan.unfilled):
            row.status = "unavailable"
            continue
        row.status, row.total = "evaluated", plan.total
        key = _plan_key(plan)
        if key < best_key:
            best, best_key = _annotate(plan, combo, per_copy, index), key
            for r in rows:
                r.used = False
            row.used = True

    return (best if best is not baseline else None), rows


def report_combos(rows: List[ComboResult], baseline: Optional[Plan]) -> None:
    if not rows:
        return
    print(f"\n{'=' * 62}\nPRECONSTRUCTED CHAMPION DECKS\n{'=' * 62}")
    if baseline is not None:
        print(f"  singles-only best: ${baseline.total:.2f}")
    shown = [r for r in rows if r.status != "pruned" or " + " not in r.label]
    for r in shown:
        if r.status == "evaluated":
            result = f"best plan with it: ${r.total:.2f}" + ("  <-- used" if r.used else "")
        elif r.status == "pruned":
            result = "can't beat singles even at cost price -- skipped"
        elif r.status == "unavailable":
            result = "not enough in stock"
        else:
            result = f"not checked (over the {MAX_EVALS}-combo limit)"
        print(f"  {r.label}")
        print(f"      covers {r.covered_units} card(s) you need, deck(s) from ${r.cheapest_decks:.2f}"
              f" -- {result}")
    hidden = len(rows) - len(shown)
    if hidden:
        print(f"  (+{hidden} two-deck combination(s) ruled out the same way)")
    print("  Deck contents only -- the booster pack/playmat/deckbox in the box aren't valued.")


def expand_bundles(plan: Plan, listings: List[Listing]) -> Plan:
    """Replace each deck line with the needed cards it covers, splitting the
    deck's price across them by each card's cheapest singles price (so a
    deck covering one $5 card and one $0.10 rune doesn't bill them equally).
    Totals, sellers and shipping are unchanged -- this only exists so
    group_buy.attribute_costs() can bill players for the cards they asked
    for, since nobody asked for "[PRECON] ..." itself."""
    if not plan.bundle_cover:
        return plan
    ref: Dict[str, float] = {}
    for l in listings:
        if not is_pseudo(l.card):
            ref[l.card] = min(ref.get(l.card, l.price), l.price)

    remaining_copies = {card: list(copies) for card, copies in plan.bundle_cover.items()}
    new_orders: Dict[str, List[Tuple[str, int, float]]] = {}
    for seller, lines in plan.orders.items():
        out: List[Tuple[str, int, float]] = []
        for card, qty, price in lines:
            if not is_pseudo(card):
                out.append((card, qty, price))
                continue
            for _ in range(qty):
                cov = remaining_copies[card].pop(0)
                weights = {c: q * ref.get(c, 0.0) for c, q in cov.items()}
                wsum = sum(weights.values())
                units = sum(cov.values())
                for c, q in cov.items():
                    share = price * (weights[c] / wsum if wsum > 0 else q / units)
                    out.append((c, q, share / q))
        new_orders[seller] = out
    return replace(plan, orders=new_orders, bundle_notes=None, bundle_cover=None)
