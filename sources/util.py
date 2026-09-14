#!/usr/bin/env python3
"""Shared plumbing for every site-specific source module: HTTP GET with
retries, the name normalizer that lets a decklist entry like "Azir,
Emperor of the Sands" match however a given site happens to spell it
("Azir - Emperor of the Sands", "AZIR, EMPEROR OF THE SANDS", etc.), and a
shared WARNINGS list for anything that could make a run's prices wrong
without it being obvious from a glance at the final numbers."""

import json
import re
import time
import urllib.error
import urllib.request
from typing import List

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; personal-deck-pricer/1.0)",
    "Accept": "application/json",
}
REQUEST_DELAY = 0.15  # seconds between requests to any one site, be polite


class RateLimited(Exception):
    """Raised by get_json when every retry hit HTTP 429. Callers must not
    treat this like an ordinary empty/failed result -- a 429 means "the
    site is telling us to slow down", not "this doesn't exist". Treating
    it as the latter would make a card that's genuinely in stock look
    like it isn't carried anywhere, silently dropping it from that
    source's candidate pool and producing a "cheapest" plan that isn't
    actually cheapest. See WARNINGS / warn() for how callers should
    surface this instead."""
    pass


def get_json(url: str, retries: int = 6):
    """Returns the parsed JSON, or None if the request failed for an
    ordinary reason (network blip, a real 404, timeout) after retrying.
    Raises RateLimited instead of returning None if every retry hit HTTP
    429 -- see that class's docstring for why that distinction matters."""
    req = urllib.request.Request(url, headers=HEADERS)
    backoff = 1.0
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt == retries:
                    raise RateLimited(url)
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = float(retry_after) if retry_after else backoff
                except ValueError:
                    delay = backoff
                print(f"      [rate limited] waiting {delay:.0f}s before retry "
                      f"{attempt + 1}/{retries}... ({url})", flush=True)
                time.sleep(delay)
                backoff = min(backoff * 2, 30.0)
                continue
            if attempt == retries:
                print(f"      failed after {retries} attempts: {url} ({e})")
                return None
            time.sleep(0.75 * attempt)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries:
                print(f"      failed after {retries} attempts: {url} ({e})")
                return None
            time.sleep(0.75 * attempt)
    return None


WARNINGS: List[str] = []


def warn(msg: str) -> None:
    """Print AND remember something that could make this run's prices
    wrong in a way that isn't obvious from the final numbers alone (a
    card that was never actually checked due to persistent rate-limiting,
    for instance). fetch_and_optimise.run() reprints everything collected
    here as an unmissable banner after the results, then clears the list."""
    print(f"  ⚠ {msg}")
    WARNINGS.append(msg)


_APOSTROPHES = re.compile(r"['‘’`]")


def normalize_name(name: str) -> str:
    """lowercase, strip everything but letters/digits, collapse whitespace --
    makes "Azir, Emperor of the Sands" == "Azir - Emperor of the Sands" ==
    "AZIR EMPEROR OF THE SANDS", regardless of a site's own punctuation
    conventions.

    Apostrophes are DELETED rather than turned into a space, since they're
    always internal to a name in this game (Rek'Sai, Kai'Sa, Doran's
    Shield, ...), never a real word boundary. Getting this wrong silently
    breaks matching for exactly those cards: "Rek'Sai" would normalize to
    "rek sai" (treating the apostrophe as a separator) while a decklist
    that just typed "Reksai" normalizes to "reksai" -- two different
    strings that never match, with no error, just a quiet false
    "not in stock anywhere". Deleting the apostrophe instead makes both
    sides converge on "reksai" regardless of which one used it.
    """
    name = _APOSTROPHES.sub("", name)
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
