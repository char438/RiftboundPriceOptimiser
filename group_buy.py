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
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from cart_optimiser import Plan, Seller, seller_shipping_cost


@dataclass
class PlayerBill:
    name: str
    card_lines: List[Tuple[str, float, float]] = field(default_factory=list)  # (card, qty, cost)
    card_total: float = 0.0
    shipping_lines: List[Tuple[str, float]] = field(default_factory=list)     # (seller, cost)
    shipping_total: float = 0.0
    unfilled: Dict[str, float] = field(default_factory=dict)                  # card -> shortfall qty
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
        qty_total = sum(qty for _, qty, _ in lines)
        ship_cost = seller_shipping_cost(sellers[seller_name], qty_total)
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


def sanity_check(plan: Plan, bills: Dict[str, PlayerBill], tol: float = 0.01) -> None:
    """Every dollar of the pooled plan must land on exactly one player --
    call this after attribute_costs() if you ever touch its math."""
    card_sum = sum(b.card_total for b in bills.values())
    ship_sum = sum(b.shipping_total for b in bills.values())
    assert abs(card_sum - plan.cards_cost) < tol, (card_sum, plan.cards_cost)
    assert abs(ship_sum - plan.shipping_cost) < tol, (ship_sum, plan.shipping_cost)


def report_bills(bills: Dict[str, PlayerBill]) -> None:
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
        print(f"      TOTAL OWED: ${bill.total_owed:>7.2f}")
        grand_total += bill.total_owed

    print(f"\n  {'=' * 44}")
    print(f"  GRAND TOTAL (all players): ${grand_total:.2f}")
