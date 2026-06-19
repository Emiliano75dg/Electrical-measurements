"""Matrix routing model — single-pole Keithley 7709 (REDESIGN.md §2.2, §3.2).

Decision 2026-06-13: the 7709 is a *pure router* of sample contacts, used in a
single-pole scheme — only the HI pin of each port is wired, so the mechanically
2-pole 6×8 crosspoint behaves as a single-pole 6×8:

    rows    = single instrument conductors  (max 6 routed)
    columns = single sample contacts        (max 8)
    crosspoint (r, c) = links one conductor to one contact

This module is hardware-agnostic: it turns a declarative layout + route steps
into lists of 7709 channel numbers.  The driver (instruments/matrix7709.py)
only ever receives channel-number lists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


def xpt(row: int, col: int) -> int:
    """Crosspoint (row, col) → 7709 channel number.  channel = (row-1)*8 + col."""
    return (row - 1) * 8 + col


class TermMode(str, Enum):
    ROUTED = "routed"   # occupies a matrix row; routable to any column
    FIXED = "fixed"     # wired outside the matrix (shared common or fixed contact)


@dataclass
class TerminalBinding:
    terminal_id: str                       # "Vxx+", "Vxx-", "I+", …
    mode: TermMode = TermMode.ROUTED
    row: int | None = None                 # if ROUTED: matrix row 1-6
    fixed_to: str | None = None            # if FIXED: common/contact name (doc only)


@dataclass
class MatrixLayout:
    """Maps routed terminals to matrix rows and sample contacts to columns."""

    terminal_row: dict[str, int] = field(default_factory=dict)   # ROUTED terminals → row 1-6
    contact_col: dict[str, int] = field(default_factory=dict)    # contact → column 1-8

    def validate(self) -> None:
        if len(self.terminal_row) > 6:
            raise ValueError(f"max 6 rows (ROUTED terminals), got {len(self.terminal_row)}")
        if len(self.contact_col) > 8:
            raise ValueError(f"max 8 columns (contacts), got {len(self.contact_col)}")
        for t, r in self.terminal_row.items():
            if not 1 <= r <= 6:
                raise ValueError(f"row of '{t}' = {r} out of range 1-6")
        for c, col in self.contact_col.items():
            if not 1 <= col <= 8:
                raise ValueError(f"column of '{c}' = {col} out of range 1-8")
        rows = list(self.terminal_row.values())
        if len(set(rows)) != len(rows):
            raise ValueError("two ROUTED terminals share the same row")
        cols = list(self.contact_col.values())
        if len(set(cols)) != len(cols):
            raise ValueError("two contacts share the same column")


@dataclass
class RouteStep:
    """One matrix configuration: a set of (routed terminal, contact) links."""

    label: str
    links: list[tuple[str, str]] = field(default_factory=list)   # (terminal_id, contact_id)

    def channels(self, layout: MatrixLayout) -> list[int]:
        """Resolve this step's links to 7709 channel numbers via the layout."""
        chans: list[int] = []
        for term, contact in self.links:
            if term not in layout.terminal_row:
                raise KeyError(f"terminal '{term}' not routed (missing from terminal_row)")
            if contact not in layout.contact_col:
                raise KeyError(f"contact '{contact}' not mapped (missing from contact_col)")
            chans.append(xpt(layout.terminal_row[term], layout.contact_col[contact]))
        return chans


# ── presets ───────────────────────────────────────────────────────────────────

def hall_routing() -> tuple[MatrixLayout, list[RouteStep]]:
    """Hall bar: I source + Vxx + Vxy, with contact C2 shared (Vxx/Vxy)."""
    layout = MatrixLayout(
        terminal_row={"I+": 3, "I-": 4, "Vxx+": 5, "Vxx-": 6, "Vxy+": 1, "Vxy-": 2},
        contact_col={"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5},
    )
    steps = [
        RouteStep("hall", [
            ("I+", "C1"), ("I-", "C4"),
            ("Vxx+", "C2"), ("Vxx-", "C3"),
            ("Vxy+", "C2"), ("Vxy-", "C5"),
        ])
    ]
    return layout, steps


def vanderpauw_routing() -> tuple[MatrixLayout, list[RouteStep]]:
    """Van der Pauw on a 4-contact square: rotate I/V around C1–C4.

    Two configurations whose sheet resistance combine via the vdP equation:
      R1 = R_12,43  (I: C1→C2, V: C4→C3)
      R2 = R_23,14  (I: C2→C3, V: C1→C4)
    """
    layout = MatrixLayout(
        terminal_row={"I+": 3, "I-": 4, "V+": 5, "V-": 6},
        contact_col={"C1": 1, "C2": 2, "C3": 3, "C4": 4},
    )
    steps = [
        RouteStep("R_12_43", [("I+", "C1"), ("I-", "C2"), ("V+", "C4"), ("V-", "C3")]),
        RouteStep("R_23_14", [("I+", "C2"), ("I-", "C3"), ("V+", "C1"), ("V-", "C4")]),
    ]
    return layout, steps
