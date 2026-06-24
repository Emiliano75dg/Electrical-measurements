"""Executor tree: build_executor, walking order, loops, and end-to-end parity.

The unit tests drive the executors through a hand-built ``RunContext`` (no Qt, no
worker).  The parity test drives the *real* ``AcquisitionWorker`` primitives
through the synthesized tree and asserts the emitted rows match the previous
flat-field behaviour.
"""

import math
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from core.channels import MeterConfig, Reading, SourceConfig
from core.derived import vanderpauw_sheet
from measurements.engine import COMBINED_LABEL, AcquisitionWorker
from measurements.executors import (
    LoopExecutor,
    RunContext,
    SequenceExecutor,
    StepExecutor,
    build_executor,
)
from measurements.routing import RouteStep
from measurements.sequence import LoopSpec, SequenceSpec, StepSpec, synthesize_default_sequence


# ── fakes (mirror tests/test_engine.py) ───────────────────────────────────────

class FakeSource:
    def __init__(self, sid="S1", config=None):
        self.id = sid
        self.config = config or SourceConfig(amplitude=1e-3)

    def configure(self, cfg): self.config = cfg
    def enable(self): pass
    def disable(self): pass


class FakeMeter:
    def __init__(self, source, resistance_ohm, mid="V", reference="S1"):
        self.id = mid
        self._src = source
        self._r = resistance_ohm
        self.config = MeterConfig(lockin=False, nplc=1.0, time_constant_s=0.001,
                                  reference=reference)

    def configure(self, cfg): self.config = cfg
    def read(self): return Reading(dc=self._src.config.amplitude * self._r, unit="V")


def _fake_ctx(published, *, is_running=lambda: True, cross=None, on_cross_error=None,
              build_row=None, build_cross_row=None, acquire=None):
    """A RunContext wired to record published rows, no hardware."""
    return RunContext(
        route=lambda step: None,
        acquire=acquire or (lambda rev: {}),
        build_row=build_row or (lambda readings, label: {"step": label}),
        build_cross_row=build_cross_row or (lambda cycle: {"step": COMBINED_LABEL}),
        publish=published.append,
        sleep_ms=lambda ms: None,
        is_running=is_running,
        now=lambda: 0.0,
        resolve_route=lambda label: RouteStep(label, []),
        cross=cross or [],
        has_step_column=True,
        default_settle_s=0.0,
        interval_s=0.0,
        on_cross_error=on_cross_error,
    )


# ── build_executor ────────────────────────────────────────────────────────────

def test_build_executor_maps_each_node_type():
    ctx = _fake_ctx([])
    assert isinstance(build_executor(StepSpec(), ctx), StepExecutor)
    seq = build_executor(SequenceSpec(children=[StepSpec(), StepSpec()]), ctx)
    assert isinstance(seq, SequenceExecutor)
    assert len(seq.children) == 2
    loop = build_executor(LoopSpec(child=StepSpec(), kind="count", count=1), ctx)
    assert isinstance(loop, LoopExecutor)
    assert isinstance(loop.child, StepExecutor)


# ── walking order ─────────────────────────────────────────────────────────────

def test_sequence_walks_children_in_order():
    published: list[dict] = []
    ctx = _fake_ctx(published)
    seq = build_executor(
        SequenceSpec(children=[StepSpec(route="a"), StepSpec(route="b"), StepSpec(route="c")]),
        ctx,
    )
    seq.run(ctx)
    assert [row["step"] for row in published] == ["a", "b", "c"]


# ── loops ─────────────────────────────────────────────────────────────────────

def test_forever_loop_runs_and_stops():
    calls = []
    # is_running True for the first 3 checks, then False -> the loop stops
    running = iter([True, True, True, False])
    ctx = _fake_ctx([], is_running=lambda: next(running))

    class Counter:
        def run(self, ctx): calls.append(1); return {}

    LoopExecutor(LoopSpec(child=StepSpec(), kind="forever"), Counter()).run(ctx)
    assert len(calls) == 3


def test_count_loop_runs_exactly_n():
    calls = []

    class Counter:
        def run(self, ctx): calls.append(1); return {}

    ctx = _fake_ctx([])
    LoopExecutor(LoopSpec(child=StepSpec(), kind="count", count=4), Counter()).run(ctx)
    assert len(calls) == 4


def test_sweep_loop_sets_axis_per_value_then_runs_child():
    applied: list[tuple[str, float]] = []
    runs: list[int] = []
    ctx = _fake_ctx([])
    ctx.sweep_axis = lambda axis, value: applied.append((axis, value))

    class Counter:
        def run(self, ctx): runs.append(1); return {}

    spec = LoopSpec(child=StepSpec(), kind="sweep", axis="gate", values=[-40.0, 0.0, 40.0])
    LoopExecutor(spec, Counter()).run(ctx)

    # axis set (by role) before each child run, once per value, in order
    assert applied == [("gate", -40.0), ("gate", 0.0), ("gate", 40.0)]
    assert len(runs) == 3


def test_sweep_without_resolver_raises():
    ctx = _fake_ctx([])           # _fake_ctx leaves sweep_axis = None
    with pytest.raises(RuntimeError):
        LoopExecutor(LoopSpec(child=StepSpec(), kind="sweep", axis="gate", values=[1.0]),
                     object()).run(ctx)


# ── cross-step emission ───────────────────────────────────────────────────────

def test_sequence_emits_cross_row_when_enabled():
    published: list[dict] = []
    ctx = _fake_ctx(published, cross=[object()])   # non-empty cross
    seq = build_executor(
        SequenceSpec(children=[StepSpec(route="a"), StepSpec(route="b")], cross_derived=True),
        ctx,
    )
    seq.run(ctx)
    assert [row["step"] for row in published] == ["a", "b", COMBINED_LABEL]


def test_cross_error_routed_to_handler_not_raised():
    errors = []
    ctx = _fake_ctx(
        [], cross=[object()],
        build_cross_row=lambda cycle: (_ for _ in ()).throw(RuntimeError("boom")),
        on_cross_error=errors.append,
    )
    seq = build_executor(
        SequenceSpec(children=[StepSpec(route="a")], cross_derived=True), ctx
    )
    seq.run(ctx)   # must not raise
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)


# ── end-to-end parity through the real worker primitives ──────────────────────

def _run_one_cycle(worker: AcquisitionWorker, steps, has_cross: bool) -> list[dict]:
    """Walk the synthesized tree for exactly one cycle and collect the rows."""
    worker._running = True
    worker._t0 = time.monotonic()
    published: list[dict] = []
    ctx = worker._make_context(published.append)
    seq = synthesize_default_sequence(
        steps, settle_s=worker._settle_s, interval_s=worker._interval_s,
        current_reversal=worker._current_reversal, has_cross=has_cross,
    ).child
    # one cycle, deterministically (the forever loop equivalent, bounded)
    build_executor(LoopSpec(child=seq, kind="count", count=1), ctx).run(ctx)
    return published


def test_parity_vdp_step_rows_and_combined():
    s = FakeSource(config=SourceConfig(amplitude=1e-3))
    m = FakeMeter(s, 120.0, mid="V")
    steps = [RouteStep("a", []), RouteStep("b", [])]
    cross = [vanderpauw_sheet("R_sheet", "a", "b", "V")]
    worker = AcquisitionWorker([s], [m], Path("/tmp/unused.csv"),
                               steps=steps, cross_derived=cross,
                               settle_s=0.0, interval_s=0.0)

    rows = _run_one_cycle(worker, steps, has_cross=True)

    assert [r["step"] for r in rows] == ["a", "b", COMBINED_LABEL]
    assert rows[0]["V_DC"] == pytest.approx(0.12)
    assert rows[1]["V_DC"] == pytest.approx(0.12)
    assert rows[2]["R_sheet"] == pytest.approx(math.pi * 120.0 / math.log(2.0), rel=1e-9)


def test_parity_static_run_has_no_step_column():
    s = FakeSource(config=SourceConfig(amplitude=1e-3))
    m = FakeMeter(s, 120.0, mid="V")
    worker = AcquisitionWorker([s], [m], Path("/tmp/unused.csv"),
                               settle_s=0.0, interval_s=0.0)

    rows = _run_one_cycle(worker, None, has_cross=False)

    assert len(rows) == 1
    assert "step" not in rows[0]
    assert rows[0]["V_DC"] == pytest.approx(0.12)
