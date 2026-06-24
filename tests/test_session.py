"""Session persistence: round-trip, schema versioning, backward compatibility."""

import json

import pytest

from core.channels import Func, MeterConfig, SourceConfig
from core.derived import Geometry
from core.session import (
    DEFAULT_M81_ID,
    DEFAULT_MATRIX_ID,
    SCHEMA_VERSION,
    TYPE_KEITHLEY_7709,
    TYPE_M81,
    ConnectionSettings,
    InstrumentSpec,
    MatrixSettings,
    MeterSpec,
    Session,
    SourceSpec,
    load_session,
    save_session,
)
from measurements.routing import MatrixLayout, RouteStep
from measurements.sequence import LoopSpec, SequenceSpec, StepSpec


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


# ── sequence tree (spec 03): additive, no schema bump ─────────────────────────

def test_sequence_defaults_to_none_and_is_omitted_from_json():
    s = _full_session()
    assert s.sequence is None
    assert "sequence" not in s.to_dict()          # absent key, byte-identical to before


def test_explicit_sequence_round_trips():
    s = _full_session()
    s.sequence = LoopSpec(
        kind="forever",
        child=SequenceSpec(
            cross_derived=True,
            children=[StepSpec(route="s1", current_reversal=True, settle_s=2.5)],
        ),
    )
    restored = Session.from_dict(s.to_dict())
    assert restored == s
    assert restored.sequence == s.sequence


def test_v2_file_without_sequence_loads_with_none():
    # a v2 file written before the tree existed: no "sequence" key -> None
    legacy = {
        "schema_version": 2,
        "connection": {"ip_address": "1.2.3.4", "simulated": True},
        "instruments": [],
        "sources": [{"port": 1, "config": {"func": "I_AC", "amplitude": 1e-6}}],
        "meters": [{"port": 1, "meter_id": "V", "config": {"lockin": True}}],
    }
    assert Session.from_dict(legacy).sequence is None


# ── v2 instrument registry: round-trip and v1 migration ──────────────────────

def test_v2_round_trip_preserves_instruments_and_bindings():
    s = _full_session()
    s.instruments = [
        InstrumentSpec(id="m81_main", type=TYPE_M81, resource="10.0.0.5", simulated=False),
        InstrumentSpec(id="gate_smu", type="keysight_b2902b",
                       resource="TCPIP0::192.168.0.5::INSTR", simulated=False),
    ]
    s.sources[0].instrument_id = "gate_smu"
    restored = Session.from_dict(s.to_dict())
    assert restored == s
    assert restored.sources[0].instrument_id == "gate_smu"
    assert [i.id for i in restored.instruments] == ["m81_main", "gate_smu"]


def test_schema_version_is_two():
    assert SCHEMA_VERSION == 2


def test_unbound_channel_omits_instrument_id_in_json():
    # a channel left on the default M81 serialises exactly as before (no key)
    data = _full_session().to_dict()
    assert "instrument_id" not in data["sources"][0]
    assert "instrument_id" not in data["meters"][0]


def test_v1_load_synthesizes_default_m81():
    legacy = {
        "schema_version": 1,
        "connection": {"ip_address": "1.2.3.4", "simulated": True},
        "sources": [{"port": 1, "config": {"func": "I_AC", "amplitude": 1e-6}}],
        "meters": [{"port": 1, "meter_id": "V", "config": {"lockin": True}}],
    }
    s = Session.from_dict(legacy)
    assert [i.id for i in s.instruments] == [DEFAULT_M81_ID]
    m81 = s.instruments[0]
    assert m81.type == TYPE_M81
    assert m81.resource == "1.2.3.4"
    assert m81.simulated is True
    assert s.sources[0].instrument_id is None   # unbound -> default M81


def test_v1_load_with_matrix_also_synthesizes_7709():
    legacy = {
        "schema_version": 1,
        "connection": {"ip_address": "1.2.3.4", "simulated": False},
        "matrix": {"enabled": True, "resource": "GPIB0::7", "simulated": False},
        "sources": [{"port": 1, "config": {"func": "I_AC"}}],
        "meters": [{"port": 1, "meter_id": "V", "config": {}}],
    }
    s = Session.from_dict(legacy)
    assert [i.id for i in s.instruments] == [DEFAULT_M81_ID, DEFAULT_MATRIX_ID]
    matrix = s.instruments[1]
    assert matrix.type == TYPE_KEITHLEY_7709
    assert matrix.resource == "GPIB0::7"
    assert matrix.simulated is False


def test_v1_file_load_via_save_path(tmp_path):
    # the public loader (not just from_dict) migrates a v1 file on disk
    legacy = {
        "schema_version": 1,
        "connection": {"ip_address": "9.9.9.9", "simulated": True},
        "sources": [{"port": 1, "config": {"func": "I_AC"}}],
        "meters": [{"port": 1, "meter_id": "V", "config": {"lockin": True}}],
    }
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(legacy))
    s = load_session(path)
    assert [i.id for i in s.instruments] == [DEFAULT_M81_ID]
    assert s.instruments[0].resource == "9.9.9.9"
