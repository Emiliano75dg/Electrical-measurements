"""Per-module M81 channel adapters.

Bind the hardware-agnostic SourceChannel / MeterChannel Protocols
(core.channels) to the Lake Shore M81-SSM through the existing M81Instrument
facade — which itself sits on M81Controller / MockM81Controller.

Keeping the wrapper (decision 2026-06-13, REDESIGN.md §4) means the mock Ω→V
normalisation lives in exactly one place (M81Instrument.read_*); these adapters
just translate config dataclasses into the wrapper's calls and wrap its returns
into a Reading.

Port numbering
──────────────
  source_port  : int  1 → "S1", 2 → "S2", 3 → "S3"
  measure_port : int  1 → "M1", 2 → "M2", 3 → "M3"
Source and measure ports are independent numbering spaces.
"""

from __future__ import annotations

from core.channels import Func, MeterConfig, Reading, SourceConfig
from instruments.m81 import M81Instrument


class M81Source:
    """SourceChannel backed by one M81 source slot (BCS-10 / VS-10 / SMU)."""

    def __init__(
        self,
        instrument: M81Instrument,
        port: int,
        config: SourceConfig | None = None,
    ) -> None:
        self.id = f"S{port}"
        self._inst = instrument
        self._port = port
        self.config = config or SourceConfig()

    def configure(self, cfg: SourceConfig) -> None:
        self.config = cfg
        if cfg.func is Func.I_AC:
            self._inst.configure_current_source_ac(
                self._port, cfg.amplitude, cfg.frequency_Hz, cfg.compliance
            )
        elif cfg.func is Func.I_DC:
            self._inst.configure_current_source_dc(
                self._port, cfg.amplitude, cfg.compliance
            )
        elif cfg.func is Func.V_AC:
            self._inst.configure_voltage_source_ac(
                self._port, cfg.amplitude, cfg.frequency_Hz, cfg.compliance
            )
        else:  # Func.V_DC
            self._inst.configure_voltage_source_dc(
                self._port, cfg.amplitude, cfg.compliance
            )

    def enable(self) -> None:
        self._inst.output_on(self._port)

    def disable(self) -> None:
        self._inst.output_off(self._port)


class M81Meter:
    """MeterChannel backed by one M81 measure slot (VM-10 / SMU)."""

    def __init__(
        self,
        instrument: M81Instrument,
        port: int,
        config: MeterConfig | None = None,
        meter_id: str | None = None,
    ) -> None:
        self.id = meter_id or f"M{port}"
        self._inst = instrument
        self._port = port
        self.config = config or MeterConfig()

    def configure(self, cfg: MeterConfig) -> None:
        self.config = cfg
        if cfg.lockin:
            self._inst.configure_measure_lockin(
                self._port,
                reference_source=cfg.reference,
                harmonic=cfg.harmonic,
                time_constant_s=cfg.time_constant_s,
                rolloff=cfg.rolloff,
                phase_shift_deg=cfg.phase_shift_deg,
                use_fir=cfg.use_fir,
            )
        else:
            self._inst.configure_measure_dc(self._port, nplc=cfg.nplc)

    def read(self) -> Reading:
        if self.config.lockin:
            x, y = self._inst.read_lockin_xy(self._port)
            return Reading(x=x, y=y, unit="V")
        dc = self._inst.read_dc_voltage(self._port)
        return Reading(dc=dc, unit="V")


class M81SMUMeter:
    """MeterChannel reading a source-measure unit through its source slot.

    Unlike M81Meter (a VM-10 on a measure slot Mn), this reads the SMU's own
    measurement on a source slot Sn: the current it delivers when voltage-
    sourcing, or the voltage across the load when current-sourcing.  Configure
    is a no-op — the SMU is set up as a source elsewhere.
    """

    def __init__(
        self,
        instrument: M81Instrument,
        source_port: int,
        config: MeterConfig | None = None,
        meter_id: str | None = None,
    ) -> None:
        self.id = meter_id or f"SMU{source_port}"
        self._inst = instrument
        self._port = source_port
        self.config = config or MeterConfig(lockin=False, smu=True)

    def configure(self, cfg: MeterConfig) -> None:
        self.config = cfg

    def read(self) -> Reading:
        value, is_current = self._inst.read_smu(self._port)
        return Reading(dc=value, unit="A" if is_current else "V")
