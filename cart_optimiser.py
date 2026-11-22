#!/usr/bin/env python3
"""
Multi-seller cart optimiser: given a want-list and per-seller listings,
find the cheapest way to buy everything across at most N sellers.

This module has no knowledge of where listings come from -- see
sources/ for the actual scrapers, and fetch_and_optimise.py for the
pipeline that calls this on their combined output.

THE PROBLEM
-----------
You need N cards. Each is listed by several sellers at different prices.
Each seller charges shipping once per order, may have a minimum order value,
and may waive shipping above some threshold.

Buying every card from its individually-cheapest seller is almost never
optimal, because each extra seller you touch adds a whole shipping fee.
Equally, buying everything from one seller is often not optimal either.
The answer is somewhere in between and it is not obvious by eye.

Formally this is the uncapacitated facility location problem:
  sellers   = facilities, "opening cost" = their shipping fee
  cards     = customers,  "assignment cost" = that seller's price
It is NP-hard in general. But two things make our instances trivial:

  1. They're small. Tens of cards, tens of sellers.
  2. The optimal solution never uses many sellers. If shipping is $5 and
     the whole deck is $40 of cardboard, a 6-parcel plan cannot win. So we
     cap the search at MAX_SELLERS and brute-force every subset.

For a FIXED subset of sellers the inner problem is trivial: buy each card
from the cheapest seller in the subset that still has stock. So the whole
thing is:

    for each subset S of sellers with |S| <= k:
        cost(S) = sum over cards of (cheapest price within S)
                + sum over sellers in S actually used of (their shipping)
    return argmin

With 30 sellers and k=4 that's ~35k subsets, each O(n*k) to score.
Runs in well under a second. This is exact within the k cap, not a heuristic.

"""

from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Seller:
    name: str
    shipping: float = 5.00          # flat postage, used when shipping_tiers is empty
    free_shipping_over: Optional[float] = None   # subtotal that waives postage
    minimum_order: float = 0.00     # refuses orders below this subtotal
    # Real per-seller postage is banded by card COUNT, not a flat fee --
    # e.g. "$8 for 1-50 cards, $15 for 50-300". Each tuple is
    # (min_cards, max_cards, cost). Empty/None falls back to `shipping`.
    shipping_tiers: Optional[List[Tuple[int, int, float]]] = None
    # Where to actually go buy from this seller -- their fetchtcg.com
    # profile, or an NZ store's own storefront. None if a source didn't
    # set one (shouldn't happen for a seller that produced any listing,
    # but report() copes either way).
    url: Optional[str] = None
    # Set (alongside pickup_note) when a source found this seller genuinely
    # offers free in-person pickup near the buyer (see fetchtcg.py's LOCAL
    # PICKUP handling) -- the postage cost if you pick up instead of using
    # `shipping`/`shipping_tiers`, almost always 0.0. This is just a fact
    # about the seller; whether/how much it actually gets used is a policy
    # decision made per-run by PickupPolicy, not baked in here, since one
    # person might happily collect from five different sellers and another
    # might want at most one meetup, or none at all.
    pickup_cost: Optional[float] = None
    pickup_note: Optional[str] = None


def seller_shipping_cost(seller: "Seller", qty: int) -> float:
    """Postage for shipping `qty` cards from this seller."""
    if not seller.shipping_tiers:
        return seller.shipping
    matching = [cost for lo, hi, cost in seller.shipping_tiers if lo <= qty <= hi]
    if matching:
        return min(matching)
    # Above every defined tier (e.g. a huge order) -- use the tier that
    # covers the highest count as the best available estimate.
    return max(seller.shipping_tiers, key=lambda t: t[1])[2]


@dataclass(frozen=True)
class Listing:
    card: str
    seller: str
    price: float                    # per copy
    stock: int = 99                 # copies this seller actually has
    url: Optional[str] = None       # product page, where a source has one per listing


@dataclass
class Plan:
    total: float
    cards_cost: float
    shipping_cost: float
    # seller -> list of (card, qty, unit_price)
    orders: Dict[str, List[Tuple[str, int, float]]]
    unfilled: Dict[str, int]        # card -> copies we could not source
    # sellers in `orders` whose shipping cost above is their pickup_cost,
    # not their normal courier rate -- report() uses this (rather than
    # inferring "shipping happened to be $0") so a free_shipping_over
    # threshold and an actual in-person pickup never get confused.
    pickup_used: frozenset = frozenset()
    # seller -> what they actually got charged (0.0 if waived or picked
    # up) -- report() reads this directly instead of recomputing
    # seller_shipping_cost() itself, which used to silently ignore both
    # free_shipping_over and pickup and could print a shipping figure
    # that didn't match what shipping_cost actually added up to.
    shipping_by_seller: Optional[Dict[str, float]] = None
    # Set by precons.improve_with_precons() when the plan buys a whole
    # preconstructed deck: (seller, "[PRECON] ..." line) -> what it covers,
    # printed by report(); and each deck line's per-copy coverage, used by
    # precons.expand_bundles() to split it into real cards for group buys.
    bundle_notes: Optional[Dict[Tuple[str, str], str]] = None
    bundle_cover: Optional[Dict[str, List[Dict[str, int]]]] = None


@dataclass
class PickupPolicy:
    """Controls whether/how a plan is allowed to use a seller's free local
    pickup instead of paying their courier rate -- see Seller.pickup_cost.
    Purely a policy knob: it has no effect on sellers that don't have
    pickup_cost set in the first place (that's still discovered per-source,
    e.g. fetchtcg.py deciding a seller is close enough to physically meet).
    """
    enabled: bool = True
    # If set, only these seller names may use pickup even if eligible --
    # for "I'll only drive to meet these specific people".
    only_sellers: Optional[frozenset] = None
    # Seller names that must never use pickup even if eligible -- for
    # "everyone except this one, they're sketchy" without having to name
    # every other eligible seller via only_sellers.
    exclude_sellers: frozenset = frozenset()
    # Cap on how many sellers in one plan may use pickup -- for "I'll do
    # at most 2 meetups, courier the rest". None = no cap.
    max_pickups: Optional[int] = None


# --------------------------------------------------------------------------
# Solver
# --------------------------------------------------------------------------

def _pickup_eligible(seller_name: str, seller: Seller, pickup: PickupPolicy) -> bool:
    if not pickup.enabled or seller.pickup_cost is None:
        return False
    if pickup.only_sellers is not None and seller_name not in pickup.only_sellers:
        return False
    return seller_name not in pickup.exclude_sellers


def _score_subset(subset: Tuple[str, ...],
                  want: Dict[str, int],
                  index: Dict[str, List[Listing]],
                  sellers: Dict[str, Seller],
                  allow_unfilled: bool,
                  enforce_minimums: bool = True,
                  pickup: PickupPolicy = PickupPolicy()) -> Optional[Plan]:
    """Cost of buying everything using only the sellers in `subset`."""
    in_subset = set(subset)
    orders: Dict[str, List[Tuple[str, int, float]]] = {s: [] for s in subset}
    unfilled: Dict[str, int] = {}
    cards_cost = 0.0

    for card, qty_needed in want.items():
        remaining = qty_needed
        # listings for this card, cheapest first, restricted to the subset
        options = [l for l in index.get(card, []) if l.seller in in_subset]
        options.sort(key=lambda l: l.price)

        for listing in options:
            if remaining <= 0:
                break
            take = min(remaining, listing.stock)
            if take <= 0:
                continue
            orders[listing.seller].append((card, take, listing.price))
            cards_cost += take * listing.price
            remaining -= take

        if remaining > 0:
            if not allow_unfilled:
                return None          # this subset can't complete the list
            unfilled[card] = remaining

    # Drop sellers we ended up not using -- don't charge their postage.
    orders = {s: lines for s, lines in orders.items() if lines}
    if not orders:
        return None

    # First pass: each seller's normal (non-pickup) postage, and whether
    # they're even eligible for pickup at all -- deciding WHICH eligible
    # sellers actually get to use it (there may be more than max_pickups
    # allows) comes after, once every seller's cost is known.
    paid_cost: Dict[str, float] = {}
    pickup_candidates: List[Tuple[str, float]] = []   # (seller, $ saved by picking up)
    for seller_name, lines in orders.items():
        seller = sellers[seller_name]
        subtotal = sum(qty * price for _, qty, price in lines)

        if enforce_minimums and subtotal < seller.minimum_order:
            return None              # infeasible: below their minimum

        if seller.free_shipping_over is not None and subtotal >= seller.free_shipping_over:
            paid_cost[seller_name] = 0.0
            continue                 # postage already waived, pickup buys nothing
        qty_total = sum(qty for _, qty, _ in lines)
        cost = seller_shipping_cost(seller, qty_total)
        paid_cost[seller_name] = cost
        if _pickup_eligible(seller_name, seller, pickup):
            savings = cost - seller.pickup_cost
            if savings > 0:
                pickup_candidates.append((seller_name, savings))

    # Picking up from any eligible seller only ever helps, so use it
    # everywhere allowed -- unless max_pickups forces a choice, in which
    # case take the sellers where it saves the most first (each seller's
    # saving is independent of every other's, so greedy-by-savings is
    # exactly optimal here, not just a heuristic).
    pickup_used = set()
    if pickup_candidates:
        pickup_candidates.sort(key=lambda t: -t[1])
        cap = len(pickup_candidates) if pickup.max_pickups is None else pickup.max_pickups
        pickup_used = {name for name, _ in pickup_candidates[:max(cap, 0)]}

    shipping_by_seller = {
        name: (sellers[name].pickup_cost if name in pickup_used else cost)
        for name, cost in paid_cost.items()
    }
    shipping_cost = sum(shipping_by_seller.values())

    return Plan(
        total=cards_cost + shipping_cost,
        cards_cost=cards_cost,
        shipping_cost=shipping_cost,
        orders=orders,
        unfilled=unfilled,
        pickup_used=frozenset(pickup_used),
        shipping_by_seller=shipping_by_seller,
    )


def _plan_key(plan: Plan) -> Tuple[int, float]:
    """Rank plans by how much of the list they complete FIRST, cost second.

    Without this, a plan that buys nothing at all has unfilled=everything
    but cost=$0 -- which would always look "cheapest" to a plain cost
    comparison. allow_unfilled exists so an infeasible full list doesn't
    make the whole search come back empty, not so the search can dodge
    buying anything.
    """
    return (sum(plan.unfilled.values()), plan.total)


# Above this many (seller-subset) combinations, brute force is no longer
# practical (each subset costs O(cards * listings-per-card) to score, and
# with real seller counts in the tens to hundreds, C(n, k) blows past
# anything Python can chew through in a human-scale wait). Fall back to a
# greedy construction plus local-swap improvement instead. It's not
# guaranteed optimal, but for this kind of small facility-location-ish
# instance it lands at or very near the true optimum in practice, and
# actually finishes.
_BRUTE_FORCE_LIMIT = 300_000


def _n_choose_k(n: int, k: int) -> int:
    from math import comb
    return comb(n, k) if 0 <= k <= n else 0


def _brute_force(want, index, seller_map, relevant, max_sellers, pickup) -> Optional[Plan]:
    best: Optional[Plan] = None
    best_key: Optional[Tuple[int, float]] = None
    for k in range(1, min(max_sellers, len(relevant)) + 1):
        for subset in combinations(relevant, k):
            plan = _score_subset(subset, want, index, seller_map, allow_unfilled=True, pickup=pickup)
            if plan is None:
                continue
            key = _plan_key(plan)
            if best_key is None or key < best_key:
                best, best_key = plan, key
    return best


def _greedy_with_local_search(want, index, seller_map, relevant, max_sellers, pickup) -> Optional[Plan]:
    selected: List[str] = []
    selected_set = set()

    def plan_for(sellers_subset: List[str]) -> Tuple[Optional[Plan], Tuple[int, float]]:
        if not sellers_subset:
            empty_unfilled = sum(want.values())
            return None, (empty_unfilled, 0.0)
        p = _score_subset(tuple(sellers_subset), want, index, seller_map, allow_unfilled=True, pickup=pickup)
        return p, (_plan_key(p) if p else (sum(want.values()), float("inf")))

    _, current_key = plan_for(selected)

    # Greedy construction: repeatedly add whichever unused seller most
    # improves (fewest unfilled, then lowest cost).
    for _ in range(max_sellers):
        best_candidate, best_key = None, None
        for cand in relevant:
            if cand in selected_set:
                continue
            _, key = plan_for(selected + [cand])
            if best_key is None or key < best_key:
                best_candidate, best_key = cand, key
        if best_candidate is None or best_key >= current_key:
            break
        selected.append(best_candidate)
        selected_set.add(best_candidate)
        current_key = best_key

    # Local search: try swapping one selected seller for one unselected
    # seller, or dropping a now-redundant seller, as long as it helps.
    improved = True
    while improved:
        improved = False
        for i, out_seller in enumerate(selected):
            trial = selected[:i] + selected[i + 1:]
            _, key = plan_for(trial)
            if key <= current_key:
                selected, selected_set = trial, set(trial)
                current_key = key
                improved = True
                break
            for cand in relevant:
                if cand in selected_set:
                    continue
                trial2 = trial + [cand]
                _, key2 = plan_for(trial2)
                if key2 < current_key:
                    selected, selected_set = trial2, set(trial2)
                    current_key = key2
                    improved = True
                    break
            if improved:
                break

    plan, _ = plan_for(selected)
    return plan


def optimise(want: Dict[str, int],
             listings: List[Listing],
             sellers: List[Seller],
             max_sellers: int = 4,
             allow_unfilled: bool = True,
             pickup: PickupPolicy = PickupPolicy()) -> Optional[Plan]:
    """Cheapest plan using at most `max_sellers` sellers, preferring to
    complete the want-list over minimizing spend (see _plan_key) -- an
    unfilled card is a failure to solve for, not a discount.

    Exact (brute force over every seller subset) when the search space is
    small enough; otherwise a greedy + local-swap heuristic (see
    _BRUTE_FORCE_LIMIT) that scales to real seller counts.

    `pickup` controls whether/how eligible sellers (Seller.pickup_cost set)
    get to use free local pickup instead of their courier rate -- see
    PickupPolicy. Defaults to "use it wherever available", same as if this
    parameter didn't exist.
    """
    seller_map = {s.name: s for s in sellers}

    index: Dict[str, List[Listing]] = {}
    for l in listings:
        index.setdefault(l.card, []).append(l)

    # Only consider sellers who stock at least one thing we want.
    relevant = sorted({l.seller for l in listings if l.card in want})
    k = min(max_sellers, len(relevant))

    total_subsets = sum(_n_choose_k(len(relevant), i) for i in range(1, k + 1))
    if total_subsets <= _BRUTE_FORCE_LIMIT:
        return _brute_force(want, index, seller_map, relevant, max_sellers, pickup)
    return _greedy_with_local_search(want, index, seller_map, relevant, max_sellers, pickup)


def sweep(want: Dict[str, int],
          listings: List[Listing],
          sellers: List[Seller],
          max_k: int,
          pickup: PickupPolicy = PickupPolicy()) -> List[Tuple[int, Optional[Plan]]]:
    """Solve for every seller cap from 1..max_k, so you can see the actual
    cost-vs-number-of-parcels tradeoff instead of guessing at max_sellers."""
    return [(k, optimise(want, listings, sellers, max_sellers=k, allow_unfilled=True, pickup=pickup))
            for k in range(1, max_k + 1)]


def best_from_sweep(results: List[Tuple[int, Optional[Plan]]]) -> Tuple[Optional[int], Optional[Plan]]:
    """The single cheapest (min unfilled, then min cost) plan across every
    seller-count sweep() tried -- the actual answer to "what should I buy
    and from whom", instead of making you eyeball the tradeoff table and
    re-run optimise() at whichever k looks best."""
    feasible = [(k, p) for k, p in results if p is not None]
    if not feasible:
        return None, None
    return min(feasible, key=lambda kp: _plan_key(kp[1]))


def report_sweep(results: List[Tuple[int, Optional[Plan]]]) -> None:
    """`k` is the seller-count CAP handed to optimise(), not necessarily how
    many sellers that plan actually used -- once more sellers stop helping,
    raising the cap further just reproduces the same plan (see the
    "parcels" column, which is the real count)."""
    best_k, _ = best_from_sweep(results)
    print(f"\n{'=' * 62}\nCOST VS. NUMBER OF ORDERS\n{'=' * 62}")
    print(f"  {'cap':>4}  {'parcels':>7}  {'cards':>9}  {'shipping':>9}  {'total':>9}  unfilled")
    for k, plan in results:
        if plan is None:
            print(f"  {k:>4}  (no feasible plan)")
            continue
        unfilled_qty = sum(plan.unfilled.values())
        marker = "  <-- cheapest" if k == best_k else ""
        print(f"  {k:>4}  {len(plan.orders):>7}  ${plan.cards_cost:>8.2f}  "
              f"${plan.shipping_cost:>8.2f}  ${plan.total:>8.2f}  {unfilled_qty}{marker}")


def naive_cheapest_per_card(want, listings, sellers,
                            pickup: PickupPolicy = PickupPolicy()) -> Optional[Plan]:
    """What you'd get by just clicking the cheapest listing for every card.
    Included so you can see how much the optimiser actually saves."""
    all_names = sorted({l.seller for l in listings})
    seller_map = {s.name: s for s in sellers}
    index: Dict[str, List[Listing]] = {}
    for l in listings:
        index.setdefault(l.card, []).append(l)
    return _score_subset(tuple(all_names), want, index, seller_map,
                         allow_unfilled=True, enforce_minimums=False, pickup=pickup)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def report(plan: Optional[Plan], label: str,
           sellers: Optional[Dict[str, Seller]] = None) -> None:
    print(f"\n{'=' * 62}\n{label}\n{'=' * 62}")
    if plan is None:
        print("No feasible plan (try raising max_sellers or allow_unfilled).")
        return

    for seller, lines in sorted(plan.orders.items()):
        subtotal = sum(q * p for _, q, p in lines)
        ship_note = ""
        url = None
        pickup_note = None
        if plan.shipping_by_seller and seller in plan.shipping_by_seller:
            ship_note = f", shipping ${plan.shipping_by_seller[seller]:.2f}"
        if sellers and seller in sellers:
            s = sellers[seller]
            url = s.url
            if seller in plan.pickup_used and s.pickup_note:
                pickup_note = s.pickup_note
        print(f"\n  {seller}   ({len(lines)} lines, subtotal ${subtotal:.2f}{ship_note})")
        if pickup_note:
            print(f"      ! {pickup_note}")
        if url:
            print(f"      {url}")
        for card, qty, price in sorted(lines):
            print(f"      {qty}x  {card:<28} @ ${price:>5.2f}  = ${qty * price:>6.2f}")
            note = (plan.bundle_notes or {}).get((seller, card))
            if note:
                print(f"            {note}")

    if plan.unfilled:
        print("\n  COULD NOT SOURCE:")
        for card, qty in sorted(plan.unfilled.items()):
            print(f"      {qty}x  {card}")

    print(f"\n  {'-' * 44}")
    print(f"  cards       ${plan.cards_cost:>8.2f}")
    print(f"  shipping    ${plan.shipping_cost:>8.2f}   ({len(plan.orders)} parcels)")
    print(f"  TOTAL       ${plan.total:>8.2f}")