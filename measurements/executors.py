"""Runtime executors — the orchestration tree the runner shell walks (spec 03).

Roadmap step 4, increment 1.  These are the runtime counterpart of the
serializable specs (``measurements/sequence.py``): ``StepExecutor`` /
``SequenceExecutor`` / ``LoopExecutor`` share a single ``run(ctx)`` interface and
are built from a spec tree by ``build_executor`` — the orchestration counterpart
of ``build_instrument``.

The executors hold **no** acquisition logic of their own.  The per-step
primitives (``_route`` / ``_acquire_readings`` / ``_build_row`` /
``_build_cross_row``) stay on ``AcquisitionWorker`` — the runner shell — and are
handed to the executors through ``RunContext`` as plain callables.  Crucially the
reads still go through the shell's ``acquire`` callable, which acquires the
**same lock** the shell uses for live reconfiguration: the executors never touch
the lock, so the "live edits apply under the same lock as the reads" guarantee is
preserved unchanged (spec 03, "Live config update is re-homed, not lost").

This keeps the engine instrument-agnostic (invariant 1): an executor knows
"route, settle, read, emit, pace" as abstract steps and nothing about an SMU.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.channels import Reading
from core.derived import CrossStepQuantity
from measurements.routing import RouteStep
from measurements.sequence import LoopSpec, NodeSpec, SequenceSpec, StepSpec

# readings of one step (meter id -> Reading); a cycle maps step label -> that.
StepReadings = dict[str, Reading]
Cycle = dict[str, StepReadings]


@dataclass
class RunContext:
    """The seam between the runner shell and the executors.

    A bundle of the shell's primitives (as callables) plus the run-wide data the
    executors need.  Assembled once by ``AcquisitionWorker.run`` and passed down
    the tree.  ``acquire`` is the shell's locked read; ``route`` / ``build_row`` /
    ``build_cross_row`` are the lifted per-step primitives; ``publish`` writes a
    row to CSV and emits it.
    """

    route: Callable[[RouteStep], None]
    acquire: Callable[[bool], StepReadings]
    build_row: Callable[[StepReadings, str | None], dict]
    build_cross_row: Callable[[Cycle], dict]
    publish: Callable[[dict], None]
    sleep_ms: Callable[[float], None]
    is_running: Callable[[], bool]
    now: Callable[[], float]
    resolve_route: Callable[[str], RouteStep | None]
    cross: list[CrossStepQuantity]
    has_step_column: bool
    default_settle_s: float
    interval_s: float
    on_cross_error: Callable[[Exception], None] | None = None


class StepExecutor:
    """Route (if any), re-settle, read, emit a row, and pace the step.

    Byte-identical to the body of the current ``for step in steps`` loop: the
    pacing pads each step to ``ctx.interval_s`` measured from before the route
    (the per-step interval — see the migration debt in spec 03), and the
    re-settle fires only when ``spec.settle_s`` is set (the ``multi`` case).
    """

    def __init__(self, spec: StepSpec) -> None:
        self.spec = spec

    def run(self, ctx: RunContext) -> Cycle:
        start = ctx.now()
        route: RouteStep | None = None
        if self.spec.route is not None:
            route = ctx.resolve_route(self.spec.route)
            if route is not None:
                ctx.route(route)
        # re-settle the lock-in after a route change between steps (multi only)
        if self.spec.settle_s and self.spec.settle_s > 0:
            ctx.sleep_ms(self.spec.settle_s * 1000)
            if not ctx.is_running():
                return {}
        readings = ctx.acquire(self.spec.current_reversal)
        label = route.label if (route is not None and ctx.has_step_column) else None
        ctx.publish(ctx.build_row(readings, label))
        # per-step pacing: pad to interval_s, measured from before the route
        remaining_ms = (ctx.interval_s - (ctx.now() - start)) * 1000
        if remaining_ms > 0:
            ctx.sleep_ms(remaining_ms)
        return {route.label: readings} if route is not None else {}


class SequenceExecutor:
    """Run children in order, then emit the cross-step row for the cycle."""

    def __init__(self, spec: SequenceSpec, children: list["Executor"]) -> None:
        self.spec = spec
        self.children = children

    def run(self, ctx: RunContext) -> Cycle:
        cycle: Cycle = {}
        for child in self.children:
            if not ctx.is_running():
                break
            cycle.update(child.run(ctx) or {})
        if self.spec.cross_derived and ctx.cross and ctx.is_running() and cycle:
            try:
                ctx.publish(ctx.build_cross_row(cycle))
            except Exception as exc:
                if ctx.on_cross_error is not None:
                    ctx.on_cross_error(exc)
                else:
                    raise
        return cycle


class LoopExecutor:
    """Repeat a child forever or a fixed count (sweep -> increment 2)."""

    def __init__(self, spec: LoopSpec, child: "Executor") -> None:
        self.spec = spec
        self.child = child

    def run(self, ctx: RunContext) -> None:
        # The loop owns the interval; thread it onto the context for its subtree.
        # In increment 1 this is consumed per-step by StepExecutor (parity); the
        # per-loop inter-iteration semantics are the named future migration.
        ctx.interval_s = self.spec.interval_s
        if self.spec.kind == "forever":
            while ctx.is_running():
                self.child.run(ctx)
        elif self.spec.kind == "count":
            for _ in range(self.spec.count or 0):
                if not ctx.is_running():
                    break
                self.child.run(ctx)
        elif self.spec.kind == "sweep":
            raise NotImplementedError("sweep loop arrives in increment 2")
        else:
            raise ValueError(f"unknown loop kind: {self.spec.kind!r}")
        return None


Executor = StepExecutor | SequenceExecutor | LoopExecutor


def build_executor(spec: NodeSpec, ctx: RunContext) -> Executor:
    """Build an executor tree from a spec tree (counterpart of build_instrument)."""
    if isinstance(spec, StepSpec):
        return StepExecutor(spec)
    if isinstance(spec, SequenceSpec):
        return SequenceExecutor(spec, [build_executor(c, ctx) for c in spec.children])
    if isinstance(spec, LoopSpec):
        return LoopExecutor(spec, build_executor(spec.child, ctx))
    raise TypeError(f"not a sequence node: {spec!r}")
