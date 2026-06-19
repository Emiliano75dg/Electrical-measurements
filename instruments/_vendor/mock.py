# VENDORED — copied verbatim from the M81_electr_meas project
# (electrical_measurements/instruments/mock.py).  Only the exceptions import below
# was changed to the in-tree vendored module.  See instruments/_vendor/__init__.py.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
import random
from typing import Any

from .exceptions import InstrumentConfigError

VALID_MEASURE_MODES = {"auto", "dc", "lockin"}
VALID_LOCKIN_ROLLOFFS = {"R6", "R12", "R18", "R24"}


class MockModule:
    def __init__(self, name: str) -> None:
        self.name = name
        self.enabled = False
        self.shape = "DC"
        self.measure_mode = "dc"
        self.frequency_hz = 0.0
        self.current_amplitude = 0.0
        self.voltage_amplitude = 0.0
        self.harmonic = 1
        self.time_constant = 0.3
        self.reference_source = "S1"
        self.nplc = 1.0
        self.rolloff = "R24"

    def set_shape(self, shape: str) -> None:
        self.shape = shape

    def set_frequency(self, value: float) -> None:
        self.frequency_hz = value

    def set_current_amplitude(self, value: float) -> None:
        self.current_amplitude = value

    def set_voltage_amplitude(self, value: float) -> None:
        self.voltage_amplitude = value

    def setup_dc_measurement(self, nplc: float = 1.0) -> None:
        self.shape = "DC"
        self.measure_mode = "dc"
        self.nplc = nplc

    def setup_lock_in_measurement(self, reference_source: str, time_constant: float, rolloff: str = "R24", reference_phase_shift: float = 0.0, reference_harmonic: int = 1, use_fir: bool = True) -> None:
        self.measure_mode = "lockin"
        self.reference_source = reference_source
        self.time_constant = time_constant
        self.harmonic = reference_harmonic
        self.rolloff = rolloff

    def set_reference_harmonic(self, harmonic: int) -> None:
        self.harmonic = harmonic

    def get_reference_harmonic(self) -> int:
        return self.harmonic

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False


@dataclass
class MockM81Controller:
    seed: int = 1234
    field_t: float = 0.0
    temperature_k: float = 300.0
    contact_error_scale: float = 0.0
    reciprocity_violation_scale: float = 0.0
    _sources: dict[str, MockModule] = field(default_factory=dict)
    _measures: dict[str, MockModule] = field(default_factory=dict)
    _preferred_measure_modes: dict[str, str] = field(default_factory=dict)
    _preferred_measure_harmonics: dict[str, int] = field(default_factory=dict)
    _preferred_measure_nplc: dict[str, float] = field(default_factory=dict)
    _preferred_measure_time_constants: dict[str, float] = field(default_factory=dict)
    _preferred_measure_rolloffs: dict[str, str] = field(default_factory=dict)
    _measurement_context: dict[str, Any] = field(default_factory=dict)
    _trace_config: dict[str, Any] = field(default_factory=dict)
    _trace_running: bool = False
    _trace_index: int = 0

    def __post_init__(self) -> None:
        self.random = random.Random(self.seed)
        self._sources = {name: MockModule(name) for name in ["S1", "S2", "S3"]}
        self._measures = {name: MockModule(name) for name in ["M1", "M2", "M3"]}

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "MockM81Controller":
        controller = cls()
        measure_modes = config.get("instruments", {}).get("m81", {}).get("measure_modes", {})
        if isinstance(measure_modes, dict):
            for measure_channel, mode in measure_modes.items():
                if isinstance(measure_channel, str) and isinstance(mode, str):
                    controller.set_preferred_measure_mode(measure_channel, mode)
        measure_harmonics = config.get("instruments", {}).get("m81", {}).get("measure_harmonics", {})
        if isinstance(measure_harmonics, dict):
            for measure_channel, harmonic in measure_harmonics.items():
                if isinstance(measure_channel, str) and isinstance(harmonic, int):
                    controller.set_preferred_measure_harmonic(measure_channel, harmonic)
        measure_nplc = config.get("instruments", {}).get("m81", {}).get("measure_nplc", {})
        if isinstance(measure_nplc, dict):
            for measure_channel, nplc in measure_nplc.items():
                if isinstance(measure_channel, str) and isinstance(nplc, (int, float)):
                    controller.set_preferred_measure_nplc(measure_channel, float(nplc))
        measure_time_constants = config.get("instruments", {}).get("m81", {}).get("measure_time_constants_s", {})
        if isinstance(measure_time_constants, dict):
            for measure_channel, time_constant_s in measure_time_constants.items():
                if isinstance(measure_channel, str) and isinstance(time_constant_s, (int, float)):
                    controller.set_preferred_measure_time_constant(measure_channel, float(time_constant_s))
        measure_rolloffs = config.get("instruments", {}).get("m81", {}).get("measure_rolloffs", {})
        if isinstance(measure_rolloffs, dict):
            for measure_channel, rolloff in measure_rolloffs.items():
                if isinstance(measure_channel, str) and isinstance(rolloff, str):
                    controller.set_preferred_measure_rolloff(measure_channel, rolloff)
        return controller

    def get_source_module(self, source: str) -> MockModule:
        return self._sources[source]

    def get_measure_module(self, measure_channel: str) -> MockModule:
        return self._measures[measure_channel]

    def set_preferred_measure_mode(self, measure_channel: str, mode: str) -> None:
        normalized = str(mode).strip().lower()
        if normalized not in VALID_MEASURE_MODES:
            raise InstrumentConfigError(f"Unsupported measure mode for {measure_channel}: {mode}")
        self._preferred_measure_modes[measure_channel] = normalized

    def set_preferred_measure_harmonic(self, measure_channel: str, harmonic: int) -> None:
        parsed = int(harmonic)
        if parsed < 1:
            raise InstrumentConfigError(f"Unsupported measure harmonic for {measure_channel}: {harmonic}")
        self._preferred_measure_harmonics[measure_channel] = parsed

    def get_preferred_measure_harmonic(self, measure_channel: str) -> int | None:
        return self._preferred_measure_harmonics.get(measure_channel)

    def set_preferred_measure_nplc(self, measure_channel: str, nplc: float) -> None:
        parsed = float(nplc)
        if parsed <= 0:
            raise InstrumentConfigError(f"Unsupported measure nplc for {measure_channel}: {nplc}")
        self._preferred_measure_nplc[measure_channel] = parsed

    def set_preferred_measure_time_constant(self, measure_channel: str, time_constant_s: float) -> None:
        parsed = float(time_constant_s)
        if parsed <= 0:
            raise InstrumentConfigError(f"Unsupported measure time constant for {measure_channel}: {time_constant_s}")
        self._preferred_measure_time_constants[measure_channel] = parsed

    def set_preferred_measure_rolloff(self, measure_channel: str, rolloff: str) -> None:
        normalized = str(rolloff).strip().upper()
        if normalized not in VALID_LOCKIN_ROLLOFFS:
            raise InstrumentConfigError(f"Unsupported measure rolloff for {measure_channel}: {rolloff}")
        self._preferred_measure_rolloffs[measure_channel] = normalized

    def resolve_measure_mode(self, measure_channel: str, requested_mode: str = "auto") -> str:
        normalized = str(requested_mode).strip().lower()
        if normalized not in VALID_MEASURE_MODES:
            raise InstrumentConfigError(f"Unsupported requested measure mode for {measure_channel}: {requested_mode}")
        if normalized != "auto":
            return normalized
        preferred = self._preferred_measure_modes.get(measure_channel, "auto")
        if preferred in VALID_MEASURE_MODES - {"auto"}:
            return preferred
        module = self.get_measure_module(measure_channel)
        if module.measure_mode in VALID_MEASURE_MODES - {"auto"}:
            return module.measure_mode
        return "auto"

    def resolve_measure_harmonic(self, measure_channel: str, requested_harmonic: int | None = None) -> int:
        if requested_harmonic is not None:
            parsed = int(requested_harmonic)
            if parsed < 1:
                raise InstrumentConfigError(f"Unsupported requested harmonic for {measure_channel}: {requested_harmonic}")
            return parsed
        preferred_harmonic = self._preferred_measure_harmonics.get(measure_channel)
        if isinstance(preferred_harmonic, int) and preferred_harmonic >= 1:
            return int(preferred_harmonic)
        module = self.get_measure_module(measure_channel)
        if isinstance(module.harmonic, int) and module.harmonic >= 1:
            return module.harmonic
        return 1

    def resolve_measure_nplc(self, measure_channel: str, requested_nplc: float | None = None) -> float:
        if requested_nplc is not None:
            parsed = float(requested_nplc)
            if parsed <= 0:
                raise InstrumentConfigError(f"Unsupported requested nplc for {measure_channel}: {requested_nplc}")
            return parsed
        preferred_nplc = self._preferred_measure_nplc.get(measure_channel)
        if isinstance(preferred_nplc, (int, float)) and float(preferred_nplc) > 0:
            return float(preferred_nplc)
        module = self.get_measure_module(measure_channel)
        if float(module.nplc) > 0:
            return float(module.nplc)
        return 1.0

    def resolve_measure_time_constant(self, measure_channel: str, requested_time_constant_s: float | None = None) -> float:
        if requested_time_constant_s is not None:
            parsed = float(requested_time_constant_s)
            if parsed <= 0:
                raise InstrumentConfigError(f"Unsupported requested time constant for {measure_channel}: {requested_time_constant_s}")
            return parsed
        preferred_time_constant_s = self._preferred_measure_time_constants.get(measure_channel)
        if isinstance(preferred_time_constant_s, (int, float)) and float(preferred_time_constant_s) > 0:
            return float(preferred_time_constant_s)
        module = self.get_measure_module(measure_channel)
        if float(module.time_constant) > 0:
            return float(module.time_constant)
        return 0.3

    def resolve_measure_rolloff(self, measure_channel: str, requested_rolloff: str | None = None) -> str:
        if requested_rolloff is not None:
            normalized = str(requested_rolloff).strip().upper()
            if normalized not in VALID_LOCKIN_ROLLOFFS:
                raise InstrumentConfigError(f"Unsupported requested rolloff for {measure_channel}: {requested_rolloff}")
            return normalized
        preferred_rolloff = self._preferred_measure_rolloffs.get(measure_channel)
        if isinstance(preferred_rolloff, str):
            normalized = preferred_rolloff.strip().upper()
            if normalized in VALID_LOCKIN_ROLLOFFS:
                return normalized
        module = self.get_measure_module(measure_channel)
        if module.rolloff in VALID_LOCKIN_ROLLOFFS:
            return module.rolloff
        return "R24"

    def configure_dc_current(self, source: str, current_a: float, compliance_v: float = 1.0, autorange: bool = True) -> None:
        module = self.get_source_module(source)
        module.set_shape("DC")
        module.set_current_amplitude(current_a)

    def configure_dc_voltage(self, source: str, voltage_v: float, compliance_a: float = 1e-3, autorange: bool = True) -> None:
        module = self.get_source_module(source)
        module.set_shape("DC")
        module.set_voltage_amplitude(voltage_v)

    def configure_ac_current_lockin(self, source: str, current_rms_a: float, frequency_hz: float, measure_channels: list[str], harmonic: int = 1, time_constant_s: float = 0.3, reference_source: str = "S1") -> None:
        module = self.get_source_module(source)
        module.set_shape("SINUSOID")
        module.set_frequency(frequency_hz)
        module.set_current_amplitude(current_rms_a * 2**0.5)
        for channel in measure_channels:
            self.configure_lockin_measure(channel, harmonic=harmonic, time_constant_s=time_constant_s, reference_source=reference_source)

    def configure_ac_voltage_lockin(self, source: str, voltage_rms_v: float, frequency_hz: float, measure_channels: list[str], harmonic: int = 1, time_constant_s: float = 0.3, reference_source: str = "S1") -> None:
        module = self.get_source_module(source)
        module.set_shape("SINUSOID")
        module.set_frequency(frequency_hz)
        module.set_voltage_amplitude(voltage_rms_v * 2**0.5)
        for channel in measure_channels:
            self.configure_lockin_measure(channel, harmonic=harmonic, time_constant_s=time_constant_s, reference_source=reference_source)

    def configure_dc_measure(self, measure_channel: str, nplc: float = 1.0) -> None:
        self.get_measure_module(measure_channel).setup_dc_measurement(nplc=nplc)

    def configure_lockin_measure(self, measure_channel: str, harmonic: int = 1, time_constant_s: float = 0.3, reference_source: str = "S1", rolloff: str = "R24") -> None:
        self.get_measure_module(measure_channel).setup_lock_in_measurement(reference_source, time_constant_s, rolloff=rolloff, reference_harmonic=harmonic)

    def configure_lockin_harmonic(self, measure_channel: str, harmonic: int) -> None:
        self.get_measure_module(measure_channel).set_reference_harmonic(harmonic)

    def enable_source(self, source: str) -> None:
        self.get_source_module(source).enable()

    def disable_source(self, source: str) -> None:
        self.get_source_module(source).disable()

    def disable_all_sources(self) -> None:
        for source in self._sources.values():
            source.disable()

    def any_source_enabled(self) -> bool:
        return any(source.enabled for source in self._sources.values())

    def get_source_settings(self, source: str) -> dict[str, Any]:
        module = self.get_source_module(source)
        return {
            "mode": module.shape,
            "enabled": module.enabled,
            "current_peak_a": module.current_amplitude,
            "voltage_peak_v": module.voltage_amplitude,
            "frequency_hz": module.frequency_hz,
        }

    def get_measure_settings(self, measure_channel: str) -> dict[str, Any]:
        module = self.get_measure_module(measure_channel)
        return {
            "mode": module.measure_mode,
            "harmonic": module.harmonic,
            "frequency_hz": module.frequency_hz,
            "reference_source": module.reference_source,
            "nplc": module.nplc,
            "time_constant_s": module.time_constant,
            "rolloff": module.rolloff,
            "preferred_mode": self._preferred_measure_modes.get(measure_channel, "auto"),
            "resolved_mode": self.resolve_measure_mode(measure_channel),
            "preferred_harmonic": self._preferred_measure_harmonics.get(measure_channel, 1),
            "resolved_harmonic": self.resolve_measure_harmonic(measure_channel),
            "preferred_nplc": self._preferred_measure_nplc.get(measure_channel, 1.0),
            "resolved_nplc": self.resolve_measure_nplc(measure_channel),
            "preferred_time_constant_s": self._preferred_measure_time_constants.get(measure_channel, 0.3),
            "resolved_time_constant_s": self.resolve_measure_time_constant(measure_channel),
            "preferred_rolloff": self._preferred_measure_rolloffs.get(measure_channel, "R24"),
            "resolved_rolloff": self.resolve_measure_rolloff(measure_channel),
        }

    def set_measurement_context(self, **context: Any) -> None:
        self._measurement_context = dict(context)

    def _base_signal(self, measure_channel: str) -> float:
        context = self._measurement_context
        state_name = str(context.get("state", ""))
        group = str(context.get("group", ""))
        current_sign = float(context.get("current_sign", 1.0))
        measure_kind = str(context.get("measure_kind", "longitudinal"))
        temp_term = 0.02 * (self.temperature_k - 300.0)
        hall_coeff = -35.0
        rxx0 = 120.0 + 8.0 * self.field_t**2 + temp_term
        hall = hall_coeff * self.field_t * current_sign
        misalign = 0.8 * self.field_t
        reciprocity_term = self.reciprocity_violation_scale * (1.0 if "reciprocal" in state_name else 0.0)
        contact_term = self.contact_error_scale
        if "second_harmonic" in group or "2omega" in state_name:
            return 0.05 * self.field_t + 0.01 * current_sign + contact_term
        if measure_kind == "transverse" or "vxy" in state_name or measure_channel == "M2":
            return hall + misalign + reciprocity_term + contact_term
        if group == "vdp":
            return (150.0 + 5.0 * abs(self.field_t) + temp_term) * current_sign + reciprocity_term + contact_term
        return rxx0 * current_sign + 0.2 * self.field_t + reciprocity_term + contact_term

    def read_lockin(self, measure_channel: str) -> dict[str, Any]:
        module = self.get_measure_module(measure_channel)
        base = self._base_signal(measure_channel)
        noise = self.random.gauss(0.0, 0.02 * max(abs(base), 1.0))
        x = base + noise
        y = 0.1 * base + self.random.gauss(0.0, 0.01 * max(abs(base), 1.0))
        r = math.hypot(x, y)
        theta = math.degrees(math.atan2(y, x))
        return {
            "x": x,
            "y": y,
            "r": r,
            "theta_deg": theta,
            "frequency_hz": module.frequency_hz,
            "harmonic": module.harmonic,
            "resistance_ohm": r,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def read_dc(self, measure_channel: str) -> dict[str, Any]:
        base = self._base_signal(measure_channel)
        return {
            "value": base + self.random.gauss(0.0, 0.01 * max(abs(base), 1.0)),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def configure_current_sweep(self, **kwargs: Any) -> None:
        return None

    def configure_voltage_sweep(self, **kwargs: Any) -> None:
        return None

    def configure_trace_stream(self, channel: str, points: int = 1024, interval_s: float = 0.1) -> None:
        self._trace_config = {"channel": channel, "points": points, "interval_s": interval_s}

    def start_trace(self) -> None:
        self._trace_running = True
        self._trace_index = 0

    def fetch_trace(self, max_points: int | None = None) -> list[dict[str, Any]]:
        channel = str(self._trace_config.get("channel", "M1"))
        interval_s = float(self._trace_config.get("interval_s", 0.1))
        point_limit = int(self._trace_config.get("points", 1))
        batch_size = max_points or 1
        batch_size = min(batch_size, max(point_limit - self._trace_index, 0))
        if batch_size <= 0:
            return []
        start = datetime.now(timezone.utc)
        records = []
        for offset in range(batch_size):
            sample = self.read_lockin(channel)
            records.append(
                {
                    "trace_channel": channel,
                    "trace_index": self._trace_index + offset,
                    "timestamp": (start + timedelta(seconds=offset * interval_s)).isoformat(),
                    "x": sample["x"],
                    "y": sample["y"],
                    "r": sample["r"],
                    "theta_deg": sample["theta_deg"],
                    "frequency_hz": sample["frequency_hz"],
                    "harmonic": sample["harmonic"],
                }
            )
        self._trace_index += batch_size
        if self._trace_index >= point_limit:
            self._trace_running = False
        return records

    def abort_sweep(self) -> None:
        self._trace_running = False

    def emergency_stop(self) -> None:
        self.disable_all_sources()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "temperature_k": self.temperature_k,
            "field_t": self.field_t,
            "sources": {
                name: {
                    "enabled": module.enabled,
                    "mode": module.shape,
                    "frequency_hz": module.frequency_hz,
                }
                for name, module in self._sources.items()
            },
            "measures": {
                name: self.get_measure_settings(name)
                for name, module in self._measures.items()
            },
        }


@dataclass
class MockMatrix7709:
    closed_channels: set[int] = field(default_factory=set)
    applied_states: list[str] = field(default_factory=list)
    settle_s: float = 0.0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "MockMatrix7709":
        settle_s = config.get("instruments", {}).get("daq6510", {}).get("settle_s", 0.0)
        return cls(settle_s=settle_s)

    def open_all(self) -> None:
        self.closed_channels.clear()

    def close_channels(self, channels: list[int]) -> None:
        self.closed_channels = set(channels)

    def apply_state(self, state_name: str, channels: list[int] | None = None) -> None:
        self.applied_states.append(state_name)
        self.closed_channels = set(channels or [])

    def emergency_stop(self) -> None:
        self.open_all()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "closed_channels": sorted(self.closed_channels),
            "applied_states": list(self.applied_states),
        }


@dataclass
class MockEnvironmentController:
    temperature_k: float = 300.0
    field_t: float = 0.0
    ramp_target_t: float | None = None
    ramp_active: bool = False
    ramp_rate_t_per_min: float = 0.0
    temperature_target_k: float | None = None
    temperature_ramp_active: bool = False
    temperature_ramp_rate_k_per_min: float = 0.0

    def set_temperature(self, target_k: float) -> None:
        self.temperature_k = target_k

    def wait_temperature_stable(self, target_k: float, tolerance: float = 0.05, timeout: float = 300.0) -> None:
        self.temperature_k = target_k

    def read_temperature(self) -> float | None:
        return self.temperature_k

    def set_field(self, target_t: float) -> None:
        self.field_t = target_t

    def wait_field_stable(self, target_t: float, tolerance: float = 1e-4, timeout: float = 300.0) -> None:
        self.field_t = target_t

    def read_field(self) -> float | None:
        return self.field_t

    def start_field_ramp(self, target_t: float, rate_t_per_min: float) -> None:
        self.ramp_target_t = target_t
        self.ramp_active = True
        self.ramp_rate_t_per_min = abs(rate_t_per_min)

    def stop_field_ramp(self) -> None:
        self.ramp_active = False

    def start_temperature_ramp(self, target_k: float, rate_k_per_min: float) -> None:
        self.temperature_target_k = target_k
        self.temperature_ramp_active = True
        self.temperature_ramp_rate_k_per_min = abs(rate_k_per_min)

    def stop_temperature_ramp(self) -> None:
        self.temperature_ramp_active = False

    def advance_time(self, dt_s: float) -> None:
        if self.ramp_active and self.ramp_target_t is not None:
            step = self.ramp_rate_t_per_min * dt_s / 60.0
            delta = self.ramp_target_t - self.field_t
            if abs(delta) <= step or step == 0:
                self.field_t = self.ramp_target_t
                self.ramp_active = False
            else:
                self.field_t += step if delta > 0 else -step
        if self.temperature_ramp_active and self.temperature_target_k is not None:
            step = self.temperature_ramp_rate_k_per_min * dt_s / 60.0
            delta = self.temperature_target_k - self.temperature_k
            if abs(delta) <= step or step == 0:
                self.temperature_k = self.temperature_target_k
                self.temperature_ramp_active = False
            else:
                self.temperature_k += step if delta > 0 else -step

    def is_field_ramp_running(self) -> bool:
        return self.ramp_active

    def is_temperature_ramp_running(self) -> bool:
        return self.temperature_ramp_active

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "temperature_k": self.temperature_k,
            "field_t": self.field_t,
            "field_ramp_running": self.ramp_active,
            "temperature_ramp_running": self.temperature_ramp_active,
        }
