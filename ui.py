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
"""

import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Dict, Optional

import decklist
import fetch_and_optimise as fao


class PlayerRow:
    """One row in the player list: a name, and a decklist -- either loaded
    from a file or typed/pasted in. The raw text is kept (not just the
    parsed want dict) so "Edit decklist" reopens showing exactly what's
    there now, editable in place, instead of an empty box you'd have to
    repaste the whole thing into just to tweak one line."""

    def __init__(self, parent: tk.Widget, on_remove):
        self.want: Optional[Dict[str, int]] = None
        self.raw_text: str = ""
        self.frame = ttk.Frame(parent)

        self.name_var = tk.StringVar(value="")
        ttk.Entry(self.frame, textvariable=self.name_var, width=14).pack(side="left", padx=(0, 6))

        self.status_var = tk.StringVar(value="no decklist loaded")
        ttk.Label(self.frame, textvariable=self.status_var, width=28,
                 foreground="#888888").pack(side="left", padx=(0, 6))

        ttk.Button(self.frame, text="Load .txt...", command=self._load_file).pack(side="left", padx=2)
        self.edit_button = ttk.Button(self.frame, text="Paste text...", command=self._edit_text)
        self.edit_button.pack(side="left", padx=2)
        ttk.Button(self.frame, text="Remove", command=lambda: on_remove(self)).pack(side="left", padx=(6, 0))

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)

    def destroy(self):
        self.frame.destroy()

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

        self._build_layout()
        self.add_player()  # start with one row -- single-deck mode by default
        self.root.after(100, self._drain_log_queue)

    # -- layout -------------------------------------------------------

    def _build_layout(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Players", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(top, text="One player = a normal run. Two or more = a group buy "
                            "(pooled purchase, cost + shipping split back out per person).",
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

        # -- run controls --
        run_frame = ttk.Frame(self.root, padding=(10, 0))
        run_frame.pack(fill="x")
        self.run_button = ttk.Button(run_frame, text="Run", command=self.run_clicked)
        self.run_button.pack(side="left")
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
        row = PlayerRow(self.players_frame, on_remove=self.remove_player)
        row.name_var.set(f"Player {len(self.player_rows) + 1}")
        row.pack(fill="x", pady=2)
        self.player_rows.append(row)

    def remove_player(self, row: PlayerRow) -> None:
        if len(self.player_rows) == 1:
            messagebox.showinfo("Can't remove", "Keep at least one player.")
            return
        row.destroy()
        self.player_rows.remove(row)

    # -- running ---------------------------------------------------------

    def run_clicked(self) -> None:
        if self.running:
            return

        loaded = [(r.name_var.get().strip(), r.want) for r in self.player_rows]
        if any(not name for name, _ in loaded):
            messagebox.showerror("Missing name", "Every player needs a name.")
            return
        if any(want is None for _, want in loaded):
            messagebox.showerror("Missing decklist",
                                 "Every player needs a decklist -- load a .txt or paste one in.")
            return
        names = [name for name, _ in loaded]
        if len(set(names)) != len(names):
            messagebox.showerror("Duplicate names", "Player names must be unique.")
            return

        max_sellers = None
        raw = self.max_sellers_var.get().strip()
        if raw:
            try:
                max_sellers = int(raw)
            except ValueError:
                messagebox.showerror("Invalid input", "'Also compare a seller cap' must be a number.")
                return

        offline = self.offline_path_var.get().strip() if self.offline_var.get() else None

        if len(loaded) == 1:
            want, players = loaded[0][1], None
        else:
            want, players = None, {name: w for name, w in loaded}

        self.output.delete("1.0", "end")
        self.running = True
        self.run_button.config(state="disabled")
        self.status_var.set("Running -- this can take a couple of minutes (live network fetch)...")

        thread = threading.Thread(target=self._run_worker,
                                  args=(want, players, offline, max_sellers,
                                        self.sweep_to_var.get()),
                                  daemon=True)
        thread.start()

    def _run_worker(self, want, players, offline, max_sellers, sweep_to) -> None:
        writer = _QueueWriter(self.log_queue)
        old_stdout = sys.stdout
        sys.stdout = writer
        try:
            fao.run(want=want, players=players, offline=offline,
                    max_sellers=max_sellers, sweep_to=sweep_to)
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
