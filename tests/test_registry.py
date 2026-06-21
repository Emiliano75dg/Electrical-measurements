"""Instrument registry: capability factories, binding resolution, M81 parity.

These are Qt-free (the registry and the M81/7709 facades do not need PySide6);
only the engine-column parity test imports the worker and skips without it.
"""

import pytest

from core.channels import MeterConfig, SourceChannel, SourceConfig
from instruments.m81 import M81Instrument
from instruments.m81_channels import M81Meter, M81SMUMeter, M81Source
from instruments.matrix7709 import Matrix7709
from instruments.registry import (
    DEFAULT_M81_ID,
    Keithley7709LabInstrument,
    M81LabInstrument,
    Registry,
    Router,
    UnknownInstrumentError,
)


def _m81() -> M81Instrument:
    # No connect() needed: the adapters only call the facade on configure/read,
    # and resolution/column tests never read.
    return M81Instrument("0.0.0.0", simulated=True)


# ── M81 entry: factories reproduce the old _build_channels selection ──────────

def test_m81_make_source_returns_m81_source():
    inst = M81LabInstrument(_m81())
    src = inst.make_source(1, SourceConfig())
    assert isinstance(src, M81Source)
    assert src.id == "S1"


def test_m81_make_meter_lockin_dc_smu_selection():
    inst = M81LabInstrument(_m81())
    lock = inst.make_meter(1, MeterConfig(lockin=True), "Vxx")
    dc = inst.make_meter(2, MeterConfig(lockin=False), "Vdc")
    smu = inst.make_meter(3, MeterConfig(lockin=False, smu=True), "Ig")
    assert isinstance(lock, M81Meter) and not isinstance(lock, M81SMUMeter)
    assert isinstance(dc, M81Meter)
    assert isinstance(smu, M81SMUMeter)
    assert (lock.id, dc.id, smu.id) == ("Vxx", "Vdc", "Ig")


def test_m81_offers_no_router_or_environment():
    inst = M81LabInstrument(_m81())
    assert inst.router() is None
    assert inst.environment() is None


# ── binding resolution through the registry ───────────────────────────────────

def test_none_binding_falls_back_to_default_m81():
    reg = Registry()
    reg.add(M81LabInstrument(_m81(), instrument_id=DEFAULT_M81_ID))
    src = reg.resolve_source(None, 1, SourceConfig())
    meter = reg.resolve_meter(None, 1, MeterConfig(lockin=True), "V")
    assert isinstance(src, M81Source)
    assert isinstance(meter, M81Meter)


def test_explicit_instrument_id_resolves():
    reg = Registry()
    reg.add(M81LabInstrument(_m81(), instrument_id="m81_main"))
    assert reg.resolve_source("m81_main", 2, SourceConfig()).id == "S2"


def test_unknown_instrument_raises():
    reg = Registry()
    reg.add(M81LabInstrument(_m81()))
    with pytest.raises(UnknownInstrumentError):
        reg.resolve_source("nope", 1, SourceConfig())


def test_duplicate_id_rejected():
    reg = Registry()
    reg.add(M81LabInstrument(_m81()))
    with pytest.raises(ValueError):
        reg.add(M81LabInstrument(_m81()))   # same default id


# ── 7709 routing capability ───────────────────────────────────────────────────

def test_7709_exposes_router_only():
    matrix = Matrix7709(simulated=True)
    inst = Keithley7709LabInstrument(matrix)
    assert inst.make_source(1, SourceConfig()) is None
    assert inst.make_meter(1, MeterConfig(), "V") is None
    router = inst.router()
    assert router is matrix
    assert isinstance(router, Router)   # Matrix7709 structurally satisfies Router


def test_registry_collects_routers():
    reg = Registry()
    reg.add(M81LabInstrument(_m81()))
    reg.add(Keithley7709LabInstrument(Matrix7709(simulated=True)))
    assert len(reg.routers()) == 1


def test_resolve_source_on_router_only_instrument_rejected():
    reg = Registry()
    reg.add(Keithley7709LabInstrument(Matrix7709(simulated=True), instrument_id="matrix"))
    with pytest.raises(ValueError, match="no source"):
        reg.resolve_source("matrix", 1, SourceConfig())


# ── a second, stub instrument resolves a binding (driver may be a stub) ────────

class _StubSource:
    def __init__(self, port, cfg):
        self.id = f"G{port}"
        self.config = cfg

    def configure(self, cfg):
        self.config = cfg

    def enable(self):
        ...

    def disable(self):
        ...


class _StubInstrument:
    type = "stub_gate"

    def __init__(self, instrument_id="gate"):
        self.id = instrument_id

    def connect(self):
        ...

    def disconnect(self):
        ...

    @property
    def connected(self):
        return True

    def make_source(self, port, cfg):
        return _StubSource(port, cfg)

    def make_meter(self, port, cfg, meter_id):
        return None

    def router(self):
        return None

    def environment(self):
        return None


def test_second_stub_instrument_resolves_binding():
    reg = Registry()
    reg.add(M81LabInstrument(_m81()))
    reg.add(_StubInstrument("gate"))
    src = reg.resolve_source("gate", 1, SourceConfig())
    assert isinstance(src, _StubSource)
    assert src.id == "G1"
    assert isinstance(src, SourceChannel)   # satisfies the engine's Protocol


# ── parity: registry-built M81 channels drive the same engine columns ─────────

def test_gui_parity_registry_channels_drive_engine_columns():
    pytest.importorskip("PySide6")
    from pathlib import Path

    from core.derived import resistance
    from measurements.engine import AcquisitionWorker

    reg = Registry()
    reg.add(M81LabInstrument(_m81()))
    # mimic the Channels tab: one current source, a lock-in Vxx and an SMU Ig
    sources = [reg.resolve_source(None, 1, SourceConfig())]
    meters = [
        reg.resolve_meter(None, 1, MeterConfig(lockin=True), "Vxx"),
        reg.resolve_meter(None, 3, MeterConfig(lockin=False, smu=True), "Ig"),
    ]
    worker = AcquisitionWorker(
        sources, meters, Path("/tmp/parity.csv"), derived=[resistance("Rxx", "Vxx")]
    )
    # identical to the pre-registry direct path: lock-in -> X/Y, SMU -> DC
    assert worker.columns() == ["time_s", "Vxx_X", "Vxx_Y", "Ig_DC", "Rxx"]
    assert isinstance(meters[1], M81SMUMeter)
