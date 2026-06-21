"""Instrument registry — decouple channels from any single instrument.

Roadmap step 1 (spec ``docs/specs/01-instrument-registry.md``).  The acquisition
engine is already instrument-agnostic: it consumes arbitrary lists of the
``SourceChannel`` / ``MeterChannel`` Protocols (``core.channels``).  What was
*not* generic was the assembly layer — ``gui.main_window._build_channels`` built
M81 adapters directly.  This module introduces the seam that lets any instrument
become a first-class, file-declared entry resolved by a channel binding, with
the engine and GUI unchanged.

Design (reconciled against the code, not the original spec sketch)
──────────────────────────────────────────────────────────────────
ELECMEAS composes channels *dynamically*: each source/meter carries its own
``SourceConfig`` / ``MeterConfig`` chosen at runtime.  A static
``sources() -> list[SourceChannel]`` enumeration therefore does not fit — the
instrument cannot know the user's ports/configs in advance.  Instead each
``LabInstrument`` is a **factory**: ``make_source(port, cfg)`` /
``make_meter(port, cfg, id)`` build the concrete Protocol on demand.  The
``Registry`` resolves a channel's ``instrument_id`` to its owning instrument and
asks it to make the channel.

A semantic ``role`` tag (excitation / gate / voltage / …) is a *sequencer*
concern with no consumer here; it is deferred to the executor tree (step 4).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.channels import MeterChannel, MeterConfig, SourceChannel, SourceConfig
# The serialization vocabulary (type tags, default ids) lives in the pure-domain
# session module; the registry is the higher instruments layer and imports it.
from core.session import DEFAULT_M81_ID, TYPE_KEITHLEY_7709, TYPE_M81
from instruments.m81 import M81Instrument
from instruments.m81_channels import M81Meter, M81SMUMeter, M81Source
from instruments.matrix7709 import Matrix7709


@runtime_checkable
class Router(Protocol):
    """A contact-routing capability (the Keithley 7709).

    ``Matrix7709`` already satisfies this structurally; the Protocol gives
    ``router()`` a typed return so the worker never names the concrete class.
    """

    settle_s: float

    def connect(self) -> str: ...
    def open_all(self) -> None: ...
    def close(self, channels: list[int]) -> None: ...


@runtime_checkable
class EnvironmentReader(Protocol):
    """Observe-only environment client (temperature/field).

    A deliberately-minimal seam: no instrument implements it this step
    (``environment()`` returns ``None``).  The reader is ported from
    ``M81_electr_meas`` in roadmap step 5; control stays unimplemented.
    """

    def read(self) -> dict[str, float]: ...


@runtime_checkable
class LabInstrument(Protocol):
    """A registry entry: an instrument advertising the capabilities it offers.

    Capabilities not offered return ``None``.  Channel capabilities are
    factories (see module docstring), not static lists.
    """

    id: str
    type: str

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...

    @property
    def connected(self) -> bool: ...

    def make_source(self, port: int, cfg: SourceConfig) -> SourceChannel | None: ...
    def make_meter(
        self, port: int, cfg: MeterConfig, meter_id: str
    ) -> MeterChannel | None: ...

    def router(self) -> Router | None: ...
    def environment(self) -> EnvironmentReader | None: ...


# ── concrete entries ──────────────────────────────────────────────────────────


class M81LabInstrument:
    """Wraps the existing ``M81Instrument`` facade as one registry entry.

    Source/meter factories reproduce ``_build_channels`` verbatim (SMU meter on
    a source slot vs VM-10 meter / lock-in), so M81-only setups are unchanged.
    Offers no router and no environment reader.
    """

    type = TYPE_M81

    def __init__(self, facade: M81Instrument, instrument_id: str = DEFAULT_M81_ID) -> None:
        self.id = instrument_id
        self._facade = facade

    def connect(self) -> None:
        self._facade.connect()

    def disconnect(self) -> None:
        self._facade.disconnect()

    @property
    def connected(self) -> bool:
        return self._facade.connected

    def make_source(self, port: int, cfg: SourceConfig) -> SourceChannel:
        return M81Source(self._facade, port, cfg)

    def make_meter(self, port: int, cfg: MeterConfig, meter_id: str) -> MeterChannel:
        if cfg.smu:
            return M81SMUMeter(self._facade, port, cfg, meter_id=meter_id)
        return M81Meter(self._facade, port, cfg, meter_id=meter_id)

    def router(self) -> Router | None:
        return None

    def environment(self) -> EnvironmentReader | None:
        return None


class Keithley7709LabInstrument:
    """Wraps the ``Matrix7709`` contact-router facade as a routing-only entry."""

    type = TYPE_KEITHLEY_7709

    def __init__(self, matrix: Matrix7709, instrument_id: str = "matrix") -> None:
        self.id = instrument_id
        self._matrix = matrix

    def connect(self) -> None:
        self._matrix.connect()

    def disconnect(self) -> None:
        # Matrix7709 has no explicit disconnect; opening all is the safe teardown.
        self._matrix.open_all()

    @property
    def connected(self) -> bool:
        return getattr(self._matrix, "_connected", False)

    def make_source(self, port: int, cfg: SourceConfig) -> SourceChannel | None:
        return None

    def make_meter(self, port: int, cfg: MeterConfig, meter_id: str) -> MeterChannel | None:
        return None

    def router(self) -> Router | None:
        return self._matrix

    def environment(self) -> EnvironmentReader | None:
        return None


# ── registry ────────────────────────────────────────────────────────────────


class UnknownInstrumentError(KeyError):
    """A channel binding names an instrument absent from the registry."""


class Registry:
    """Holds the session's ``LabInstrument`` set and resolves channel bindings.

    Resolution turns a ``(instrument_id, port, cfg)`` binding into a concrete
    ``SourceChannel`` / ``MeterChannel`` via the owning instrument's factory.
    A ``None`` ``instrument_id`` falls back to :data:`DEFAULT_M81_ID`.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, LabInstrument] = {}

    def add(self, instrument: LabInstrument) -> LabInstrument:
        if instrument.id in self._by_id:
            raise ValueError(f"Duplicate instrument id: {instrument.id!r}")
        self._by_id[instrument.id] = instrument
        return instrument

    def get(self, instrument_id: str) -> LabInstrument:
        try:
            return self._by_id[instrument_id]
        except KeyError as exc:
            raise UnknownInstrumentError(
                f"No instrument {instrument_id!r} in the registry "
                f"(known: {sorted(self._by_id)})"
            ) from exc

    def __contains__(self, instrument_id: str) -> bool:
        return instrument_id in self._by_id

    def __iter__(self):
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def _resolve_id(self, instrument_id: str | None) -> str:
        return instrument_id if instrument_id is not None else DEFAULT_M81_ID

    def resolve_source(
        self, instrument_id: str | None, port: int, cfg: SourceConfig
    ) -> SourceChannel:
        inst = self.get(self._resolve_id(instrument_id))
        ch = inst.make_source(port, cfg)
        if ch is None:
            raise ValueError(f"Instrument {inst.id!r} offers no source channels")
        return ch

    def resolve_meter(
        self, instrument_id: str | None, port: int, cfg: MeterConfig, meter_id: str
    ) -> MeterChannel:
        inst = self.get(self._resolve_id(instrument_id))
        ch = inst.make_meter(port, cfg, meter_id)
        if ch is None:
            raise ValueError(f"Instrument {inst.id!r} offers no meter channels")
        return ch

    def routers(self) -> list[Router]:
        return [r for inst in self for r in (inst.router(),) if r is not None]
