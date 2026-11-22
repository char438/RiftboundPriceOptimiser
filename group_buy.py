#!/usr/bin/env python3
"""
Splits one pooled cart_optimiser.Plan back out into a fair per-player bill.

THE PROBLEM
-----------
Buying for N people at once and optimising the POOLED want-list (everyone's
cards combined) is strictly cheaper than N separate optimise() runs -- more
combined volume means more of each seller's shipping gets amortised across
more cards. But the moment you pool, "how much does the group spend" stops
answering the actual question, which is "how much does each person owe".
This module does that attribution.

CARD COST -- averaged, not lot-assigned
------------------------------------------
Pooled demand for a single card can be filled by more than one seller at
different prices (e.g. 5 needed, 3 from Seller A at $1, 2 from Seller B at
$1.50). If a card is fungible (any copy is as good as any other -- true for
bulk deck commons), there's no fair reason one player's units should be
"assigned" to the pricier lot and another's to the cheaper one just because
of allocation order. So every player pays the same AVERAGE price for that
card (total spent on it / total copies bought), and how many of those
average-priced copies each player gets is apportioned to their share of
demand for that card via the largest-remainder method (apportion_shares
below) -- exact integers, summing to exactly how many were actually bought,
even when demand outstrips supply.

SHIPPING -- proportional to each player's dollar share of that seller's order
--------------------------------------------------------------------------------
Splitting a seller's shipping charge evenly across everyone who ordered
anything from them isn't fair when the amounts are lopsided (one person's
$2 of commons vs. another's $50 chase card, both from the same seller).
So each player's share of a seller's shipping is proportional to their
dollar share of that seller's subtotal. That dollar share is computed from
the SAME per-card averaged price and apportioned quantities used for the
card-cost split above, weighted by what fraction of that card's total
purchase came from this particular seller -- so a player who only touches
one lot of a multi-seller card still gets billed shipping fairly across
whichever seller(s) their apportioned units are attributed to.

Both splits are exact: sum of every player's card_total == plan.cards_cost,
and sum of every player's shipping_total == plan.shipping_cost, always
(verify this yourself with sanity_check() below if you touch this logic).

INDIVIDUAL RATIONALITY -- nobody should pay more than they'd pay alone
------------------------------------------------------------------------
Pooling is supposed to make everyone's life cheaper by amortising shipping
over more cards. The averaged-price + proportional-shipping split above is
fair in the sense that it tracks actual usage, but "fair" isn't the same as
"never worse than the alternative" -- a player who only wants one $0.20
common, pooled alongside someone buying $80 of chase cards from a seller
with expensive shipping, could end up owing more of that shipping than
they'd have paid getting that single common from whoever's solo-cheapest,
purely because their dollar share of that seller's cart looks proportional
even though the shipping itself has nothing to do with card value. Nobody
who joins a group buy should come out behind a player who just bought
alone -- that's not a rounding nitpick, it's the whole reason someone would
agree to pool in the first place.

solo_plan_costs() computes what each player's want-list would cost them
optimising ENTIRELY alone (same market, same listings/sellers -- just not
pooled). apply_individual_rationality() then caps anyone whose proportional
bill above exceeds that solo cost, and rebalances the shortfall onto
whoever still has headroom (repeating -- capping one player can push
another over their own cap next), via water-filling: at each round, split
whatever's still uncovered among the still-uncapped players in proportion
to their ORIGINAL (uncapped) bills, cap anyone that pushes over, repeat.
This never touches card_lines/shipping_total (the breakdown stays honest
about what was actually bought) -- it adds a separate, visible
`fairness_adjustment` on top, which always sums to exactly $0.00 across the
group (it's a pure transfer, not new or vanished money). If pooling somehow
costs MORE in total than the sum of everyone's solo prices (should be rare
-- see the module docstring's opening claim, and the function's own
docstring for when this can happen), full individual rationality is
mathematically impossible and apply_individual_rationality() leaves bills
unadjusted, returning False so the caller can say so out loud instead of
quietly serving a broken guarantee.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from cart_optimiser import (Plan, PickupPolicy, Seller, best_from_sweep,
                            seller_shipping_cost, sweep)


@dataclass
class PlayerBill:
    name: str
    card_lines: List[Tuple[str, float, float]] = field(default_factory=list)  # (card, qty, cost)
    card_total: float = 0.0
    shipping_lines: List[Tuple[str, float]] = field(default_factory=list)     # (seller, cost)
    shipping_total: float = 0.0
    unfilled: Dict[str, float] = field(default_factory=dict)                  # card -> shortfall qty
    # What this player would have paid buying entirely alone (see
    # solo_plan_costs) -- None if no solo plan could source anything for
    # them at all, in which case there's no meaningful ceiling to enforce.
    solo_cost: Optional[float] = None
    # Signed $ moved by apply_individual_rationality() on top of
    # card_total + shipping_total: negative = this player was over their
    # solo_cost and got discounted down to it; positive = they picked up
    # part of someone else's discount. Always 0.0 until that's called, and
    # always sums to ~$0.00 across every player's bill when it is.
    fairness_adjustment: float = 0.0
    total_owed: float = 0.0


def pool_players(players: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    """Combine every player's want-list into one pooled want-list, summing
    quantities for any card more than one person needs."""
    pooled: Dict[str, int] = {}
    for want in players.values():
        for card, qty in want.items():
            pooled[card] = pooled.get(card, 0) + qty
    return pooled


def apportion_shares(ideal_shares: Dict[str, float], total: int) -> Dict[str, int]:
    """Largest-remainder apportionment: round each player's real-valued ideal
    share down, then hand the leftover whole units to whoever had the
    largest fractional remainder, one each, until they're gone. Guarantees
    the result sums to exactly `total` while staying as proportional to
    ideal_shares as integers allow."""
    floors = {k: int(v) for k, v in ideal_shares.items()}
    remainder = total - sum(floors.values())
    order = sorted(ideal_shares.keys(), key=lambda k: ideal_shares[k] - floors[k], reverse=True)
    for k in order[:remainder]:
        floors[k] += 1
    return floors


def attribute_costs(plan: Plan, players: Dict[str, Dict[str, int]],
                    sellers: Dict[str, Seller]) -> Dict[str, PlayerBill]:
    bills = {name: PlayerBill(name=name) for name in players}

    # card -> player -> quantity that player asked for
    demand: Dict[str, Dict[str, int]] = {}
    for name, want in players.items():
        for card, qty in want.items():
            demand.setdefault(card, {})
            demand[card][name] = demand[card].get(name, 0) + qty

    # card -> total qty bought / total $ spent, across every seller that supplied it
    filled_by_card: Dict[str, int] = {}
    spent_by_card: Dict[str, float] = {}
    for lines in plan.orders.values():
        for card, qty, price in lines:
            filled_by_card[card] = filled_by_card.get(card, 0) + qty
            spent_by_card[card] = spent_by_card.get(card, 0.0) + qty * price

    # card -> player -> apportioned fulfilled qty (integers, sums to filled_by_card[card])
    fulfilled: Dict[str, Dict[str, int]] = {}
    for card, per_player in demand.items():
        total_demand = sum(per_player.values())
        total_filled = filled_by_card.get(card, 0)
        if total_filled <= 0:
            fulfilled[card] = {name: 0 for name in per_player}
        elif total_filled >= total_demand:
            fulfilled[card] = dict(per_player)
        else:
            ideal = {name: (qty / total_demand) * total_filled for name, qty in per_player.items()}
            fulfilled[card] = apportion_shares(ideal, total_filled)

    # card cost + shortfall per player, at the averaged price
    for card, per_player in demand.items():
        total_filled = filled_by_card.get(card, 0)
        avg_price = (spent_by_card[card] / total_filled) if total_filled else 0.0
        for name, wanted in per_player.items():
            got = fulfilled[card].get(name, 0)
            if got > 0:
                cost = got * avg_price
                bills[name].card_lines.append((card, got, cost))
                bills[name].card_total += cost
            short = wanted - got
            if short > 0:
                bills[name].unfilled[card] = bills[name].unfilled.get(card, 0) + short

    # shipping: each player's dollar share of each seller's order, then that
    # seller's shipping charge split in the same proportions
    for seller_name, lines in plan.orders.items():
        seller_subtotal = sum(qty * price for _, qty, price in lines)
        # plan.shipping_by_seller is what this seller was actually charged
        # in this exact plan -- unlike recomputing seller_shipping_cost()
        # fresh, it already accounts for a waived free_shipping_over
        # threshold and for pickup (see cart_optimiser.Plan), so a $0
        # pickup or free-shipping order correctly bills nobody for it
        # instead of splitting a courier fee that was never charged.
        ship_cost = (plan.shipping_by_seller or {}).get(
            seller_name, seller_shipping_cost(sellers[seller_name],
                                              sum(qty for _, qty, _ in lines)))
        if ship_cost <= 0:
            continue

        player_contrib: Dict[str, float] = {}
        for card, qty, price in lines:
            total_filled = filled_by_card[card]
            fraction_from_this_seller = qty / total_filled
            for name in demand[card]:
                got = fulfilled[card].get(name, 0)
                if got > 0:
                    player_contrib[name] = (player_contrib.get(name, 0.0)
                                            + got * fraction_from_this_seller * price)

        if seller_subtotal <= 0:
            continue
        for name, contrib in player_contrib.items():
            share = ship_cost * (contrib / seller_subtotal)
            if share > 0:
                bills[name].shipping_lines.append((seller_name, share))
                bills[name].shipping_total += share

    for bill in bills.values():
        bill.total_owed = bill.card_total + bill.shipping_total

    return bills


def solo_plan_costs(players: Dict[str, Dict[str, int]],
                    listings, sellers,
                    sweep_to: int = 15,
                    pickup: PickupPolicy = PickupPolicy()) -> Dict[str, Optional[float]]:
    """Each player's own cheapest total (cards + shipping) buying entirely
    alone from this same market -- the ceiling apply_individual_rationality()
    enforces. Uses the same best-of-sweep search the pooled plan itself was
    chosen by (see cart_optimiser.best_from_sweep), so it's an
    apples-to-apples "what would THIS optimiser have found you alone",
    not a cruder estimate.

    -> name -> cost, or None if that player's want-list has NO feasible
    solo plan at all (e.g. every card they want is completely unlisted
    anywhere) -- such a player has no meaningful "would've paid" to compare
    against, so apply_individual_rationality() leaves them uncapped rather
    than pretending their ceiling is $0."""
    costs: Dict[str, Optional[float]] = {}
    for name, want in players.items():
        if not want:
            costs[name] = 0.0
            continue
        _, plan = best_from_sweep(sweep(want, listings, sellers, max_k=sweep_to, pickup=pickup))
        costs[name] = plan.total if plan is not None else None
    return costs


def _water_fill_cap(total_owed: Dict[str, float],
                    caps: Dict[str, float]) -> Tuple[Dict[str, float], bool]:
    """-> (new_total_owed, feasible). caps holds only players who HAVE a
    ceiling; anyone missing from it is uncapped and can absorb shortfall
    without limit. See the module docstring for the algorithm. `feasible`
    is False if even capping every player at their ceiling can't cover the
    group's actual total (sum(caps) < total, for the players that have
    one) -- new_total_owed is meaningless in that case, don't use it."""
    total = sum(total_owed.values())
    remaining = set(total_owed)
    fixed: Dict[str, float] = {}

    while remaining:
        target = total - sum(fixed.values())
        basis_sum = sum(total_owed[n] for n in remaining)
        if basis_sum <= 1e-9:
            # Everyone left originally owed ~$0 -- split whatever's left
            # (should itself be ~$0 too) evenly rather than divide by zero.
            share = target / len(remaining)
            for n in remaining:
                fixed[n] = share
            remaining.clear()
            break

        shares = {n: target * (total_owed[n] / basis_sum) for n in remaining}
        over = [n for n in remaining if n in caps and shares[n] > caps[n] + 1e-9]
        if not over:
            fixed.update(shares)
            remaining.clear()
            break
        for n in over:
            fixed[n] = caps[n]
            remaining.discard(n)

    feasible = abs(sum(fixed.values()) - total) < 1e-6
    return fixed, feasible


def apply_individual_rationality(bills: Dict[str, PlayerBill],
                                 solo_costs: Dict[str, Optional[float]]) -> bool:
    """Mutates `bills` in place so nobody's total_owed exceeds their
    solo_costs[name] (players absent or None there are left uncapped),
    redistributing the difference onto whoever still has headroom -- see
    the module docstring. card_lines/card_total/shipping_lines/
    shipping_total are untouched; only fairness_adjustment and total_owed
    change, and fairness_adjustment always sums to ~$0.00 on success.

    -> True if the cap was actually enforceable and applied; False (bills
    left exactly as attribute_costs() produced them, fairness_adjustment
    all 0.0) if even everyone paying their own solo price wouldn't cover
    the pooled total -- meaning pooling this exact group came out MORE
    expensive than everyone buying separately. In principle the pooled
    optimiser always had the option of reproducing separate solo purchases
    routed through the same sellers, so this "should" only happen from real
    allocation effects (stock contention on a scarce card, a minimum_order
    threshold that bites one combination but not the other). In practice,
    real seller counts push both the pooled AND solo searches past
    cart_optimiser's exact brute-force limit into its greedy+local-search
    heuristic (see _BRUTE_FORCE_LIMIT) -- which is NOT guaranteed optimal --
    so a $1-2 gap from heuristic noise landing on a slightly different local
    optimum in the larger pooled search space is, in practice, the more
    common cause. Either way the right response is the same: don't claim a
    guarantee the numbers don't back up."""
    for name, bill in bills.items():
        bill.solo_cost = solo_costs.get(name)

    caps = {name: cost for name, cost in solo_costs.items()
           if cost is not None and name in bills}
    original = {name: bill.total_owed for name, bill in bills.items()}
    new_totals, feasible = _water_fill_cap(original, caps)
    if not feasible:
        return False

    for name, bill in bills.items():
        bill.fairness_adjustment = new_totals[name] - original[name]
        bill.total_owed = new_totals[name]
    return True


def sanity_check(plan: Plan, bills: Dict[str, PlayerBill], tol: float = 0.01) -> None:
    """Every dollar of the pooled plan must land on exactly one player --
    call this after attribute_costs() if you ever touch its math. Still
    holds after apply_individual_rationality(): fairness_adjustment is a
    zero-sum transfer on top, so total_owed still sums to plan.total even
    though card_total/shipping_total alone no longer do (that's expected --
    see PlayerBill.fairness_adjustment)."""
    card_sum = sum(b.card_total for b in bills.values())
    ship_sum = sum(b.shipping_total for b in bills.values())
    adjustment_sum = sum(b.fairness_adjustment for b in bills.values())
    total_sum = sum(b.total_owed for b in bills.values())
    assert abs(card_sum - plan.cards_cost) < tol, (card_sum, plan.cards_cost)
    assert abs(ship_sum - plan.shipping_cost) < tol, (ship_sum, plan.shipping_cost)
    assert abs(adjustment_sum) < tol, adjustment_sum
    assert abs(total_sum - plan.total) < tol, (total_sum, plan.total)


def report_bills(bills: Dict[str, PlayerBill], fairness_applied: bool = False) -> None:
    print(f"\n{'=' * 62}\nWHO OWES WHAT\n{'=' * 62}")
    grand_total = 0.0
    for name, bill in bills.items():
        print(f"\n  {name}")
        for card, qty, cost in sorted(bill.card_lines):
            qty_str = f"{qty:.2f}" if qty != int(qty) else str(int(qty))
            print(f"      {qty_str}x  {card:<28} = ${cost:>6.2f}")
        if bill.unfilled:
            for card, qty in sorted(bill.unfilled.items()):
                qty_str = f"{qty:.2f}" if qty != int(qty) else str(int(qty))
                print(f"      COULD NOT SOURCE {qty_str}x {card}")
        print(f"      {'-' * 34}")
        print(f"      cards:     ${bill.card_total:>7.2f}")
        for seller, cost in sorted(bill.shipping_lines):
            print(f"      shipping share ({seller}): ${cost:.2f}")
        print(f"      shipping:  ${bill.shipping_total:>7.2f}")
        if abs(bill.fairness_adjustment) > 0.005:
            kind = "discount -- capped at solo price" if bill.fairness_adjustment < 0 \
                  else "covers part of another player's discount"
            print(f"      fairness:  {'-' if bill.fairness_adjustment < 0 else '+'}"
                  f"${abs(bill.fairness_adjustment):>6.2f}   ({kind})")
        if bill.solo_cost is not None:
            print(f"      (buying alone would have cost: ${bill.solo_cost:.2f})")
        print(f"      TOTAL OWED: ${bill.total_owed:>7.2f}")
        grand_total += bill.total_owed

    print(f"\n  {'=' * 44}")
    print(f"  GRAND TOTAL (all players): ${grand_total:.2f}")
    if fairness_applied:
        print(f"  (fairness cap applied: nobody above pays more than they would have alone)")
