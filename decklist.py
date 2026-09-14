#!/usr/bin/env python3
"""
Parses riftdecks.com's exported decklist .txt format into a card-name ->
quantity dict, ready to hand to fetch_and_optimise.py's --decklist.

FORMAT
------
Section headers end in a colon; each card line is "<qty> <card name>":

    Legend:
    1 Master Yi, Wuju Bladesman

    Champion:
    1 Master Yi, Tempered

    MainDeck:
    1 Alpha Strike
    3 Charm
    ...

    Battlefields:
    1 Abandoned Hall

    Rune Pool:
    7 Body Rune
    5 Calm Rune

    Sideboard:
    1 Alpha Strike
    2 Decree of Focus

Section names themselves aren't semantically important for buying purposes
-- what matters is card name -> total quantity you need to own. A name
that appears in more than one section (e.g. "Alpha Strike" in both
MainDeck and Sideboard above) gets its quantities SUMMED, not overwritten,
since that means real, separate physical copies you need for both.

GETTING THE .txt FROM RIFTDECKS.COM
--------------------------------------
Every deck page has an "Export this Deck" -> "Export TXT" button, which
links to `/decks/export/{deckId}/txt` -- a real, fetchable URL, not just a
client-side download. fetch_from_url() below will try it directly. BUT:
riftdecks.com sits behind Cloudflare's bot-protection, which blocks plain
scripted requests (confirmed: a bare fetch gets Cloudflare's "Attention
Required" block page, not the deck). Whether that also blocks a request
from your own machine depends on Cloudflare's bot-scoring of your IP --
it's worth trying, but if it fails, the reliable path is: open the deck
page yourself, click Export TXT to download the file, then pass that file
to --decklist instead.
"""

import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/plain,text/html",
}

_CARD_LINE = re.compile(r"^(\d+)\s+(.+)$")


def parse_decklist_text(text: str) -> Dict[str, int]:
    """card name -> total quantity, summed across every section it appears in."""
    want: Dict[str, int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # tolerate riftdecks.com's tab-separated on-page rendering too
        # ("1 \t Card Name \t $0.20"), not just the plain export format.
        if "\t" in line:
            line = line.split("\t")[0].strip()
        line = re.sub(r"\s*\$[\d,.]+\s*$", "", line).strip()

        if line.endswith(":"):
            continue  # section header -- not semantically needed, see module docstring

        m = _CARD_LINE.match(line)
        if not m:
            continue
        qty = int(m.group(1))
        name = m.group(2).strip()
        if not name:
            continue
        want[name] = want.get(name, 0) + qty
    return want


def parse_decklist_file(path: str) -> Dict[str, int]:
    return parse_decklist_text(Path(path).read_text())


def fetch_from_url(deck_url: str) -> Dict[str, int]:
    """Best-effort direct pull from a riftdecks.com deck page URL. Likely
    to be blocked by Cloudflare -- see module docstring. Raises RuntimeError
    with a clear message (never silently returns nothing) if it fails, so
    the caller can fall back to --decklist <exported .txt> instead."""
    m = re.search(r"riftdecks\.com/decks/([0-9a-fA-F-]{36})", deck_url)
    if not m:
        # Not a raw deck-id URL -- try the page itself and look for the
        # export link, since a "view deck" URL uses a slug, not the id.
        page_html = _get(deck_url)
        id_match = re.search(r"/decks/export/([0-9a-fA-F-]{36})/txt", page_html)
        if not id_match:
            raise RuntimeError(
                f"couldn't find a deck export link on {deck_url} -- "
                "open the deck page yourself, click 'Export this Deck' -> "
                "'Export TXT', and pass that file to --decklist instead.")
        deck_id = id_match.group(1)
    else:
        deck_id = m.group(1)

    export_url = f"https://riftdecks.com/decks/export/{deck_id}/txt"
    text = _get(export_url)
    if "cloudflare" in text.lower() and "blocked" in text.lower():
        raise RuntimeError(
            f"riftdecks.com blocked this request (Cloudflare bot-protection) -- "
            f"open {deck_url} yourself, click 'Export this Deck' -> 'Export TXT' "
            "to download the file, then pass it to --decklist instead.")
    want = parse_decklist_text(text)
    if not want:
        raise RuntimeError(
            f"fetched {export_url} but found no card lines in it -- the page "
            "format may have changed; fall back to --decklist with a "
            "manually-exported .txt file.")
    return want


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise RuntimeError(f"couldn't reach {url}: {e}") from e
