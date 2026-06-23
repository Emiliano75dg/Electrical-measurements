"""External SMU adapter (Keysight B2902B) + the generic instrument factory.

Qt-free: the SCPI transport, the B2902B adapter and the registry factory need no
PySide6.  The dry-run transport answers measure queries by a deterministic Ohm's
law, so the canonical gate case (source V on a channel, read the leakage I on the
*same* channel) is exercised end to end without hardware.
"""

import pytest

from core.channels import (
    Func,
    MeterChannel,
    MeterConfig,
    SourceChannel,
    SourceConfig,
)
from core.session import (
    DEFAULT_M81_ID,
    InstrumentSpec,
    TYPE_KEITHLEY_7709,
    TYPE_KEYSIGHT_B2902B,
    TYPE_M81,
)
from instruments.b2902b import (
    B2902BLabInstrument,
    B2902BMeter,
    B2902BSource,
    parse_host_port,
)
from instruments.m81_channels import M81Source
from instruments.registry import (
    Keithley7709LabInstrument,
    M81LabInstrument,
    Registry,
    build_instrument,
)
from instruments.scpi import DryRunTransport


# ── SCPI dry-run transport ────────────────────────────────────────────────────

def test_dryrun_idn_and_command_logging():
    t = DryRunTransport(name="GATE")
    t.write(":SOUR1:VOLT 2.0")
    assert any(":SOUR1:VOLT 2.0" in line for line in t.log)
    assert t.query("*IDN?") == "DRY,RUN,0,0"
    assert t.query("*OPC?") == "1"


def test_dryrun_source_voltage_measure_current_is_ohmic_leakage():
    # Gate case: source V, read leakage I = V / R.
    t = DryRunTransport(dry_run_resistance_ohm=200.0)
    t.write(":SOUR1:FUNC:MODE VOLT")
    t.write(":SOUR1:VOLT 1.0")
    leakage = float(t.query(":MEAS:CURR? (@1)"))
    assert leakage == pytest.approx(1.0 / 200.0)


def test_dryrun_source_current_measure_voltage_is_ohmic():
    t = DryRunTransport(dry_run_resistance_ohm=50.0)
    t.write(":SOUR1:CURR 0.01")
    voltage = float(t.query(":MEAS:VOLT? (@1)"))
    assert voltage == pytest.approx(0.01 * 50.0)


def test_dryrun_sense_command_does_not_disturb_source_state():
    # A :SENS compliance write must not be mistaken for a source level.
    t = DryRunTransport(dry_run_resistance_ohm=100.0)
    t.write(":SOUR1:VOLT 1.0")
    t.write(":SENS1:CURR:PROT 0.001")          # compliance, not a source level
    assert float(t.query(":MEAS:CURR? (@1)")) == pytest.approx(1.0 / 100.0)


def test_dryrun_compliance_not_tripped():
    assert DryRunTransport().query(":SENS1:CURR:PROT:TRIP?") == "0"


# ── B2902B channel adapters ───────────────────────────────────────────────────

def _connected_smu(resistance_ohm: float = 100.0) -> B2902BLabInstrument:
    inst = B2902BLabInstrument("0.0.0.0", simulated=True, instrument_id="gate_smu")
    inst.connect()
    inst._transport.dry_run_resistance_ohm = resistance_ohm  # type: ignore[union-attr]
    return inst


def test_source_voltage_meter_current_same_channel_share_transport():
    # The canonical gate+leakage case: one channel sources V and the meter on the
    # *same* channel reads I.  Source (:SOUR) and sense (:SENS) are orthogonal, so
    # they compose over a single shared transport without state conflict.
    inst = _connected_smu(resistance_ohm=100.0)
    src = inst.make_source(1, SourceConfig(func=Func.V_DC, amplitude=1.0, compliance=1e-3))
    meter = inst.make_meter(1, MeterConfig(lockin=False), "Ig")

    src.configure(src.config)
    src.enable()
    meter.configure(meter.config)
    reading = meter.read()

    assert reading.unit == "A"
    assert reading.dc == pytest.approx(1.0 / 100.0)   # leakage I = V / R


def test_source_voltage_sets_mode_compliance_and_level():
    inst = _connected_smu()
    src = inst.make_source(1, SourceConfig(func=Func.V_DC, amplitude=2.5, compliance=1e-3))
    src.configure(src.config)
    log = "\n".join(inst._transport.log)              # type: ignore[union-attr]
    assert ":SOUR1:FUNC:MODE VOLT" in log
    assert ":SENS1:CURR:PROT 0.001" in log            # current compliance = safety limit
    assert ":SOUR1:VOLT 2.5" in log


def test_source_requires_positive_compliance():
    inst = _connected_smu()
    src = inst.make_source(1, SourceConfig(func=Func.V_DC, amplitude=1.0, compliance=0.0))
    with pytest.raises(ValueError, match="compliance"):
        src.configure(src.config)


def test_source_rejects_ac_function():
    inst = _connected_smu()
    src = inst.make_source(1, SourceConfig(func=Func.I_AC, amplitude=1e-6, compliance=1.0))
    with pytest.raises(ValueError, match="DC SMU"):
        src.configure(src.config)


def test_disable_is_safe_disable_output_off():
    inst = _connected_smu()
    src = inst.make_source(1, SourceConfig(func=Func.V_DC, amplitude=1.0, compliance=1e-3))
    src.disable()
    assert any("OUTP1 OFF" in line for line in inst._transport.log)   # type: ignore[union-attr]


def test_disconnect_drives_both_outputs_off():
    inst = _connected_smu()
    transport = inst._transport
    inst.disconnect()
    log = "\n".join(transport.log)                    # type: ignore[union-attr]
    assert "OUTP1 OFF" in log and "OUTP2 OFF" in log
    assert not inst.connected


# ── parse_host_port ───────────────────────────────────────────────────────────

def test_parse_host_port_defaults_and_explicit():
    assert parse_host_port("192.168.0.5") == ("192.168.0.5", 5025)
    assert parse_host_port("10.0.0.7:7777") == ("10.0.0.7", 7777)


# ── generic factory: every declared type → a LabInstrument ────────────────────

def test_factory_builds_all_three_types():
    m81 = build_instrument(InstrumentSpec(id="m", type=TYPE_M81, simulated=True))
    matrix = build_instrument(InstrumentSpec(id="x", type=TYPE_KEITHLEY_7709, simulated=True))
    smu = build_instrument(
        InstrumentSpec(id="g", type=TYPE_KEYSIGHT_B2902B, resource="1.2.3.4:5025", simulated=True)
    )
    assert isinstance(m81, M81LabInstrument)
    assert isinstance(matrix, Keithley7709LabInstrument)
    assert isinstance(smu, B2902BLabInstrument)
    assert (m81.id, matrix.id, smu.id) == ("m", "x", "g")


def test_factory_b2902b_parses_resource_into_host_port():
    smu = build_instrument(
        InstrumentSpec(id="g", type=TYPE_KEYSIGHT_B2902B, resource="10.0.0.7:7777")
    )
    assert (smu._host, smu._port) == ("10.0.0.7", 7777)   # type: ignore[attr-defined]


def test_factory_unknown_type_raises():
    with pytest.raises(ValueError, match="Unknown instrument type"):
        build_instrument(InstrumentSpec(id="z", type="not_a_real_type"))


# ── registry: a B2902B resolves a binding alongside the default M81 ───────────

def test_b2902b_resolves_alongside_m81_in_registry():
    reg = Registry()
    reg.add(build_instrument(InstrumentSpec(id=DEFAULT_M81_ID, type=TYPE_M81, simulated=True)))
    gate = build_instrument(InstrumentSpec(id="gate", type=TYPE_KEYSIGHT_B2902B, simulated=True))
    gate.connect()
    reg.add(gate)

    m81_src = reg.resolve_source(None, 1, SourceConfig())
    gate_src = reg.resolve_source(
        "gate", 1, SourceConfig(func=Func.V_DC, amplitude=0.5, compliance=1e-3)
    )
    leak = reg.resolve_meter("gate", 1, MeterConfig(lockin=False), "Ig")

    assert isinstance(m81_src, M81Source)
    assert isinstance(gate_src, B2902BSource) and isinstance(gate_src, SourceChannel)
    assert isinstance(leak, B2902BMeter) and isinstance(leak, MeterChannel)
