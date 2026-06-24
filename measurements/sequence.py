"""Serializable sequence specs — the composable orchestration tree (spec 03).

Roadmap step 4, increment 1.  These are the *declarative* counterpart of the
runtime executors (``measurements/executors.py``): pure, Qt-free dataclasses that
live in the Session and describe the *shape* of an acquisition run — repeat
forever, run N times, walk a list of steps in order — without any engine logic.

The tree mirrors the instrument layer exactly: ``StepSpec`` / ``SequenceSpec`` /
``LoopSpec`` are to ``build_executor`` what ``SourceSpec`` is to
``build_instrument``.  A ``StepSpec`` *references* a ``RouteStep`` by its label;
the routing content stays in ``matrix`` / ``routes`` and never moves into the
tree (invariant 2: geometries are presets, not engine branches).

Serialization is a recursive JSON object discriminated by a ``"type"`` key
(``"step"`` / ``"sequence"`` / ``"loop"``).  The field is **optional** on the
Session (``sequence: NodeSpec | None``, default ``None``) and the default tree is
synthesized at *runtime* from the flat fields when it is absent — so no
``SCHEMA_VERSION`` bump is needed (see spec 03, "Schema: additive").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from measurements.routing import RouteStep


@dataclass
class StepSpec:
    """One acquisition step: optionally route, (re)settle, read, emit a row."""

    route: str | None = None          # references a RouteStep by label; None -> static route
    current_reversal: bool = False    # per-step (was a global worker kwarg)
    settle_s: float | None = None     # per-step re-settle after a route change; None -> none
    strategy: str = "point"           # "point" (today) | "stream" (later increment)


@dataclass
class SequenceSpec:
    """A list of children run in order, optionally emitting a cross-step row."""

    children: list["NodeSpec"] = field(default_factory=list)
    cross_derived: bool = False       # emit the cross-step derived (vdP R_sheet, …) per cycle


@dataclass
class LoopSpec:
    """Repeat a child: forever, a fixed count, or (increment 2) a sweep."""

    child: "NodeSpec" = None  # type: ignore[assignment]
    kind: str = "forever"            # "forever" | "count" | "sweep"
    count: int | None = None          # for kind == "count"
    # interval_s: see the migration debt below.  In increment 1 this is plumbed
    # *per-step* (consumed by StepExecutor) for byte-identical parity with the
    # current per-step pacing — NOT a per-loop inter-iteration wait yet.
    interval_s: float = 0.0
    axis: str | None = None           # for kind == "sweep": the source *role* to sweep
    values: list[float] | None = None  # for kind == "sweep": amplitudes applied per iteration


NodeSpec = Union[StepSpec, SequenceSpec, LoopSpec]


# ── serialisation (recursive, discriminated by "type") ────────────────────────

def sequence_to_dict(node: NodeSpec | None) -> dict | None:
    """Serialise a sequence tree to a JSON-ready dict (or None when absent).

    Default-valued fields are omitted to keep files compact and to match the
    serialization examples in spec 03; ``sequence_from_dict`` restores the same
    defaults, so the round-trip is exact.
    """
    if node is None:
        return None
    if isinstance(node, StepSpec):
        d: dict = {"type": "step"}
        if node.route is not None:
            d["route"] = node.route
        if node.current_reversal:
            d["current_reversal"] = True
        if node.settle_s is not None:
            d["settle_s"] = node.settle_s
        if node.strategy != "point":
            d["strategy"] = node.strategy
        return d
    if isinstance(node, SequenceSpec):
        return {
            "type": "sequence",
            "cross_derived": node.cross_derived,
            "children": [sequence_to_dict(c) for c in node.children],
        }
    if isinstance(node, LoopSpec):
        d = {
            "type": "loop",
            "kind": node.kind,
            "interval_s": node.interval_s,
            "child": sequence_to_dict(node.child),
        }
        if node.count is not None:
            d["count"] = node.count
        if node.axis is not None:
            d["axis"] = node.axis
        if node.values is not None:
            d["values"] = list(node.values)
        return d
    raise TypeError(f"not a sequence node: {node!r}")


def sequence_from_dict(d: dict | None) -> NodeSpec | None:
    """Reconstruct a sequence tree from a dict (inverse of ``sequence_to_dict``)."""
    if d is None:
        return None
    t = d.get("type")
    if t == "step":
        return StepSpec(
            route=d.get("route"),
            current_reversal=d.get("current_reversal", False),
            settle_s=d.get("settle_s"),
            strategy=d.get("strategy", "point"),
        )
    if t == "sequence":
        return SequenceSpec(
            children=[sequence_from_dict(c) for c in d.get("children", [])],
            cross_derived=d.get("cross_derived", False),
        )
    if t == "loop":
        return LoopSpec(
            child=sequence_from_dict(d.get("child")),
            kind=d.get("kind", "forever"),
            count=d.get("count"),
            interval_s=d.get("interval_s", 0.0),
            axis=d.get("axis"),
            values=d.get("values"),
        )
    raise ValueError(f"unknown sequence node type: {t!r}")


# ── default-tree synthesis (the parity mechanism) ─────────────────────────────

def synthesize_default_sequence(
    steps: list[RouteStep] | None,
    *,
    settle_s: float,
    interval_s: float,
    current_reversal: bool,
    has_cross: bool,
) -> LoopSpec:
    """Build the default tree from the flat fields when ``Session.sequence`` is None.

    Reproduces the current ``AcquisitionWorker`` shape exactly:

        Loop(forever, interval_s) -> Sequence([Step per RouteStep], cross_derived)

    The single subtlety that the parity (and, worst case, the readings) hinges on
    is the per-step *re-settle*: today it fires only when ``multi = len(steps) >
    1`` (re-settle the lock-in after a route change *between* steps), never for a
    single step or a static route — the initial settle already covers those.  So
    ``settle_s`` is distributed onto the Steps **iff** ``multi`` was True; a
    single-step or static run gets ``settle_s=None`` (no re-settle).  Geometry
    presets reuse this helper, so a preset emits a Sequence subtree, not a flat
    list (invariant 2).
    """
    if steps is None:
        # static single route: no "step" column, no re-settle, no cross
        children: list[NodeSpec] = [
            StepSpec(route=None, current_reversal=current_reversal, settle_s=None)
        ]
        cross_derived = False
    else:
        multi = len(steps) > 1
        children = [
            StepSpec(
                route=s.label,
                current_reversal=current_reversal,
                settle_s=settle_s if multi else None,
            )
            for s in steps
        ]
        cross_derived = has_cross
    return LoopSpec(
        child=SequenceSpec(children=children, cross_derived=cross_derived),
        kind="forever",
        interval_s=interval_s,
    )
