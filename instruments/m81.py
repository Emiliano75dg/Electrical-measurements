"""
Lake Shore M81-SSM instrument wrapper.

Backends
────────
  Real hardware:
    Uses lakeshore.SSMSystem via M81Controller from the electrical_measurements
    package (M81_electr_meas project, already on sys.path).

  Simulation:
    Uses MockM81Controller from the same package — fully physics-aware mock
    with field-dependent Hall effect, temperature, and realistic noise.
    Does NOT require hardware.

Port numbering convention (this file)
──────────────────────────────────────
  source_port  : int  1 → "S1", 2 → "S2", 3 → "S3"
  measure_port : int  1 → "M1", 2 → "M2", 3 → "M3"

The underlying M81Controller / MockM81Controller use string labels internally.

Signal convention
─────────────────
  The M81Controller returns voltages [V] from real hardware.
  The MockM81Controller returns resistance values [Ω] directly.
  This wrapper normalises both to voltages:
    - Real:  x, y  come back as V → returned as-is
    - Mock:  x, y  come back as Ω → multiplied by I_source to get V
  The measurement worker then divides by I to recover R.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# The physics mock is vendored in-tree (instruments/_vendor), so simulation, the
# test suite and CI are self-contained — no sibling checkout, no hardware driver.
from instruments._vendor.mock import MockM81Controller
_MOCK_AVAILABLE = True

# The real-hardware backend (M81Controller, which pulls in 'lakeshore') is NOT
# vendored: it is imported from the sibling M81_electr_meas project, only needed
# on the lab machine.  Its absence must never break simulation, so it is guarded.
_SRC = Path(__file__).resolve().parents[2] / "M81_electr_meas" / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:
    from electrical_measurements.instruments.m81 import M81Controller
    _HARDWARE_AVAILABLE = True
except ImportError:
    M81Controller = None  # type: ignore[assignment]
    _HARDWARE_AVAILABLE = False

# Backwards-compatible aggregate flag (either backend available).
_BACKEND_AVAILABLE = _MOCK_AVAILABLE or _HARDWARE_AVAILABLE


class SourceMode(str, Enum):
    AC = "AC"
    DC = "DC"


@dataclass
class _SourceState:
    mode: SourceMode = SourceMode.AC
    amplitude_A: float = 100e-6
    voltage_V: float = 0.0
    frequency_Hz: float = 17.77
    output_on: bool = False
    is_voltage_mode: bool = False


def _s(port: int) -> str:
    return f"S{port}"

def _m(port: int) -> str:
    return f"M{port}"


class M81Instrument:
    """Thin facade over M81Controller / MockM81Controller.

    Parameters
    ----------
    ip_address:
        IPv4 address of the M81 (e.g. '192.168.0.1'). Ignored when simulated.
    simulated:
        Use MockM81Controller with realistic Hall bar physics (no hardware needed).
    field_t:
        Applied magnetic field in Tesla — only used by the mock.
    temperature_k:
        Temperature in Kelvin — only used by the mock.
    """

    def __init__(
        self,
        ip_address: str,
        simulated: bool = False,
        field_t: float = 0.0,
        temperature_k: float = 300.0,
    ):
        self._ip = ip_address
        self._simulated = simulated
        self._field_t = field_t
        self._temperature_k = temperature_k
        self._ctrl: "M81Controller | MockM81Controller | None" = None
        self._connected = False
        self._source_states: dict[int, _SourceState] = {}

    # ── connection ─────────────────────────────────────────────────────────────

    def connect(self) -> str:
        """Connect and return IDN string."""
        if self._simulated:
            if not _MOCK_AVAILABLE:
                raise RuntimeError(
                    "Simulation backend not found.\n"
                    "Make sure M81_electr_meas/src is on PYTHONPATH "
                    "(provides the physics mock; no hardware driver needed)."
                )
            self._ctrl = MockM81Controller(
                field_t=self._field_t,
                temperature_k=self._temperature_k,
            )
            self._connected = True
            return (
                f"LAKESHORE,M81-SSM,MOCK  "
                f"[B={self._field_t:.3f} T  T={self._temperature_k:.1f} K]"
            )

        if not _HARDWARE_AVAILABLE:
            raise RuntimeError(
                "Hardware backend not available.\n"
                "Install 'lakeshore' and make sure M81_electr_meas/src is on PYTHONPATH."
            )

        cfg = {
            "instruments": {
                "m81": {
                    "connection": {"kind": "tcp", "ip_address": self._ip, "tcp_port": 7777}
                }
            }
        }
        self._ctrl = M81Controller.from_config(cfg)
        self._connected = True
        return f"LAKESHORE,M81-SSM,{self._ip}"

    def disconnect(self) -> None:
        self._ctrl = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ── mock parameters (can be updated at runtime) ────────────────────────────

    def set_mock_field(self, field_t: float) -> None:
        """Update the simulated magnetic field (only effective in simulation mode)."""
        self._field_t = field_t
        if self._simulated and self._ctrl is not None:
            self._ctrl.field_t = field_t

    def set_mock_temperature(self, temperature_k: float) -> None:
        """Update the simulated temperature (only effective in simulation mode)."""
        self._temperature_k = temperature_k
        if self._simulated and self._ctrl is not None:
            self._ctrl.temperature_k = temperature_k

    # ── source configuration ───────────────────────────────────────────────────

    def configure_current_source_ac(
        self,
        source_port: int,
        amplitude_A: float,
        frequency_Hz: float,
        compliance_V: float = 1.0,
    ) -> None:
        """Configure the AC current source only — measure channels are configured separately."""
        self._ctrl.configure_ac_current_lockin(
            source=_s(source_port),
            current_rms_a=amplitude_A,
            frequency_hz=frequency_Hz,
            measure_channels=[],          # configured explicitly via configure_measure_lockin
            harmonic=1,                   # unused (no measure channels), required by API
            time_constant_s=0.1,          # unused (no measure channels), required by API
            reference_source=_s(source_port),
        )
        self._apply_source_compliance(_s(source_port), compliance_v=compliance_V)
        self._set_mock_current_sign(amplitude_A)
        self._source_states[source_port] = _SourceState(
            mode=SourceMode.AC,
            amplitude_A=amplitude_A,
            frequency_Hz=frequency_Hz,
            output_on=False,
        )

    def configure_current_source_dc(
        self,
        source_port: int,
        level_A: float,
        compliance_V: float = 1.0,
    ) -> None:
        """Configure the DC current source."""
        self._ctrl.configure_dc_current(
            source=_s(source_port), current_a=level_A, compliance_v=compliance_V
        )
        self._apply_source_compliance(_s(source_port), compliance_v=compliance_V)
        self._set_mock_current_sign(level_A)
        self._source_states[source_port] = _SourceState(
            mode=SourceMode.DC,
            amplitude_A=abs(level_A),
            output_on=False,
        )

    def configure_voltage_source_dc(
        self,
        source_port: int,
        voltage_V: float,
        compliance_A: float = 1e-3,
    ) -> None:
        """Configure the SMU slot as a DC voltage source."""
        self._ctrl.configure_dc_voltage(
            source=_s(source_port),
            voltage_v=voltage_V,
            compliance_a=compliance_A,
        )
        self._apply_source_compliance(_s(source_port), compliance_a=compliance_A)
        self._source_states[source_port] = _SourceState(
            mode=SourceMode.DC,
            voltage_V=abs(voltage_V),
            is_voltage_mode=True,
            output_on=False,
        )

    def configure_voltage_source_ac(
        self,
        source_port: int,
        voltage_rms_V: float,
        frequency_Hz: float,
        compliance_A: float = 1e-3,
    ) -> None:
        """Configure the SMU slot as an AC voltage source for lock-in."""
        self._ctrl.configure_ac_voltage_lockin(
            source=_s(source_port),
            voltage_rms_v=voltage_rms_V,
            frequency_hz=frequency_Hz,
            measure_channels=[],
            harmonic=1,
            time_constant_s=0.1,
            reference_source=_s(source_port),
        )
        self._source_states[source_port] = _SourceState(
            mode=SourceMode.AC,
            voltage_V=voltage_rms_V,
            frequency_Hz=frequency_Hz,
            is_voltage_mode=True,
            output_on=False,
        )

    def output_on(self, source_port: int) -> None:
        self._ctrl.enable_source(_s(source_port))
        if source_port in self._source_states:
            self._source_states[source_port].output_on = True

    def output_off(self, source_port: int) -> None:
        self._ctrl.disable_source(_s(source_port))
        if source_port in self._source_states:
            self._source_states[source_port].output_on = False

    def prepare_for_acquisition(self, mode: str) -> None:
        """Best-effort cleanup before switching acquisition mode.

        A manual front-panel reset restoring sane DC readings suggests stale
        source/trace/lock-in state can leak across runs. We avoid a full device
        reset and instead stop ongoing activity, disable sources, and clear our
        cached source-enable state before reconfiguring the new mode.
        """
        abort_sweep = getattr(self._ctrl, "abort_sweep", None)
        if callable(abort_sweep):
            try:
                abort_sweep()
            except Exception:
                pass

        disable_all_sources = getattr(self._ctrl, "disable_all_sources", None)
        if callable(disable_all_sources):
            try:
                disable_all_sources()
            except Exception:
                pass

        for state in self._source_states.values():
            state.output_on = False

    # ── measure configuration ──────────────────────────────────────────────────

    def configure_measure_lockin(
        self,
        measure_port: int,
        reference_source: str,
        harmonic: int = 1,
        time_constant_s: float = 0.3,
        rolloff: str = "R24",
        phase_shift_deg: float = 0.0,
        use_fir: bool = True,
    ) -> None:
        self._ctrl.configure_lockin_measure(
            measure_channel=_m(measure_port),
            harmonic=harmonic,
            time_constant_s=time_constant_s,
            reference_source=reference_source,
            rolloff=rolloff,
        )
        # phase shift and FIR are not in the shared M81Controller wrapper;
        # apply them directly on real hardware only
        if not self._simulated:
            module = self._ctrl.get_measure_module(_m(measure_port))
            if hasattr(module, "set_reference_phase_shift"):
                module.set_reference_phase_shift(phase_shift_deg)
            if hasattr(module, "set_lock_in_averaging_state"):
                module.set_lock_in_averaging_state(use_fir)

    def configure_measure_dc(self, measure_port: int, nplc: float = 1.0) -> None:
        self._ctrl.configure_dc_measure(measure_channel=_m(measure_port), nplc=nplc)
        if not self._simulated:
            module = self._ctrl.get_measure_module(_m(measure_port))
            if hasattr(module, "disable_lock_in_averaging"):
                module.disable_lock_in_averaging()

    # ── readings ───────────────────────────────────────────────────────────────

    def read_lockin_xy(self, measure_port: int) -> tuple[float, float]:
        """Return (X, Y) in Volts (current mode) or V/V ratios (voltage mode).

        Mock normalisation: mock returns Ω; multiply by I to get V in current
        mode.  In voltage mode the raw Ω value is already the transfer ratio
        (Vxx/V_source), so no multiplication is needed.
        """
        data = self._ctrl.read_lockin(_m(measure_port))
        x, y = float(data["x"]), float(data["y"])
        if self._simulated and not self._active_source_is_voltage():
            I = self._source_amplitude()
            x *= I
            y *= I
        return x, y

    def read_lockin_xy_pair(
        self,
        measure_port_1: int,
        measure_port_2: int,
    ) -> tuple[float, float, float, float]:
        """Return (x1, y1, x2, y2) in Volts."""
        x1, y1 = self.read_lockin_xy(measure_port_1)
        x2, y2 = self.read_lockin_xy(measure_port_2)
        return x1, y1, x2, y2

    def read_dc_voltage(self, measure_port: int) -> float:
        v = self._read_dc_value(_m(measure_port))
        if self._simulated and not self._active_source_is_voltage():
            v *= self._source_amplitude()
        return v

    def read_dc_voltage_pair(
        self,
        measure_port_1: int,
        measure_port_2: int,
    ) -> tuple[float, float]:
        return self.read_dc_voltage(measure_port_1), self.read_dc_voltage(measure_port_2)

    # ── SMU measurement (source-measure unit read through its source slot) ───────

    # representative mock load for synthesising the SMU's measured value
    _SMU_MOCK_LOAD_OHM = 100.0

    def read_smu(self, source_port: int) -> tuple[float, bool]:
        """Read the SMU's own measurement on a source slot.

        Returns (value, is_current): a voltage-sourcing SMU measures the current
        it delivers (is_current=True); a current-sourcing SMU measures the
        voltage across the load (is_current=False).  The SMU occupies a source
        slot (Sn) and is read through it — it does not use a VM-10 measure slot.
        """
        st = self._source_states.get(source_port)
        is_voltage = bool(st and st.is_voltage_mode)

        if self._simulated:
            r = self._SMU_MOCK_LOAD_OHM
            if is_voltage:
                v = st.voltage_V if st else 0.0
                return v / r, True            # measured current [A]
            i = st.amplitude_A if st else 0.0
            return i * r, False               # measured voltage [V]

        module = self._ctrl.get_source_module(_s(source_port))
        getters = (
            ("measure_i", "get_i", "measure_current", "get_current")
            if is_voltage else
            ("measure_v", "get_v", "measure_voltage", "get_voltage")
        )
        for attr in getters:
            getter = getattr(module, attr, None)
            if callable(getter):
                return float(getter()), is_voltage
        raise RuntimeError(
            f"SMU measurement not available on source module S{source_port} for this driver"
        )

    # ── internal helpers ───────────────────────────────────────────────────────

    def _set_mock_current_sign(self, level: float) -> None:
        """Tell the mock the current polarity so DC current reversal is reproducible.

        The mock's signal model honours a ``current_sign`` context (odd in I);
        without this the simulated V would not flip with −I and antisymmetrisation
        would cancel to zero.  No-op on real hardware.
        """
        if not self._simulated or self._ctrl is None:
            return
        set_ctx = getattr(self._ctrl, "set_measurement_context", None)
        if callable(set_ctx):
            set_ctx(current_sign=1.0 if level >= 0 else -1.0)

    def _source_amplitude(self) -> float:
        # Always the magnitude: the mock carries current polarity via current_sign,
        # so the Ω→V normalisation must multiply by |I| (otherwise −I would flip the
        # sign twice and DC current reversal would cancel to noise).
        for st in self._source_states.values():
            if st.output_on:
                return abs(st.amplitude_A)
        for st in self._source_states.values():
            return abs(st.amplitude_A)
        return 100e-6

    def _active_source_is_voltage(self) -> bool:
        for st in self._source_states.values():
            if st.output_on:
                return st.is_voltage_mode
        for st in self._source_states.values():
            return st.is_voltage_mode
        return False

    def _apply_source_compliance(
        self,
        source_name: str,
        compliance_v: float | None = None,
        compliance_a: float | None = None,
    ) -> None:
        """Best-effort compliance programming across different driver variants."""
        if self._simulated:
            return
        module = self._ctrl.get_source_module(source_name)
        if compliance_v is not None:
            for attr in (
                "set_compliance_voltage",
                "set_voltage_compliance",
                "set_compliance_v",
            ):
                setter = getattr(module, attr, None)
                if callable(setter):
                    setter(float(compliance_v))
                    break
        if compliance_a is not None:
            for attr in (
                "set_compliance_current",
                "set_current_compliance",
                "set_compliance_a",
            ):
                setter = getattr(module, attr, None)
                if callable(setter):
                    setter(float(compliance_a))
                    break
        disable_on_compliance = getattr(module, "set_disable_on_compliance", None)
        if callable(disable_on_compliance):
            disable_on_compliance(True)

    def _read_dc_value(self, measure_name: str) -> float:
        """Read a DC value across multiple backend variants."""
        try:
            data = self._ctrl.read_dc(measure_name)
            return float(data["value"])
        except Exception as primary_exc:
            module = self._ctrl.get_measure_module(measure_name)
            for attr in ("get_dc", "get_dc_value", "get_voltage", "read_dc"):
                getter = getattr(module, attr, None)
                if callable(getter):
                    return float(getter())
            system = getattr(self._ctrl, "system", None)
            mnemonic = getattr(system, "DataSourceMnemonic", None)
            get_data = getattr(system, "get_data", None)
            if mnemonic is not None and callable(get_data) and hasattr(mnemonic, "MEASURE_DC"):
                channel_index = int(measure_name[1:])
                rows = get_data(1, 1, (mnemonic.MEASURE_DC, channel_index))
                if rows:
                    row = rows[0]
                    if isinstance(row, (tuple, list)):
                        for value in reversed(row):
                            if isinstance(value, (int, float)):
                                return float(value)
                    elif isinstance(row, (int, float)):
                        return float(row)
            raise primary_exc
