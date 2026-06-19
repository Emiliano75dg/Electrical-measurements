"""Integration tests against the physics-aware M81 mock backend.

Skipped automatically when the sibling electrical_measurements package is not on
the path (instruments.m81._BACKEND_AVAILABLE is False).
"""

import time

import pytest

pytest.importorskip("PySide6")

from instruments import m81 as m81mod

pytestmark = pytest.mark.skipif(
    not m81mod._MOCK_AVAILABLE,
    reason="electrical_measurements mock backend (sibling project) not available",
)

from pathlib import Path

from PySide6.QtWidgets import QApplication

from core.channels import Func, MeterConfig, SourceConfig
from core.derived import resistance
from instruments.m81 import M81Instrument
from instruments.m81_channels import M81Meter, M81Source
from measurements.engine import AcquisitionWorker


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _build(field_t=0.0):
    inst = M81Instrument("0.0.0.0", simulated=True, field_t=field_t)
    inst.connect()
    src = M81Source(inst, 1, SourceConfig(func=Func.I_AC, amplitude=1e-3, frequency_Hz=17.77))
    vxx = M81Meter(inst, 1, MeterConfig(lockin=True), meter_id="Vxx")
    vxy = M81Meter(inst, 2, MeterConfig(lockin=True), meter_id="Vxy")
    return inst, src, vxx, vxy


def test_mock_is_vendored_in_tree():
    # the mock must come from the in-tree vendored copy, not the sibling project
    from instruments.m81 import MockM81Controller
    assert MockM81Controller.__module__.startswith("instruments._vendor")


def test_simulation_independent_of_hardware_backend(monkeypatch):
    # simulation must not depend on the real-hardware backend (lakeshore)
    monkeypatch.setattr(m81mod, "_HARDWARE_AVAILABLE", False)
    inst = M81Instrument("0.0.0.0", simulated=True)
    idn = inst.connect()
    inst.disconnect()
    assert "MOCK" in idn


def test_mock_recovers_longitudinal_resistance():
    _, src, vxx, _ = _build(field_t=0.0)
    w = AcquisitionWorker([src], [vxx], Path("/tmp/x.csv"),
                          derived=[resistance("Rxx", "Vxx")], settle_s=0.0)
    row = w.read_single()
    w.disable_sources()
    # mock longitudinal resistance ~ 120 Ω at zero field
    assert row["Rxx"] == pytest.approx(120.0, abs=5.0)


def test_mock_hall_resistance_tracks_field():
    _, src, _, vxy = _build(field_t=0.3)
    w = AcquisitionWorker([src], [vxy], Path("/tmp/x.csv"),
                          derived=[resistance("Rxy", "Vxy")], settle_s=0.0)
    row = w.read_single()
    w.disable_sources()
    # mock transverse resistance ~ -35 * B  → ~ -10.5 Ω at 0.3 T
    assert row["Rxy"] == pytest.approx(-35.0 * 0.3, abs=3.0)


def test_threaded_run_writes_csv(qapp, tmp_path):
    _, src, vxx, _ = _build(field_t=0.0)
    out = tmp_path / "run.csv"
    w = AcquisitionWorker([src], [vxx], out,
                          derived=[resistance("Rxx", "Vxx")],
                          settle_s=0.0, interval_s=0.01)

    rows = []
    w.sample_ready.connect(rows.append)
    w.start()

    deadline = time.monotonic() + 5.0
    while len(rows) < 3 and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    w.stop()
    w.wait(2000)
    qapp.processEvents()

    assert len(rows) >= 3
    assert out.exists()
    header = out.read_text().splitlines()[0]
    assert "time_s" in header and "Rxx" in header
