"""Keithley 7709 matrix driver — pure router of sample contacts (REDESIGN.md §2.2).

The 7709 lives in a DAQ6510 and is reached over SCPI via pyvisa.  Used here in
the single-pole scheme (only HI pins wired): we only ever open everything or
close an explicit list of channel numbers — the DMM and its channels (49/50)
stay out because ``:ROUT:MULT:CLOS`` closes *only* the listed channels.

Two backends, mirroring instruments/m81.py:
  * real      — pyvisa SCPI to the DAQ6510
  * simulated — MatrixMock, records closed channels, no hardware / no pyvisa

Relays are latching but ``*RST`` opens them after a few seconds, so we always
``open_all()`` at setup and teardown (the acquisition engine does this around
every route step).
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger(__name__)


class MatrixMock:
    """Simulated 7709 — just remembers which channels are closed."""

    def __init__(self) -> None:
        self.closed: set[int] = set()

    def open_all(self) -> None:
        self.closed.clear()

    def close(self, channels: list[int]) -> None:
        self.closed = set(channels)

    @property
    def closed_channels(self) -> list[int]:
        return sorted(self.closed)


class Matrix7709:
    """Thin SCPI/pyvisa facade over the 7709 matrix in a DAQ6510.

    Parameters
    ----------
    resource:
        VISA resource string (e.g. 'TCPIP0::192.168.0.2::inst0::INSTR').
        Ignored when simulated.
    simulated:
        Use MatrixMock — no pyvisa, no hardware.
    settle_s:
        Dwell after each relay operation to let mechanical relays settle.
    """

    def __init__(self, resource: str = "", simulated: bool = True, settle_s: float = 0.05) -> None:
        self._resource = resource
        self._simulated = simulated
        self.settle_s = settle_s
        self._dev: object | None = None
        self._mock: MatrixMock | None = None
        self._connected = False

    # ── connection ─────────────────────────────────────────────────────────────

    def connect(self) -> str:
        if self._simulated:
            self._mock = MatrixMock()
            self._connected = True
            return "KEITHLEY,7709,MOCK"
        try:
            import pyvisa
        except ImportError as exc:
            raise RuntimeError("pyvisa not installed — cannot use the real matrix.") from exc
        rm = pyvisa.ResourceManager()
        self._dev = rm.open_resource(self._resource)
        self._connected = True
        self.open_all()
        idn = self._query("*IDN?").strip()
        return idn or f"KEITHLEY,7709,{self._resource}"

    def disconnect(self) -> None:
        if self._connected:
            try:
                self.open_all()
            except Exception:
                pass
        dev = self._dev
        if dev is not None and hasattr(dev, "close"):
            try:
                dev.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._dev = None
        self._mock = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def simulated(self) -> bool:
        return self._simulated

    # ── relay control ──────────────────────────────────────────────────────────

    def open_all(self) -> None:
        """Open every relay (:ROUT:OPEN:ALL)."""
        if self._simulated:
            assert self._mock is not None
            self._mock.open_all()
            return
        self._write(":ROUT:OPEN:ALL")

    def close(self, channels: list[int]) -> None:
        """Close exactly the given channels (:ROUT:MULT:CLOS), leaving others open."""
        if not channels:
            return
        if self._simulated:
            assert self._mock is not None
            self._mock.close(channels)
            return
        joined = ",".join(str(c) for c in channels)
        self._write(f":ROUT:MULT:CLOS (@{joined})")

    @property
    def closed_channels(self) -> list[int]:
        """Channels currently closed (mock only; [] for real hardware)."""
        if self._simulated and self._mock is not None:
            return self._mock.closed_channels
        return []

    # ── SCPI helpers ───────────────────────────────────────────────────────────

    def _write(self, cmd: str) -> None:
        LOGGER.debug("7709 ← %s", cmd)
        self._dev.write(cmd)  # type: ignore[union-attr]

    def _query(self, cmd: str) -> str:
        return self._dev.query(cmd)  # type: ignore[union-attr]
