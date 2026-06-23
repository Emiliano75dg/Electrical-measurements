"""Keysight B2902B SMU adapter — registry instrument for gate drive + leakage.

Spec 02 (``docs/specs/02-external-smu-adapter.md``).  Three layers, mirroring how
``vdp_measure`` already splits them, mapped onto the step-1 registry seam:

- :class:`B2902B`            — the SCPI command set, lifted from
  ``vdp_measure.instruments`` and **extended** with the source-V / measure-I path
  (vdp only did source-I / measure-V).  Granular, per-channel methods; source
  (``:SOUR``) and measure (``:SENS``) live in orthogonal SCPI subsystems, so one
  channel can source V *and* measure I without state conflict.
- :class:`B2902BSource` / :class:`B2902BMeter` — the ``SourceChannel`` /
  ``MeterChannel`` Protocol adapters (``core.channels``) the engine consumes.
  ``B2902BSource.disable()`` is the safe-disable (output off) the interlock calls.
- :class:`B2902BLabInstrument` — the ``LabInstrument`` registry entry whose
  ``make_source`` / ``make_meter`` factories build those channels on demand.

The B2902B is a DC SMU: only ``V_DC`` / ``I_DC`` source functions are supported
(AC / lock-in is a non-goal — see the spec).  Compliance is a mandatory safety
limit: sourcing V applies a current compliance, sourcing I a voltage compliance.
"""

from __future__ import annotations

from core.channels import Func, MeterConfig, Reading, SourceConfig
from instruments.scpi import DryRunTransport, SocketTransport, Transport

DEFAULT_SCPI_PORT = 5025


def parse_host_port(resource: str, default_port: int = DEFAULT_SCPI_PORT) -> tuple[str, int]:
    """Split an ``InstrumentSpec.resource`` into ``(host, port)``.

    Accepts a bare host (``"192.168.0.5"``) or ``"host:port"``.  No VISA resource
    strings — the B2902B is reached over a raw socket (spec 02, note 3).
    """
    resource = resource.strip()
    host, sep, port = resource.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return resource, default_port


def _first_numeric_field(response: str) -> str:
    return response.strip().split(",", 1)[0].strip()


class B2902B:
    """B2902B SCPI command set over a :class:`Transport`, bound to one channel.

    Stateless beyond the channel number: several instances may share a transport
    (e.g. a source adapter and a meter adapter on the same channel), each
    addressing ``:SOUR{ch}`` / ``:SENS{ch}`` / ``:OUTP{ch}``.
    """

    def __init__(self, transport: Transport, channel: int = 1) -> None:
        self.transport = transport
        self.channel = channel

    # ── identity / lifecycle ────────────────────────────────────────────────────
    def identify(self) -> str:
        return self.transport.query("*IDN?")

    def reset(self) -> None:
        self.transport.write("*RST")
        self.transport.query("*OPC?")

    # ── source mode + compliance (mandatory safety limit) ───────────────────────
    def set_source_mode_voltage(self) -> None:
        self.transport.write(f":SOUR{self.channel}:FUNC:MODE VOLT")

    def set_source_mode_current(self) -> None:
        self.transport.write(f":SOUR{self.channel}:FUNC:MODE CURR")

    def set_current_compliance(self, compliance_a: float) -> None:
        """Current compliance — the safety limit while sourcing voltage."""
        self.transport.write(f":SENS{self.channel}:CURR:PROT {compliance_a:.12g}")

    def set_voltage_compliance(self, compliance_v: float) -> None:
        """Voltage compliance — the safety limit while sourcing current."""
        self.transport.write(f":SENS{self.channel}:VOLT:PROT {compliance_v:.12g}")

    # ── source level ────────────────────────────────────────────────────────────
    def set_voltage(self, voltage_v: float) -> None:
        self.transport.write(f":SOUR{self.channel}:VOLT {voltage_v:.12g}")

    def set_current(self, current_a: float) -> None:
        self.transport.write(f":SOUR{self.channel}:CURR {current_a:.12g}")

    # ── measure setup + read ────────────────────────────────────────────────────
    def configure_measure_current(self, *, nplc: float = 1.0) -> None:
        self.transport.write(f':SENS{self.channel}:FUNC "CURR"')
        self.transport.write(f":SENS{self.channel}:CURR:NPLC {nplc:.12g}")
        self.transport.write(f":SENS{self.channel}:CURR:RANG:AUTO ON")

    def configure_measure_voltage(self, *, nplc: float = 1.0) -> None:
        self.transport.write(f':SENS{self.channel}:FUNC "VOLT"')
        self.transport.write(f":SENS{self.channel}:VOLT:NPLC {nplc:.12g}")
        self.transport.write(f":SENS{self.channel}:VOLT:RANG:AUTO ON")

    def measure_current(self) -> float:
        return float(_first_numeric_field(self.transport.query(f":MEAS:CURR? (@{self.channel})")))

    def measure_voltage(self) -> float:
        return float(_first_numeric_field(self.transport.query(f":MEAS:VOLT? (@{self.channel})")))

    def current_compliance_tripped(self) -> bool:
        response = self.transport.query(f":SENS{self.channel}:CURR:PROT:TRIP?")
        return bool(int(float(response.strip())))

    def voltage_compliance_tripped(self) -> bool:
        response = self.transport.query(f":SENS{self.channel}:VOLT:PROT:TRIP?")
        return bool(int(float(response.strip())))

    # ── output ──────────────────────────────────────────────────────────────────
    def output_on(self) -> None:
        self.transport.write(f":OUTP{self.channel} ON")

    def output_off(self) -> None:
        self.transport.write(f":OUTP{self.channel} OFF")


# ── channel adapters (SourceChannel / MeterChannel Protocols) ────────────────────


class B2902BSource:
    """``SourceChannel`` over one B2902B channel (DC voltage or current source).

    Owns the source subsystem and the compliance limit; the meter adapter (if the
    same channel is also a meter) owns the sense function.  ``disable()`` is the
    interlock's safe-disable (output off).
    """

    def __init__(self, owner: "B2902BLabInstrument", port: int, config: SourceConfig | None = None) -> None:
        self.id = f"SMU{port}"
        self._owner = owner
        self._port = port
        self.config = config or SourceConfig()

    def configure(self, cfg: SourceConfig) -> None:
        if cfg.compliance <= 0:
            raise ValueError("B2902B source requires a positive compliance (safety limit)")
        self.config = cfg
        cmd = self._owner.channel(self._port)
        if cfg.func is Func.V_DC:
            cmd.set_source_mode_voltage()
            cmd.set_current_compliance(cfg.compliance)   # A — leakage safety limit
            cmd.set_voltage(cfg.amplitude)
        elif cfg.func is Func.I_DC:
            cmd.set_source_mode_current()
            cmd.set_voltage_compliance(cfg.compliance)   # V
            cmd.set_current(cfg.amplitude)
        else:
            raise ValueError(f"B2902B is a DC SMU; AC function {cfg.func.value!r} is unsupported")

    def enable(self) -> None:
        self._owner.channel(self._port).output_on()

    def disable(self) -> None:
        # Safe-disable (CLAUDE.md invariant 5 / spec 02): output off before any
        # relay switch and on teardown.
        self._owner.channel(self._port).output_off()


class B2902BMeter:
    """``MeterChannel`` over one B2902B channel — reads the leakage current.

    Canonical gate+leakage case: the same channel sources V and this meter reads I
    (``Reading.dc`` in amps).  Owns only the sense subsystem, so it composes with a
    :class:`B2902BSource` on the same channel without overwriting source state.
    """

    def __init__(
        self,
        owner: "B2902BLabInstrument",
        port: int,
        config: MeterConfig | None = None,
        meter_id: str | None = None,
    ) -> None:
        self.id = meter_id or f"SMU{port}"
        self._owner = owner
        self._port = port
        self.config = config or MeterConfig(lockin=False)

    def configure(self, cfg: MeterConfig) -> None:
        self.config = cfg
        self._owner.channel(self._port).configure_measure_current(nplc=cfg.nplc)

    def read(self) -> Reading:
        current = self._owner.channel(self._port).measure_current()
        return Reading(dc=current, unit="A")


# ── registry entry (LabInstrument) ───────────────────────────────────────────────


class B2902BLabInstrument:
    """A B2902B as one registry entry: source / meter factories over one transport.

    The simulated path builds a :class:`DryRunTransport` (no socket); the real path
    a :class:`SocketTransport`.  Channels are made on demand and share the
    transport — so a source and a meter on the *same* port both drive that channel.
    """

    type = "keysight_b2902b"

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_SCPI_PORT,
        *,
        simulated: bool = True,
        instrument_id: str = "gate_smu",
    ) -> None:
        self.id = instrument_id
        self._host = host
        self._port = port
        self._simulated = simulated
        self._transport: Transport | None = None
        self._connected = False

    def connect(self) -> None:
        if self._simulated:
            self._transport = DryRunTransport(name=self.id)
        else:
            self._transport = SocketTransport(self._host, self._port)
        self._connected = True

    def disconnect(self) -> None:
        # Fail safe: drive both channels' outputs off before closing the link.
        if self._transport is not None:
            for ch in (1, 2):
                try:
                    B2902B(self._transport, ch).output_off()
                except Exception:  # noqa: BLE001 — teardown is best-effort
                    pass
            self._transport.close()
        self._transport = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def channel(self, port: int) -> B2902B:
        """Build a command object for ``port`` over the live transport."""
        if self._transport is None:
            raise RuntimeError(f"B2902B {self.id!r} is not connected")
        return B2902B(self._transport, channel=port)

    def make_source(self, port: int, cfg: SourceConfig) -> B2902BSource:
        return B2902BSource(self, port, cfg)

    def make_meter(self, port: int, cfg: MeterConfig, meter_id: str) -> B2902BMeter:
        return B2902BMeter(self, port, cfg, meter_id=meter_id)

    def router(self) -> None:
        return None

    def environment(self) -> None:
        return None
