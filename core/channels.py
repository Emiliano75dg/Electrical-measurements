"""Typed measurement channels — hardware-agnostic abstractions.

These Protocols and dataclasses decouple the acquisition engine from any
specific instrument.  Concrete adapters (e.g. instruments/m81_channels.py) bind
them to real modules; the engine and GUI only ever see SourceChannel /
MeterChannel and their config/reading dataclasses.

See REDESIGN.md §3.2 for the agreed interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class Func(str, Enum):
    """Source function: current/voltage × AC/DC."""

    I_AC = "I_AC"
    I_DC = "I_DC"
    V_AC = "V_AC"
    V_DC = "V_DC"

    @property
    def is_ac(self) -> bool:
        return self in (Func.I_AC, Func.V_AC)

    @property
    def is_current(self) -> bool:
        return self in (Func.I_AC, Func.I_DC)


@dataclass
class SourceConfig:
    func: Func = Func.I_AC
    amplitude: float = 1e-6        # A if current source, V if voltage source
    frequency_Hz: float = 17.77    # AC only
    compliance: float = 1.0        # V if I-source, A if V-source


@dataclass
class MeterConfig:
    lockin: bool = True
    reference: str | None = "S1"   # which AC source provides the lock-in reference
    harmonic: int = 1
    time_constant_s: float = 0.3
    rolloff: str = "R24"
    phase_shift_deg: float = 0.0
    use_fir: bool = True
    nplc: float = 1.0              # DC mode
    smu: bool = False             # read the source-measure unit on a source slot (not a VM-10)


@dataclass
class Reading:
    x: float = 0.0                 # AC in-phase (V)
    y: float = 0.0                 # AC quadrature (V)
    dc: float = 0.0               # DC (V)
    unit: str = "V"


@runtime_checkable
class SourceChannel(Protocol):
    id: str
    config: SourceConfig

    def configure(self, cfg: SourceConfig) -> None: ...
    def enable(self) -> None: ...
    def disable(self) -> None: ...


@runtime_checkable
class MeterChannel(Protocol):
    id: str
    config: MeterConfig

    def configure(self, cfg: MeterConfig) -> None: ...
    def read(self) -> Reading: ...
