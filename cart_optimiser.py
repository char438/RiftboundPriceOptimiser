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


@dataclass
class Plan:
    total: float
    cards_cost: float
    shipping_cost: float
    # seller -> list of (card, qty, unit_price)
    orders: Dict[str, List[Tuple[str, int, float]]]
    unfilled: Dict[str, int]        # card -> copies we could not source


# --------------------------------------------------------------------------
# Solver
# --------------------------------------------------------------------------

def _score_subset(subset: Tuple[str, ...],
                  want: Dict[str, int],
                  index: Dict[str, List[Listing]],
                  sellers: Dict[str, Seller],
                  allow_unfilled: bool,
                  enforce_minimums: bool = True) -> Optional[Plan]:
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

    shipping_cost = 0.0
    for seller_name, lines in orders.items():
        seller = sellers[seller_name]
        subtotal = sum(qty * price for _, qty, price in lines)

        if enforce_minimums and subtotal < seller.minimum_order:
            return None              # infeasible: below their minimum

        if seller.free_shipping_over is not None and subtotal >= seller.free_shipping_over:
            continue                 # postage waived
        qty_total = sum(qty for _, qty, _ in lines)
        shipping_cost += seller_shipping_cost(seller, qty_total)

    return Plan(
        total=cards_cost + shipping_cost,
        cards_cost=cards_cost,
        shipping_cost=shipping_cost,
        orders=orders,
        unfilled=unfilled,
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


def _brute_force(want, index, seller_map, relevant, max_sellers) -> Optional[Plan]:
    best: Optional[Plan] = None
    best_key: Optional[Tuple[int, float]] = None
    for k in range(1, min(max_sellers, len(relevant)) + 1):
        for subset in combinations(relevant, k):
            plan = _score_subset(subset, want, index, seller_map, allow_unfilled=True)
            if plan is None:
                continue
            key = _plan_key(plan)
            if best_key is None or key < best_key:
                best, best_key = plan, key
    return best


def _greedy_with_local_search(want, index, seller_map, relevant, max_sellers) -> Optional[Plan]:
    selected: List[str] = []
    selected_set = set()

    def plan_for(sellers_subset: List[str]) -> Tuple[Optional[Plan], Tuple[int, float]]:
        if not sellers_subset:
            empty_unfilled = sum(want.values())
            return None, (empty_unfilled, 0.0)
        p = _score_subset(tuple(sellers_subset), want, index, seller_map, allow_unfilled=True)
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
             allow_unfilled: bool = True) -> Optional[Plan]:
    """Cheapest plan using at most `max_sellers` sellers, preferring to
    complete the want-list over minimizing spend (see _plan_key) -- an
    unfilled card is a failure to solve for, not a discount.

    Exact (brute force over every seller subset) when the search space is
    small enough; otherwise a greedy + local-swap heuristic (see
    _BRUTE_FORCE_LIMIT) that scales to real seller counts.
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
        return _brute_force(want, index, seller_map, relevant, max_sellers)
    return _greedy_with_local_search(want, index, seller_map, relevant, max_sellers)


def sweep(want: Dict[str, int],
          listings: List[Listing],
          sellers: List[Seller],
          max_k: int) -> List[Tuple[int, Optional[Plan]]]:
    """Solve for every seller cap from 1..max_k, so you can see the actual
    cost-vs-number-of-parcels tradeoff instead of guessing at max_sellers."""
    return [(k, optimise(want, listings, sellers, max_sellers=k, allow_unfilled=True))
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


def naive_cheapest_per_card(want, listings, sellers) -> Optional[Plan]:
    """What you'd get by just clicking the cheapest listing for every card.
    Included so you can see how much the optimiser actually saves."""
    all_names = sorted({l.seller for l in listings})
    seller_map = {s.name: s for s in sellers}
    index: Dict[str, List[Listing]] = {}
    for l in listings:
        index.setdefault(l.card, []).append(l)
    return _score_subset(tuple(all_names), want, index, seller_map,
                         allow_unfilled=True, enforce_minimums=False)


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
        qty_total = sum(q for _, q, _ in lines)
        ship_note = ""
        url = None
        if sellers and seller in sellers:
            ship_note = f", shipping ${seller_shipping_cost(sellers[seller], qty_total):.2f}"
            url = sellers[seller].url
        print(f"\n  {seller}   ({len(lines)} lines, subtotal ${subtotal:.2f}{ship_note})")
        if url:
            print(f"      {url}")
        for card, qty, price in sorted(lines):
            print(f"      {qty}x  {card:<28} @ ${price:>5.2f}  = ${qty * price:>6.2f}")

    if plan.unfilled:
        print("\n  COULD NOT SOURCE:")
        for card, qty in sorted(plan.unfilled.items()):
            print(f"      {qty}x  {card}")

    print(f"\n  {'-' * 44}")
    print(f"  cards       ${plan.cards_cost:>8.2f}")
    print(f"  shipping    ${plan.shipping_cost:>8.2f}   ({len(plan.orders)} parcels)")
    print(f"  TOTAL       ${plan.total:>8.2f}")