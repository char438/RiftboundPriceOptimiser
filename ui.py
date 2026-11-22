#!/usr/bin/env python3
"""
Minimal desktop GUI for fetch_and_optimise.py -- add one or more players,
each with a decklist (load a .txt or paste one in), hit Run, read the
result. No server, no browser: this is a local Tkinter window driving the
exact same backend (fetch_and_optimise.run()) the command line uses, just
without needing the command line.

Why a desktop app and not a web page: the actual scraping (Fetch TCG's
API, the Shopify stores) is plain Python urllib calls with no CORS headers
permitting arbitrary origins -- a web page can't make those requests
itself. A local Tkinter window has no such restriction, since it's just
Python calling Python, and tkinter ships with the standard library, so
there's nothing extra to install.

USAGE
-----
    python3 ui.py

One player = a normal single-deck run. Two or more = a group buy (pooled
purchase, cost/shipping split back out per person -- see group_buy.py).

LOCAL PICKUP, AND RE-OPTIMISING WITHOUT REFETCHING
---------------------------------------------------
The "Local pickup" panel controls cart_optimiser.PickupPolicy (see
fetchtcg.py's LOCAL PICKUP docs for what this actually is): whether to use
a seller's free in-person pickup at all, restrict it to/away from specific
sellers, and cap how many meetups one plan may use.

Changing any of those, or the seller-cap fields above them, doesn't need a
new fetch -- "Re-optimise (no refetch)" re-solves against the exact
listings/sellers from the last "Run", entirely in memory, so it's near-
instant. It's disabled until a Run has completed, and gets disabled again
the moment a player/decklist changes (so it can never silently price a
decklist that's no longer the one that was actually fetched).
"""

import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Dict, List, Optional

import decklist
import fetch_and_optimise as fao
import group_buy
from cart_optimiser import Listing, PickupPolicy, Seller


class PlayerRow:
    """One row in the player list: a name, and a decklist -- either loaded
    from a file or typed/pasted in. The raw text is kept (not just the
    parsed want dict) so "Edit decklist" reopens showing exactly what's
    there now, editable in place, instead of an empty box you'd have to
    repaste the whole thing into just to tweak one line."""

    def __init__(self, parent: tk.Widget, on_remove, on_change=lambda: None):
        self.want: Optional[Dict[str, int]] = None
        self.raw_text: str = ""
        self.on_change = on_change
        self.frame = ttk.Frame(parent)

        # Excluding a player from THIS run (e.g. "Bob's sitting this one
        # out") without deleting their pasted decklist -- unchecking just
        # drops them from the pool/validation, same as if their row didn't
        # exist, but everything they typed stays right there for next time.
        self.active_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.frame, variable=self.active_var,
                        command=self._on_active_toggled).pack(side="left", padx=(0, 4))

        self.name_var = tk.StringVar(value="")
        self.name_entry = ttk.Entry(self.frame, textvariable=self.name_var, width=14)
        self.name_entry.pack(side="left", padx=(0, 6))

        self.status_var = tk.StringVar(value="no decklist loaded")
        self.status_label = ttk.Label(self.frame, textvariable=self.status_var, width=28,
                                      foreground="#888888")
        self.status_label.pack(side="left", padx=(0, 6))

        self.load_button = ttk.Button(self.frame, text="Load .txt...", command=self._load_file)
        self.load_button.pack(side="left", padx=2)
        self.edit_button = ttk.Button(self.frame, text="Paste text...", command=self._edit_text)
        self.edit_button.pack(side="left", padx=2)
        ttk.Button(self.frame, text="Remove", command=lambda: on_remove(self)).pack(side="left", padx=(6, 0))

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)

    def destroy(self):
        self.frame.destroy()

    def _on_active_toggled(self) -> None:
        # Visual only -- excluded/included is read straight from
        # active_var wherever a player list is gathered, this is just so
        # an unchecked row doesn't look identical to a checked one.
        active = self.active_var.get()
        state = "normal" if active else "disabled"
        self.name_entry.config(state=state)
        self.load_button.config(state=state)
        self.edit_button.config(state=state)
        self.status_label.config(foreground="#888888" if active else "#555555")
        self.on_change()

    def _set_from_text(self, content: str, source: str, dialog: Optional[tk.Toplevel] = None) -> bool:
        """Parse `content`, and if it has at least one card line, store it
        as this row's decklist (both the parsed want and the raw text, so
        it can be edited again later) and update the status label. Returns
        whether it succeeded -- on failure the caller's dialog (if any)
        stays open so nothing typed is lost."""
        want = decklist.parse_decklist_text(content)
        if not want:
            messagebox.showwarning("Empty decklist",
                                   "No card lines found in that text.", parent=dialog)
            return False
        self.want = want
        self.raw_text = content
        n_unique, n_total = len(want), sum(want.values())
        self.status_var.set(f"{source}: {n_unique} cards, {n_total} copies")
        self.edit_button.config(text="Edit decklist...")
        self.on_change()
        return True

    def _load_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose an exported decklist .txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            content = open(path).read()
        except Exception as e:
            messagebox.showerror("Couldn't read file", str(e))
            return
        self._set_from_text(content, f"loaded {path.split('/')[-1]}")

    def _edit_text(self) -> None:
        """Opens with whatever this row's decklist currently is (blank if
        nothing loaded yet) -- pasting a fresh export and editing an
        already-loaded one are the same dialog."""
        dialog = tk.Toplevel()
        dialog.title("Edit decklist" if self.raw_text else "Paste decklist text")
        dialog.geometry("500x500")
        ttk.Label(dialog, text="Paste or edit the riftdecks.com exported decklist text below:").pack(
            anchor="w", padx=8, pady=(8, 4))
        text = scrolledtext.ScrolledText(dialog, wrap="word")
        text.pack(fill="both", expand=True, padx=8, pady=4)
        if self.raw_text:
            text.insert("1.0", self.raw_text)
        text.focus_set()

        def submit():
            source = "edited text" if self.raw_text else "pasted text"
            if self._set_from_text(text.get("1.0", "end"), source, dialog=dialog):
                dialog.destroy()

        btns = ttk.Frame(dialog)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="Cancel", command=dialog.destroy).pack(side="right", padx=(4, 0))
        ttk.Button(btns, text="Save", command=submit).pack(side="right")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Riftbound Deck Pricer")
        root.geometry("880x700")

        self.player_rows: list[PlayerRow] = []
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.running = False

        # Set after a successful "Run" (fetch + optimise) -- lets
        # "Re-optimise" recompute against the exact same listings/sellers
        # with a new PickupPolicy or seller-count settings, without ever
        # touching the network or re-reading the cache file. Cleared
        # whenever the player list changes, since a stale (want, listings)
        # pair could otherwise silently price the WRONG decklist.
        self.last_resolved_want: Optional[Dict[str, int]] = None
        self.last_players: Optional[Dict[str, Dict[str, int]]] = None
        self.last_listings: Optional[List[Listing]] = None
        self.last_sellers: Optional[List[Seller]] = None

        self._build_layout()
        self.add_player()  # start with one row -- single-deck mode by default
        self.root.after(100, self._drain_log_queue)

    # -- layout -------------------------------------------------------

    def _build_layout(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Players", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(top, text="One active player = a normal run. Two or more = a group buy "
                            "(pooled purchase, cost + shipping split back out per person). "
                            "Untick a player's box to sit them out of this run without "
                            "losing their pasted decklist.",
                 foreground="#666666", wraplength=820).pack(anchor="w", pady=(0, 6))

        self.players_frame = ttk.Frame(top)
        self.players_frame.pack(fill="x")

        ttk.Button(top, text="+ Add player", command=self.add_player).pack(anchor="w", pady=(6, 0))

        # -- options --
        opts = ttk.LabelFrame(self.root, text="Options", padding=10)
        opts.pack(fill="x", padx=10, pady=(0, 6))

        self.offline_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Reuse cached listings (skip live fetch) --",
                        variable=self.offline_var).grid(row=0, column=0, sticky="w")
        self.offline_path_var = tk.StringVar(value="listings_cache.json")
        ttk.Entry(opts, textvariable=self.offline_path_var, width=30).grid(row=0, column=1, sticky="w")

        ttk.Label(opts, text="Search up to this many sellers:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.sweep_to_var = tk.IntVar(value=15)
        ttk.Spinbox(opts, from_=1, to=50, textvariable=self.sweep_to_var, width=6).grid(
            row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(opts, text="Also compare a specific seller cap (optional):").grid(
            row=2, column=0, sticky="w", pady=(6, 0))
        self.max_sellers_var = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.max_sellers_var, width=6).grid(
            row=2, column=1, sticky="w", pady=(6, 0))

        self.use_precons_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Consider buying whole champion decks when cheaper than singles",
                        variable=self.use_precons_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # -- local pickup --
        pickup = ttk.LabelFrame(self.root, text="Local pickup", padding=10)
        pickup.pack(fill="x", padx=10, pady=(0, 6))

        self.allow_pickup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(pickup, text="Use a seller's free local pickup where available "
                                     "(instead of always paying courier)",
                        variable=self.allow_pickup_var).grid(
            row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(pickup, text="Only pick up from (comma-separated names, blank = any eligible):").grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        self.pickup_only_var = tk.StringVar(value="")
        ttk.Entry(pickup, textvariable=self.pickup_only_var, width=40).grid(
            row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(pickup, text="Never pick up from (comma-separated names):").grid(
            row=2, column=0, sticky="w", pady=(6, 0))
        self.pickup_exclude_var = tk.StringVar(value="")
        ttk.Entry(pickup, textvariable=self.pickup_exclude_var, width=40).grid(
            row=2, column=1, sticky="w", pady=(6, 0))

        ttk.Label(pickup, text="Max sellers to pick up from at once (blank = unlimited):").grid(
            row=3, column=0, sticky="w", pady=(6, 0))
        self.max_pickups_var = tk.StringVar(value="")
        ttk.Entry(pickup, textvariable=self.max_pickups_var, width=6).grid(
            row=3, column=1, sticky="w", pady=(6, 0))

        # -- run controls --
        run_frame = ttk.Frame(self.root, padding=(10, 0))
        run_frame.pack(fill="x")
        self.run_button = ttk.Button(run_frame, text="Run", command=self.run_clicked)
        self.run_button.pack(side="left")
        self.reoptimize_button = ttk.Button(run_frame, text="Re-optimise (no refetch)",
                                            command=self.reoptimize_clicked, state="disabled")
        self.reoptimize_button.pack(side="left", padx=(6, 0))
        self.status_var = tk.StringVar(value="")
        ttk.Label(run_frame, textvariable=self.status_var, foreground="#888888").pack(side="left", padx=10)
        ttk.Button(run_frame, text="Save output...", command=self.save_output).pack(side="right")

        # -- output --
        out_frame = ttk.Frame(self.root, padding=10)
        out_frame.pack(fill="both", expand=True)
        self.output = scrolledtext.ScrolledText(out_frame, wrap="word", font=("Menlo", 11),
                                                bg="#111111", fg="#dddddd", insertbackground="#dddddd")
        self.output.pack(fill="both", expand=True)

    # -- player row management -----------------------------------------

    def add_player(self) -> None:
        row = PlayerRow(self.players_frame, on_remove=self.remove_player,
                        on_change=self._invalidate_last_run)
        row.name_var.set(f"Player {len(self.player_rows) + 1}")
        row.pack(fill="x", pady=2)
        self.player_rows.append(row)
        self._invalidate_last_run()

    def remove_player(self, row: PlayerRow) -> None:
        if len(self.player_rows) == 1:
            messagebox.showinfo("Can't remove", "Keep at least one player.")
            return
        row.destroy()
        self.player_rows.remove(row)
        self._invalidate_last_run()

    def _invalidate_last_run(self) -> None:
        """The player list or a decklist changed since the last Run --
        Re-optimise would otherwise happily reprice a DIFFERENT decklist
        against stale listings and nobody would notice, so disable it
        until the next full Run re-establishes what "last fetched" means."""
        self.last_resolved_want = None
        self.last_players = None
        self.last_listings = None
        self.last_sellers = None
        self.reoptimize_button.config(state="disabled")

    # -- pickup policy ----------------------------------------------------

    def _read_pickup_policy(self) -> Optional[PickupPolicy]:
        """None on invalid input (an error dialog is already shown) --
        callers should bail out rather than fall back to a default."""
        only_raw = self.pickup_only_var.get().strip()
        exclude_raw = self.pickup_exclude_var.get().strip()
        if only_raw and exclude_raw:
            messagebox.showerror("Invalid input",
                                 "Use either 'Only pick up from' or 'Never pick up "
                                 "from', not both.")
            return None

        max_pickups = None
        raw = self.max_pickups_var.get().strip()
        if raw:
            try:
                max_pickups = int(raw)
            except ValueError:
                messagebox.showerror("Invalid input",
                                     "'Max sellers to pick up from at once' must be a number.")
                return None

        return PickupPolicy(
            enabled=self.allow_pickup_var.get(),
            only_sellers=frozenset(s.strip() for s in only_raw.split(",") if s.strip())
                         if only_raw else None,
            exclude_sellers=frozenset(s.strip() for s in exclude_raw.split(",") if s.strip())
                            if exclude_raw else frozenset(),
            max_pickups=max_pickups,
        )

    # -- running ---------------------------------------------------------

    def _read_players(self):
        """-> (want, players) for a single-deck or group-buy run, built only
        from rows whose "active" checkbox is ticked -- an unticked row is
        skipped entirely, same as if it didn't exist, but nothing about it
        (name, pasted decklist) is touched. Returns None (with an error
        dialog already shown) if the ACTIVE player list is invalid; an
        inactive row's missing name/decklist is never an error."""
        loaded = [(r.name_var.get().strip(), r.want)
                 for r in self.player_rows if r.active_var.get()]
        if not loaded:
            messagebox.showerror("No active players",
                                 "Every player is unticked -- tick at least one to run.")
            return None
        if any(not name for name, _ in loaded):
            messagebox.showerror("Missing name", "Every active player needs a name.")
            return None
        if any(want is None for _, want in loaded):
            messagebox.showerror("Missing decklist",
                                 "Every active player needs a decklist -- load a .txt "
                                 "or paste one in.")
            return None
        names = [name for name, _ in loaded]
        if len(set(names)) != len(names):
            messagebox.showerror("Duplicate names", "Active player names must be unique.")
            return None
        if len(loaded) == 1:
            return loaded[0][1], None
        return None, {name: w for name, w in loaded}

    def _read_max_sellers(self) -> tuple:
        """-> (ok, value). Shows an error dialog and returns (False, None)
        on bad input."""
        raw = self.max_sellers_var.get().strip()
        if not raw:
            return True, None
        try:
            return True, int(raw)
        except ValueError:
            messagebox.showerror("Invalid input", "'Also compare a seller cap' must be a number.")
            return False, None

    def run_clicked(self) -> None:
        if self.running:
            return

        read = self._read_players()
        if read is None:
            return
        want, players = read

        ok, max_sellers = self._read_max_sellers()
        if not ok:
            return

        pickup = self._read_pickup_policy()
        if pickup is None:
            return

        offline = self.offline_path_var.get().strip() if self.offline_var.get() else None

        self.output.delete("1.0", "end")
        self.running = True
        self.run_button.config(state="disabled")
        self.reoptimize_button.config(state="disabled")
        self.status_var.set("Running -- this can take a couple of minutes (live network fetch)...")

        thread = threading.Thread(target=self._run_worker,
                                  args=(want, players, offline, max_sellers,
                                        self.sweep_to_var.get(), pickup,
                                        self.use_precons_var.get()),
                                  daemon=True)
        thread.start()

    def _run_worker(self, want, players, offline, max_sellers, sweep_to, pickup,
                    use_precons) -> None:
        writer = _QueueWriter(self.log_queue)
        old_stdout = sys.stdout
        sys.stdout = writer
        try:
            if players:
                resolved_want = group_buy.pool_players(players)
                print(f"\npooled {len(players)} players: {len(resolved_want)} unique "
                      f"cards, {sum(resolved_want.values())} total copies needed")
            else:
                resolved_want = want
            # Deck prices are always fetched (cheap, cached), so the "champion
            # decks" checkbox can be flipped later via Re-optimise without a refetch.
            listings, sellers = fao.load_listings(resolved_want, offline=offline)
            fao.solve_and_report(resolved_want, players, listings, sellers,
                                 max_sellers=max_sellers, sweep_to=sweep_to, pickup=pickup,
                                 use_precons=use_precons)
            # Only kept on success -- an exception below leaves these unset
            # (or stale from before), which is exactly right: Re-optimise
            # should never silently work from a run that half-failed.
            self.last_resolved_want = resolved_want
            self.last_players = players
            self.last_listings = listings
            self.last_sellers = sellers
        except Exception as e:
            print(f"\nERROR: {e}")
        finally:
            sys.stdout = old_stdout
            self.log_queue.put("\n__DONE__")

    def reoptimize_clicked(self) -> None:
        """Re-solve against whatever load_listings() returned last time --
        no network, no cache file read, just cart_optimiser over data
        already in memory. Lets a pickup/shipping toggle (or the seller-cap
        fields) get applied instantly instead of waiting through a full
        re-fetch that would only ever reproduce the same listings anyway."""
        if self.running or self.last_listings is None:
            return

        ok, max_sellers = self._read_max_sellers()
        if not ok:
            return
        pickup = self._read_pickup_policy()
        if pickup is None:
            return

        self.output.delete("1.0", "end")
        self.running = True
        self.run_button.config(state="disabled")
        self.reoptimize_button.config(state="disabled")
        self.status_var.set("Re-optimising (no network)...")

        thread = threading.Thread(
            target=self._reoptimize_worker,
            args=(self.last_resolved_want, self.last_players, self.last_listings,
                  self.last_sellers, max_sellers, self.sweep_to_var.get(), pickup,
                  self.use_precons_var.get()),
            daemon=True)
        thread.start()

    def _reoptimize_worker(self, resolved_want, players, listings, sellers,
                           max_sellers, sweep_to, pickup, use_precons) -> None:
        writer = _QueueWriter(self.log_queue)
        old_stdout = sys.stdout
        sys.stdout = writer
        try:
            fao.solve_and_report(resolved_want, players, listings, sellers,
                                 max_sellers=max_sellers, sweep_to=sweep_to, pickup=pickup,
                                 use_precons=use_precons)
        except Exception as e:
            print(f"\nERROR: {e}")
        finally:
            sys.stdout = old_stdout
            self.log_queue.put("\n__DONE__")

    def _drain_log_queue(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line == "\n__DONE__":
                    self.running = False
                    self.run_button.config(state="normal")
                    self.reoptimize_button.config(
                        state="normal" if self.last_listings is not None else "disabled")
                    self.status_var.set("Done.")
                    continue
                self.output.insert("end", line)
                self.output.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def save_output(self) -> None:
        content = self.output.get("1.0", "end")
        if not content.strip():
            messagebox.showinfo("Nothing to save", "Run it first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                            filetypes=[("Text files", "*.txt")])
        if not path:
            return
        with open(path, "w") as f:
            f.write(content)


class _QueueWriter:
    """Makes a queue.Queue look enough like a file object for print() to
    use as sys.stdout -- the worker thread writes, the main thread drains
    it into the Text widget (Tkinter widgets aren't thread-safe to touch
    directly from a background thread)."""

    def __init__(self, q: "queue.Queue[str]"):
        self.q = q

    def write(self, s: str) -> None:
        if s:
            self.q.put(s)

    def flush(self) -> None:
        pass


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
