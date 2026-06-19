"""Session persistence: round-trip, schema versioning, backward compatibility."""

import json

import pytest

from core.channels import Func, MeterConfig, SourceConfig
from core.derived import Geometry
from core.session import (
    SCHEMA_VERSION,
    ConnectionSettings,
    MatrixSettings,
    MeterSpec,
    Session,
    SourceSpec,
    load_session,
    save_session,
)
from measurements.routing import MatrixLayout, RouteStep


def _full_session() -> Session:
    return Session(
        connection=ConnectionSettings(ip_address="10.0.0.5", simulated=False),
        sources=[
            SourceSpec(port=1, config=SourceConfig(
                func=Func.I_DC, amplitude=5e-4, frequency_Hz=23.3, compliance=2.0))
        ],
        meters=[
            MeterSpec(port=1, meter_id="Vxx", config=MeterConfig(
                lockin=True, reference="S1", harmonic=2, time_constant_s=0.1,
                rolloff="R18", phase_shift_deg=12.5, use_fir=False)),
            MeterSpec(port=2, meter_id="Vxy", config=MeterConfig(lockin=False, nplc=5.0)),
        ],
        derived_mode="Hall preset (Rxx, Rxy, ρ)",
        geometry=Geometry(width_m=2e-3, length_m=5e-3, thickness_m=1e-6),
        settle_s=2.5,
        interval_s=0.25,
        current_reversal=True,
        matrix=MatrixSettings(enabled=True, resource="GPIB0::7", simulated=False,
                              settle_s=0.08, vdp_sheet=True),
        layout=MatrixLayout(terminal_row={"I+": 1, "I-": 2}, contact_col={"C1": 1, "C2": 2}),
        routes=[RouteStep("s1", [("I+", "C1"), ("I-", "C2")])],
    )


def test_round_trip_preserves_everything():
    s = _full_session()
    restored = Session.from_dict(s.to_dict())
    assert restored == s


def test_round_trip_lockin_params_preserved():
    # closes the Phase-3 known limit: harmonic + phase survive serialisation
    s = _full_session()
    m = Session.from_dict(s.to_dict()).meters[0]
    assert m.config.harmonic == 2
    assert m.config.phase_shift_deg == 12.5


def test_file_round_trip(tmp_path):
    s = _full_session()
    path = tmp_path / "setup.json"
    save_session(s, path)
    assert load_session(path) == s


def test_schema_version_written(tmp_path):
    path = tmp_path / "setup.json"
    save_session(_full_session(), path)
    data = json.loads(path.read_text())
    assert data["schema_version"] == SCHEMA_VERSION


def test_load_rejects_newer_schema(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"schema_version": SCHEMA_VERSION + 1}))
    with pytest.raises(ValueError, match="schema"):
        load_session(path)


def test_from_dict_tolerates_missing_matrix_keys():
    # a file written before the matrix model existed must still load
    legacy = {
        "schema_version": 1,
        "connection": {"ip_address": "1.2.3.4", "simulated": True},
        "sources": [{"port": 1, "config": {"func": "I_AC", "amplitude": 1e-6}}],
        "meters": [{"port": 1, "meter_id": "V", "config": {"lockin": True}}],
    }
    s = Session.from_dict(legacy)
    assert s.matrix.enabled is False
    assert s.routes == []
    assert s.sources[0].config.func is Func.I_AC


def test_from_dict_empty_uses_defaults():
    s = Session.from_dict({})
    assert s == Session()
