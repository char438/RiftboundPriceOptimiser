# Riftbound Deck Pricer

Given a Riftbound decklist, finds the cheapest way to actually buy every
card: checking multiple sellers and stores, accounting for real shipping
costs, and picking the seller split that minimizes total cost. That's not
the same as the cheapest listing per card, which usually isn't the
cheapest cart.

Buying every card from its individually-cheapest listing is almost never
optimal, because each extra seller you buy from adds a whole shipping
charge. Buying everything from one seller is often not optimal either.
This tool searches every seller split up to a configurable limit and
finds the actual minimum.

## What it does

- Pulls live listings from [Fetch TCG](https://www.fetchtcg.com) (a
  multi-seller Riftbound marketplace) and a set of NZ single-store TCG
  retailers.
- Computes real per-seller shipping cost, not a flat guess, wherever the
  data is available.
- Finds the cheapest total (cards + shipping) across every seller count
  from 1 up to a configurable maximum, and reports which count is
  actually cheapest, not just "cheapest if capped at N".
- **Group buys**: pool multiple people's decklists into one purchase
  (more combined volume amortises shipping further), then splits the
  cost and shipping back out fairly per person.
- Imports decklists directly from a
  [riftdecks.com](https://riftdecks.com) export.
- A minimal desktop GUI (`ui.py`) if you'd rather not use the command line.

## Setup

No dependencies beyond the Python standard library.

```bash
python3 --version   # needs Python 3.8+
```

For the GUI, you also need `tkinter`. It's usually bundled, but on
Homebrew-installed Python on macOS it isn't by default:

```bash
python3 -c "import tkinter"   # errors if missing
brew install python-tk@3.14   # match your python3 --version
```

## Usage

```bash
# Price a decklist
python3 fetch_and_optimise.py --decklist examples/azir_emperor_of_the_sands.txt

# Try a riftdecks.com URL directly. Best-effort: riftdecks.com is behind
# Cloudflare bot-protection, so this may get blocked. If it does, use
# --decklist with a manually-exported .txt instead.
python3 fetch_and_optimise.py --decklist-url https://riftdecks.com/decks/<slug>

# Group buy: pool several people's decks into one purchase, split the bill
python3 fetch_and_optimise.py \
    --player Alice:alice.txt --player Bob:bob.txt --player You:my_deck.txt

# Re-run against a previous run's data without hitting the network again
python3 fetch_and_optimise.py --decklist my_deck.txt --offline listings_cache.json

# GUI
python3 ui.py
```

See `python3 fetch_and_optimise.py --help` for the full flag list, and
`decklist.py` for the exported-.txt format: a plain-text card list
grouped into sections like `MainDeck:`, `Sideboard:`, `Rune Pool:`.

## How it works

```
sources/fetchtcg.py     Fetch TCG marketplace (many sellers)
sources/nz_shopify.py   single-store NZ retailers (one Seller each)
        |
        v
fetch_and_optimise.py   calls fetch(want) on each enabled source,
                        merges the results
        |
        v
cart_optimiser.py       finds the cheapest seller split
        |
        v
group_buy.py            (only for --player) splits the pooled plan
                        back into a fair per-person bill
```

Each source module exposes a single `fetch(want) -> (listings, sellers)`
function and knows nothing about the others. Adding a new source means
writing one new module with that signature and registering it in
`sources/__init__.py` (see that file's docstring, and
`sources/nz_shopify.py`'s docstring for how to add another Shopify-based
retailer specifically, which is usually just one config entry, no new
code).

## Data sources: unofficial APIs

None of the endpoints this uses are officially documented or supported.
They were found by inspecting each site's own network traffic.

- **Fetch TCG**: a plain JSON API on `api.fetchtcg.com` that the site's
  own frontend calls. See `sources/fetchtcg.py` for the endpoints and
  the measured rate limit.
- **NZ retailers**: all run Shopify, which exposes a public JSON mirror
  of any product collection (`/collections/{handle}/products.json`), a
  standard Shopify feature, not anything site-specific.

Both are read-only, unauthenticated, and rate-limited on this end to be
a reasonable citizen. That said, this isn't a sanctioned integration
with either platform, and could break if a site changes its API. Use
accordingly, and don't hammer it.

## Known limitations

- Shipping is confirmed/real for most sellers, but a few NZ retailers
  don't publish a flat rate. Those use a rough estimate, flagged
  explicitly in the output.
- Stock counts from the NZ Shopify stores are a guess (`available: true`
  is all their public API exposes, not an exact quantity); Fetch TCG's
  counts are real.
- Currency/shipping assumes a New Zealand buyer. If you're elsewhere,
  you'll want to adjust `COUNTRY_CODE`/`CURRENCY_CODE` in
  `sources/fetchtcg.py` and the NZ-specific store list.
- Only covers NZ sellers. No eBay, no other countries' marketplaces.

## License

Not yet licensed. Add one (MIT is a reasonable default for a project
like this) when you publish it.
