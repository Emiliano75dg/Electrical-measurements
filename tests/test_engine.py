"""Acquisition engine logic with fake channels — no Qt event loop, no hardware.

These exercise the pure-Python row/column construction and the current-reversal
antisymmetrisation through read_single(), which configures+reads once without
starting the QThread.
"""

import math
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from core.channels import Func, MeterConfig, Reading, SourceConfig
from core.derived import resistance, vanderpauw_sheet
from measurements.engine import COMBINED_LABEL, AcquisitionWorker
from measurements.routing import MatrixLayout, RouteStep


class FakeSource:
    def __init__(self, sid="S1", config=None):
        self.id = sid
        self.config = config or SourceConfig(amplitude=1e-3)

    def configure(self, cfg):
        self.config = cfg

    def enable(self):
        pass

    def disable(self):
        pass


class FakeMeter:
    """Reads (signed source amplitude * resistance) + a constant offset.

    The offset models a current-independent error (thermal EMF) that current
    reversal must reject.
    """

    def __init__(self, source, resistance_ohm, offset=0.0, lockin=False, mid="V", reference="S1"):
        self.id = mid
        self._src = source
        self._r = resistance_ohm
        self._offset = offset
        self.config = MeterConfig(lockin=lockin, nplc=1.0, time_constant_s=0.001,
                                  reference=reference)

    def configure(self, cfg):
        self.config = cfg

    def read(self):
        v = self._src.config.amplitude * self._r + self._offset
        if self.config.lockin:
            return Reading(x=v, y=0.0, unit="V")
        return Reading(dc=v, unit="V")


def _worker(sources, meters, **kw):
    return AcquisitionWorker(sources, meters, Path("/tmp/unused.csv"), **kw)


# ── dynamic columns ──────────────────────────────────────────────────────────

def test_columns_lockin_vs_dc():
    s = FakeSource()
    lock = FakeMeter(s, 100, lockin=True, mid="Vxx")
    dc = FakeMeter(s, 100, lockin=False, mid="Vdc")
    w = _worker([s], [lock, dc], derived=[resistance("R", "Vxx")])
    assert w.columns() == ["time_s", "Vxx_X", "Vxx_Y", "Vdc_DC", "R"]


def test_columns_include_step_when_steps_present():
    s = FakeSource()
    m = FakeMeter(s, 100, mid="V")
    w = _worker([s], [m], steps=[RouteStep("a", []), RouteStep("b", [])])
    assert "step" in w.columns()


def test_columns_no_step_when_static():
    s = FakeSource()
    m = FakeMeter(s, 100, mid="V")
    assert "step" not in _worker([s], [m]).columns()


# ── single read / row building ───────────────────────────────────────────────

def test_read_single_static_recovers_resistance():
    s = FakeSource(config=SourceConfig(amplitude=1e-3))
    m = FakeMeter(s, 120.0, lockin=True, mid="Vxx")
    w = _worker([s], [m], derived=[resistance("Rxx", "Vxx")])
    row = w.read_single()
    assert row["Vxx_X"] == pytest.approx(0.12)
    assert row["Rxx"] == pytest.approx(120.0)
    assert "time_s" in row


def test_current_reversal_rejects_offset():
    # with a 50 mV current-independent offset, plain read sees R+offset/I,
    # reversal recovers the true R.
    amp = 1e-3
    s = FakeSource(config=SourceConfig(amplitude=amp))
    m = FakeMeter(s, 120.0, offset=0.05, lockin=False, mid="V")

    no_rev = _worker([s], [m], current_reversal=False).read_single()
    assert no_rev["V_DC"] == pytest.approx(amp * 120.0 + 0.05)

    s2 = FakeSource(config=SourceConfig(amplitude=amp))
    m2 = FakeMeter(s2, 120.0, offset=0.05, lockin=False, mid="V")
    rev = _worker([s2], [m2], current_reversal=True).read_single()
    assert rev["V_DC"] == pytest.approx(amp * 120.0)   # offset gone


def test_current_reversal_restores_positive_sign():
    s = FakeSource(config=SourceConfig(amplitude=1e-3))
    m = FakeMeter(s, 120.0, lockin=False, mid="V")
    _worker([s], [m], current_reversal=True).read_single()
    assert s.config.amplitude > 0    # source left at +|amplitude|


def test_current_reversal_leaves_voltage_source_untouched():
    si = FakeSource("S1", SourceConfig(func=Func.I_AC, amplitude=1e-3))
    sv = FakeSource("S2", SourceConfig(func=Func.V_DC, amplitude=0.5))
    m = FakeMeter(si, 120.0, lockin=False, mid="V")   # normalised by the current source
    _worker([si, sv], [m], current_reversal=True).read_single()
    assert sv.config.amplitude == 0.5   # voltage source not flipped
    assert si.config.amplitude > 0      # current source restored to +


# ── multi-source normalisation ───────────────────────────────────────────────

def test_multi_source_resistance_normalised_per_source():
    s1 = FakeSource("S1", SourceConfig(func=Func.I_AC, amplitude=1e-3))
    s2 = FakeSource("S2", SourceConfig(func=Func.I_AC, amplitude=2e-3))
    a = FakeMeter(s1, 100.0, lockin=True, mid="A", reference="S1")
    b = FakeMeter(s2, 100.0, lockin=True, mid="B", reference="S2")
    w = _worker([s1, s2], [a, b],
                derived=[resistance("RA", "A"), resistance("RB", "B")])
    row = w.read_single()
    # each meter divided by its OWN source: both recover 100 Ω despite 1× vs 2× drive
    assert row["RA"] == pytest.approx(100.0)
    assert row["RB"] == pytest.approx(100.0)


# ── cross-step ───────────────────────────────────────────────────────────────

def test_read_single_cross_step_combined_row():
    s = FakeSource(config=SourceConfig(amplitude=1e-3))
    m = FakeMeter(s, 120.0, lockin=False, mid="V")
    steps = [RouteStep("a", []), RouteStep("b", [])]
    cross = [vanderpauw_sheet("R_sheet", "a", "b", "V")]
    w = _worker([s], [m], steps=steps, cross_derived=cross)
    row = w.read_single()
    assert row["step"] == COMBINED_LABEL
    assert row["R_sheet"] == pytest.approx(math.pi * 120.0 / math.log(2.0), rel=1e-9)


# ── routed-only interlock (spec 03, increment 2) ──────────────────────────────

class RecordingSource:
    """Counts disable()/enable() so the interlock can be observed per step."""

    def __init__(self, sid, config=None):
        self.id = sid
        self.config = config or SourceConfig(amplitude=1e-3)
        self.disabled = 0
        self.enabled = 0

    def configure(self, cfg): self.config = cfg
    def enable(self): self.enabled += 1
    def disable(self): self.disabled += 1


class FakeMatrix:
    settle_s = 0.0
    def __init__(self): self.opened = 0
    def open_all(self): self.opened += 1
    def close(self, channels): pass


def test_route_leaves_fixed_source_totally_untouched_across_steps():
    # A gate driven by the external SMU (id "SMU2") wired outside the matrix:
    # across two route steps it must be neither disabled NOR re-enabled (no yo-yo),
    # while the routed M81 source (id "S1") is cycled each step.
    routed = RecordingSource("S1")
    gate = RecordingSource("SMU2")          # external-SMU style id, FIXED
    m = FakeMeter(routed, 100.0, mid="V")
    w = _worker([routed, gate], [m], matrix=FakeMatrix(), layout=MatrixLayout(),
                steps=[RouteStep("a", []), RouteStep("b", [])],
                fixed_source_ids={"SMU2"})

    w._route(RouteStep("a", []))
    w._route(RouteStep("b", []))

    assert (routed.disabled, routed.enabled) == (2, 2)   # cycled every step
    assert (gate.disabled, gate.enabled) == (0, 0)       # never touched: no yo-yo


def test_route_cycles_all_sources_by_default_identical_to_step3():
    # Empty fixed set (the default) -> every source disabled+re-enabled, exactly
    # the step-3 interlock.
    a = RecordingSource("S1")
    b = RecordingSource("S2")
    w = _worker([a, b], [FakeMeter(a, 100.0, mid="V")],
                matrix=FakeMatrix(), layout=MatrixLayout(), steps=[RouteStep("a", [])])

    w._route(RouteStep("a", []))

    assert (a.disabled, a.enabled) == (1, 1)
    assert (b.disabled, b.enabled) == (1, 1)


# ── sweep by role (spec 03, increment 2) ──────────────────────────────────────

def test_set_sweep_axis_sets_amplitude_by_role():
    gate = RecordingSource("SMU1")
    w = _worker([gate], [FakeMeter(gate, 100.0, mid="V")], source_roles={"SMU1": "gate"})

    w._set_sweep_axis("gate", -7.5)

    assert gate.config.amplitude == -7.5
    assert gate.enabled >= 1                 # re-enabled with the new setpoint


def test_set_sweep_axis_rejects_unresolved_role():
    gate = RecordingSource("SMU1")
    w = _worker([gate], [FakeMeter(gate, 100.0, mid="V")], source_roles={"SMU1": "gate"})
    with pytest.raises(ValueError):
        w._set_sweep_axis("nonexistent", 1.0)
