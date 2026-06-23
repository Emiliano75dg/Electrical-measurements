"""SCPI transport over a raw TCP socket — lifted from ``vdp_measure.scpi``.

Spec 02 (``docs/specs/02-external-smu-adapter.md``).  The van der Pauw repo
already drives instruments over a socket behind a small ``Transport`` Protocol
with a separate dry-run.  We **lift that triad verbatim** rather than rewrite the
SCPI, so the B2902B adapter (and a future sibling SMU) reuses tested transport:

- ``Transport``        — Protocol: ``write`` / ``query`` / ``close``.
- ``SocketTransport``  — real raw socket with timeout, retry + backoff.
- ``DryRunTransport``  — simulated transport (no socket): logs commands,
  remembers source state, answers queries via :func:`_default_response`.

This module is **not** B2902B-specific: it carries no instrument command logic,
only bytes in/out.  Picking the simulated path is a *choice of transport*
(``DryRunTransport`` vs ``SocketTransport``), not a flag inside one class.

Reconciliation note (spec 02, note 7): the upstream dry-run only remembered
``:SOUR:CURR``.  We extend it symmetrically to also remember ``:SOUR:VOLT`` and
answer ``:MEAS:CURR?`` with an Ohmic leakage ``V / R`` — keeping the response
model a plain, deterministic Ohm's law (good for tests), still instrument-
agnostic.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


class Transport(Protocol):
    def write(self, command: str) -> None:
        ...

    def query(self, command: str) -> str:
        ...

    def close(self) -> None:
        ...


@dataclass
class DryRunTransport:
    """Simulated transport: no socket, deterministic Ohm's-law responses.

    Remembers the last programmed source level (current *or* voltage) and which
    function was programmed, so a measure query returns the conjugate quantity by
    Ohm's law: a current source reads back ``I`` and ``I·R``; a voltage source
    reads back ``V`` and the leakage ``V / R``.
    """

    name: str = "DRY"
    log: list[str] = field(default_factory=list)
    current_a: float = 0.0
    voltage_v: float = 0.0
    dry_run_resistance_ohm: float = 100.0
    source_mode: str = "curr"          # "curr" | "volt" — whichever was last set

    def write(self, command: str) -> None:
        self.log.append(f"{self.name} << {command}")
        self._remember_state(command)

    def query(self, command: str) -> str:
        self.log.append(f"{self.name} << {command}")
        response = _default_response(
            command,
            current_a=self.current_a,
            voltage_v=self.voltage_v,
            dry_run_resistance_ohm=self.dry_run_resistance_ohm,
            source_mode=self.source_mode,
        )
        self.log.append(f"{self.name} >> {response}")
        return response

    def close(self) -> None:
        self.log.append(f"{self.name} -- close")

    def _remember_state(self, command: str) -> None:
        normalized = command.strip().upper()
        if ":SOUR" not in normalized:
            return
        parts = command.strip().split()
        if len(parts) < 2:
            return
        try:
            level = float(parts[-1])
        except ValueError:
            return
        # A level command is ``:SOURn:VOLT <num>`` / ``:SOURn:CURR <num>``.  The
        # mode command ``:SOURn:FUNC:MODE VOLT`` has no ``:VOLT``/``:CURR`` token
        # and its trailing field is non-numeric, so it is ignored above.
        if ":VOLT" in normalized:
            self.voltage_v = level
            self.source_mode = "volt"
        elif ":CURR" in normalized:
            self.current_a = level
            self.source_mode = "curr"


class SocketTransport:
    """TCP socket transport for SCPI commands with retry logic and error handling.

    Automatically retries on transient timeouts (up to max_retries times).
    Fails fast on connection refused, broken pipe, and other permanent errors.
    """

    def __init__(
        self,
        host: str,
        port: int = 5025,
        *,
        timeout_s: float = 5.0,
        termination: str = "\n",
        read_buffer_bytes: int = 65536,
        max_retries: int = 3,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._termination = termination.encode("ascii")
        self._read_buffer_bytes = read_buffer_bytes
        self._max_retries = max_retries
        self._socket: socket.socket | None = None

        try:
            self._socket = socket.create_connection((host, port), timeout=timeout_s)
            self._socket.settimeout(timeout_s)
            logger.debug("Connected to %s:%s", host, port)
        except ConnectionRefusedError:
            logger.error("Connection refused to %s:%s - is instrument online?", host, port)
            raise
        except socket.timeout:
            logger.error("Socket timeout during connect to %s:%s", host, port)
            raise
        except OSError as exc:
            logger.error("Socket error during connect to %s:%s: %s", host, port, exc)
            raise

    def write(self, command: str) -> None:
        logger.debug("SCPI write: %s", command)
        for attempt in range(1, self._max_retries + 2):
            try:
                self._socket.sendall(command.encode("ascii") + self._termination)
                return
            except socket.timeout:
                if attempt <= self._max_retries:
                    wait_s = 2 ** (attempt - 1) * 0.1
                    logger.warning("Socket timeout on write attempt %s, retrying in %.2fs", attempt, wait_s)
                    time.sleep(wait_s)
                else:
                    logger.error("Socket timeout on write (failed after %s attempts)", self._max_retries + 1)
                    raise
            except (BrokenPipeError, ConnectionRefusedError):
                logger.error("Connection error on write")
                raise
            except OSError as exc:
                logger.error("OSError on write: %s", exc)
                raise

    def query(self, command: str) -> str:
        logger.debug("SCPI query: %s", command)
        for attempt in range(1, self._max_retries + 2):
            try:
                self._socket.sendall(command.encode("ascii") + self._termination)
                response = (
                    self._socket.recv(self._read_buffer_bytes)
                    .decode("ascii", errors="replace")
                    .strip()
                )
                if not response:
                    logger.warning("Empty response to query: %s", command)
                    raise ValueError(f"Empty response to query '{command}'")
                logger.debug("SCPI response: %s", response)
                return response
            except socket.timeout:
                if attempt <= self._max_retries:
                    wait_s = 2 ** (attempt - 1) * 0.1
                    logger.warning("Socket timeout on query attempt %s, retrying in %.2fs", attempt, wait_s)
                    time.sleep(wait_s)
                else:
                    logger.error("Socket timeout on query (failed after %s attempts)", self._max_retries + 1)
                    raise
            except (BrokenPipeError, ConnectionRefusedError):
                logger.error("Connection error on query")
                raise
            except OSError as exc:
                logger.error("OSError on query: %s", exc)
                raise

    def close(self) -> None:
        if self._socket:
            try:
                self._socket.close()
                logger.debug("Socket closed")
            except Exception as exc:  # noqa: BLE001 — close is best-effort
                logger.warning("Error closing socket: %s", exc)


def _default_response(
    command: str,
    *,
    current_a: float = 0.0,
    voltage_v: float = 0.0,
    dry_run_resistance_ohm: float = 100.0,
    source_mode: str = "curr",
) -> str:
    normalized = command.strip().upper()
    if normalized == "*IDN?":
        return "DRY,RUN,0,0"
    if normalized == "*LANG?":
        return "SCPI"
    if normalized == "*OPC?":
        return "1"
    if normalized.endswith(":ERR?") or normalized == "SYST:ERR?":
        return '0,"No error"'
    if "PROT:TRIP?" in normalized or "PROTECTION:TRIPPED?" in normalized:
        return "0"
    r = dry_run_resistance_ohm or 1.0
    if "MEAS:VOLT" in normalized or "MEASURE:VOLT" in normalized:
        voltage = voltage_v if source_mode == "volt" else current_a * r
        return f"{voltage:.12g}"
    if "MEAS:CURR" in normalized or "MEASURE:CURR" in normalized:
        current = voltage_v / r if source_mode == "volt" else current_a
        return f"{current:.12g}"
    if "FETC" in normalized or "READ" in normalized:
        voltage = voltage_v if source_mode == "volt" else current_a * r
        current = voltage_v / r if source_mode == "volt" else current_a
        return f"{voltage:.12g},{current:.12g},0"
    return "0"
