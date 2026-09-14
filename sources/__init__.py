"""
Site-specific source modules. Each one exposes:

    NAME: str
    fetch(want: Dict[str, int]) -> Tuple[List[Listing], List[Seller]]

`fetch` does whatever that site needs (search, paginate, scrape) and
returns only the cards it actually found from `want`, plus the Seller(s)
behind them (their shipping cost/tiers). fetch_and_optimise.py merges the
results of every enabled source before handing them to cart_optimiser.

  * fetchtcg.py    -- fetchtcg.com, a many-sellers marketplace.
  * nz_shopify.py  -- a growing list of single-store NZ/Auckland TCG
                       retailers (Card Merchant, Card Masters, Calico
                       Keep, Game Roost, ...), each its own Seller. Add a
                       new store by adding one StoreConfig entry to its
                       STORES list -- no new file needed, as long as the
                       store also runs Shopify (check for it with
                       `curl {base_url}/products.json?limit=1`).

To add an entirely new *kind* of site (not Shopify-based): write a new
module here with the same `fetch` signature, then add it to ALL_SOURCES
below.
"""

from . import fetchtcg, nz_shopify

ALL_SOURCES = [fetchtcg, nz_shopify]
