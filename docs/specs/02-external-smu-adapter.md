# Spec 02 — External SMU adapter (Keysight B2902B)

Second work item. Goal: add a Keysight B2902B SMU as a registry instrument, **reusing
`vdp-measure`'s existing SCPI/socket transport**, so it can apply a gate voltage and read
the gate leakage current. Depends on spec 01 (the registry and the `LabInstrument` seam).

> **This spec was written from the `vdp-measure` README and the ELECMEAS
> `ARCHITECTURE.md`, not from line-by-line source.** Class names, method names, and SCPI
> strings below were the intended shape; they have now been **reconciled against the
> actual code** (see "Reconciliation notes"). Where the original sketch differed from
> reality, this document follows the code.

## Reconciliation notes (what the code actually shows)

Reconnaissance against `../Vdp/src/vdp_measure/` and the ELECMEAS step-1 seam established:

1. **The transport is already behind a Protocol with a separate dry-run** — there is no
   `ScpiSocket` to build. `scpi.py` already provides `Transport` (Protocol:
   `write`/`query`/`close`), `SocketTransport` (real raw socket, retry + backoff), and
   `DryRunTransport` (mock: logs commands, remembers state, answers via
   `_default_response`). We **lift this triad**; the `simulated` flag is the *choice*
   between constructing `SocketTransport` vs `DryRunTransport`, not an attribute of one
   class.
2. **vdp drives the B2902B the opposite way to our case.** vdp does *source I / measure V*
   (`configure_current_source`, voltage compliance). Our gate+leakage case is *source V /
   measure I*. The SCPI subsystems are symmetric, but **`configure_voltage_source` does
   not exist in vdp** — that inverse command path is the one piece of new B2902B logic to
   add, mirroring the existing one. The transport is **not** rewritten.
3. **Raw socket, not pyvisa.** vdp uses `host` + `port` (default 5025), no VISA resource
   string. The session `InstrumentSpec` already carries `resource` + `simulated`; the host
   (optionally `host:port`) goes in `resource`. **No new schema fields, no schema-version
   bump** — the v2 `instruments` block + per-channel `instrument_id` already accommodate a
   new `type`.
4. **`safe_disable` is per-source-channel, not per-instrument.** The engine only knows the
   `SourceChannel` Protocol (`enable()`/`disable()`); it has no per-instrument hook. So the
   B2902B's safe-disable *is* its `SourceChannel.disable()` (output off). A
   `LabInstrument.safe_disable()` would be dead weight the engine never calls — dropped, to
   keep the engine instrument-agnostic (invariant 1).
5. **The interlock hook does not yet exist.** `engine._route()` does `open_all()` →
   `close(channels)` only; it never disables sources before a relay switch nor re-enables
   after (sources are disabled only in run() teardown). Invariant 5 is therefore *not*
   enforced during routing. Establishing the minimal hook (disable → open → switch →
   re-enable, channel-level on `SourceChannel`) is in scope here and touches the **shared**
   engine path used by M81/vdP runs — broader than the adapter alone, applied to all
   sources so it stays instrument-agnostic.
6. **A `build_instrument(spec)` factory is missing.** The GUI assembles the registry by
   hand from live facades and ignores `session.instruments`. For "B2902B declared in a
   Session and resolved by the registry" we add a small `InstrumentSpec -> LabInstrument`
   factory (type → entry). Full GUI rewiring to auto-connect file-declared instruments is
   the larger integration and is a follow-up; this step proves the path via the factory +
   a headless/mock run.
7. **Mock parity uses `DryRunTransport`.** Its state memory only tracks `:SOUR:CURR`; we
   extend the lifted copy to also remember `:SOUR:VOLT` and answer `MEAS:CURR` with an
   Ohmic leakage `V/R` (symmetric, kept transport-generic, deterministic for tests).

## Reconnaissance (read first)

Before writing anything, read and summarize:

- `vdp-measure` (repo `vdp-measure`, cloned locally at `../Vdp/src/vdp_measure/` —
  transport in `scpi.py`, B2902B command set in `instruments.py`, analogous to how
  `m81.py` hooks `../M81_electr_meas/src`) — the SMU driver module (the B2902B SCPI command
  set), the socket / transport layer, the Pydantic instrument config, and any
  mock/simulated path. Capture exact class/method names and the actual SCPI command strings
  used.
- ELECMEAS — `core/channels.py` (the `SourceChannel` / `MeterChannel` Protocols this
  adapter implements), the `LabInstrument` Protocol and `Registry` from spec 01, how the
  `simulated` flag is plumbed, and **where the engine drives sources to a safe state
  before routing** (the interlock hook).
- Report what you found and where this spec diverges before implementing.

If `vdp-measure` is not available to you, that is a blocker — the transport cannot be
lifted. Resolve access before proceeding (see `CLAUDE.md`).

## What we are reusing

`vdp-measure` already drives a B2902B over SCPI on a socket (repo `vdp-measure`, cloned in
`../Vdp/src/vdp_measure/`: `scpi.py` + `instruments.py`). **Lift that transport and
command logic; do not rewrite the SCPI.** The new work is wrapping it behind the spec-01
seam (`LabInstrument` + the existing channel Protocols), plus three things vdp may not
express in this form: current/voltage **compliance** as a safety limit, a **safe-disable**
path tied to the interlock, and **mock parity** with ELECMEAS's simulated mode.

## Target design

Three concerns stay separate, mapped onto what the code already provides:

1. **Transport** — the **lifted** `Transport` / `SocketTransport` / `DryRunTransport`
   triad from `vdp_measure.scpi` (raw socket, `write` / `query` / `close`). Not
   B2902B-specific; ready for a sibling SMU (e.g. Keithley).
2. **Instrument logic** — the B2902B command set, lifted from `vdp_measure.instruments`
   and **extended** with the source-V / measure-I path (`configure_voltage_source` +
   current measure): set source mode (V/I) + compliance, set measure (NPLC; auto-range),
   read, output on/off.
3. **Protocol wrapper** — a `B2902BLabInstrument` implementing `LabInstrument` (spec 01),
   whose `make_source` / `make_meter` factories build `B2902BSource` / `B2902BMeter`
   exposing the channel as `SourceChannel` / `MeterChannel`.

```python
# Reconciled against vdp_measure (scpi.py / instruments.py) and core/channels.py.

# Transport: lifted as-is into instruments/scpi.py (not rewritten).
class Transport(Protocol):
    def write(self, command: str) -> None: ...
    def query(self, command: str) -> str: ...
    def close(self) -> None: ...
# SocketTransport(host, port=5025, ...) for hardware; DryRunTransport(name) for mock.

class B2902BLabInstrument:              # implements LabInstrument (spec 01)
    id: str
    type: str                          # "keysight_b2902b"
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...   # fail-safe: outputs off
    def make_source(self, port: int, cfg: SourceConfig) -> SourceChannel: ...
    def make_meter(self, port, cfg, meter_id) -> MeterChannel: ...   # same port → source+meter
    def router(self) -> None: ...       # an SMU has no routing
    def environment(self) -> None: ...  # and no environment
```

`safe_disable` lives on the **source channel**: `B2902BSource.disable()` turns the output
off (the safe state). The engine already drives `SourceChannel.disable()` — so the SMU
participates in the interlock with no per-instrument method. (See Reconciliation note 4.)

### One channel can be source and meter at once

The B2902B has two channels (`ch1`, `ch2`). Each can be a source, a meter, or **both
simultaneously** — a single channel sourcing V and measuring I is the canonical
gate + leakage case. So the same `port: ch1` appears under both `sources` and `meters` in
the Session, and the adapter must support a channel being source and meter at the same
time without conflicting SCPI state.

## Connection & config (Session)

The real schema (step-1 v2) is JSON with an `instruments` block plus `sources`/`meters`
carrying typed `SourceConfig`/`MeterConfig` and an optional `instrument_id` binding. The
B2902B is a raw socket, so its host goes in `resource` (port defaults to 5025; an explicit
`host:port` is accepted). The channel number (1/2) is the `port`. No new schema fields.

```jsonc
{
  "schema_version": 2,
  "instruments": [
    { "id": "gate_smu", "type": "keysight_b2902b",
      "connection": { "resource": "192.168.0.5", "simulated": false } }
  ],
  "sources": [
    // gate: source V_DC on channel 1; compliance = current safety limit (A)
    { "port": 1, "instrument_id": "gate_smu",
      "config": { "func": "V_DC", "amplitude": 0.0, "compliance": 1.0e-6 } }
  ],
  "meters": [
    // leakage: SAME channel 1, measure I; reuses the existing nplc field, auto-range
    { "port": 1, "meter_id": "gate_leak", "instrument_id": "gate_smu",
      "config": { "lockin": false, "nplc": 1.0 } }
  ]
}
```

This reuses existing fields: `SourceConfig.func` (`V_DC`) / `amplitude` (the gate setpoint,
V) / `compliance` (A); `MeterConfig.nplc`. `range` / `averaging` are **not** existing
fields — the adapter uses auto-range and does not add them this step.

## Compliance & safety

- Sourcing voltage requires a **current compliance** limit; sourcing current requires a
  **voltage compliance**. Treat compliance as a mandatory source parameter (a safety
  limit), not optional.
- The SMU is a source, so it participates in the interlock invariant: before any 7709
  relay switch, drive outputs off via the source channel's `disable()`. **No such hook
  exists yet** — `engine._route()` opens/closes relays without disabling sources (see
  Reconciliation note 5). Establishing the minimal one (disable all sources → open → close
  → re-enable, channel-level so it stays instrument-agnostic) is in scope here and applies
  to every source, including the existing M81 path — broader than the adapter alone.
- On disconnect or error, fail safe (output off).

## Mock mode

Provide a simulated transport (no socket) returning plausible values — e.g. leakage as a
small function of applied V plus noise — so the `simulated` flag from
`connect(*, simulated=True)` works end-to-end. Reuse vdp's mock if it has one.

## Migration (ordered)

1. Reconnaissance (done — see Reconciliation notes).
2. Lift vdp's transport triad (`Transport` / `SocketTransport` / `DryRunTransport`) into
   `instruments/scpi.py`; keep the B2902B command logic, adding the source-V / measure-I
   path.
3. Implement `B2902BLabInstrument` as a `LabInstrument` with `make_source` / `make_meter`
   factories building `B2902BSource` / `B2902BMeter`, supporting source + meter on one
   channel (no SCPI state conflict).
4. Wire compliance as a mandatory source parameter; `B2902BSource.disable()` is the
   safe-disable; add the minimal interlock ordering to `engine._route()`.
5. Add the `type: keysight_b2902b` tag (`core.session`) and a `build_instrument(spec)`
   factory in the registry. **No Session schema-version bump** — existing v2 fields suffice.
6. Extend the lifted `DryRunTransport` for mock leakage (V→I), so `simulated=True` works
   end-to-end.
7. Minimal validation: a fixed gate voltage + leakage read, in mock (and against hardware
   if available).

## Non-goals (this step)

- Other SMUs (Keithley 2400/2450) — but keep the transport / instrument-logic split so a
  sibling adapter is straightforward; do **not** hardcode B2902B assumptions into the
  reusable transport.
- The sequencer / executor tree (roadmap step 4). Gate *sweeping* is a `Loop` concern
  there; this step only needs the SMU to hold a setpoint and read.
- AC / lock-in / `StreamStep` — the B2902B is a DC SMU and is a point-by-point
  (`PointStep`) instrument.

## Acceptance criteria

- A B2902B can be declared in a Session and resolved by the registry.
- One channel can be bound simultaneously as a source (gate V) and a meter (leakage I)
  without SCPI state conflict.
- Compliance is configurable and applied; output fails safe on disconnect/error.
- `safe_disable` is invoked before relay switching (interlock honored).
- Mock mode works end-to-end via the simulated transport.
- A fixed-gate + leakage-read run produces sensible output in mock.
- Tests cover: SCPI command formatting; source/measure round-trip in mock; compliance
  applied; safe-disable; registry resolution of the same-channel source + meter binding;
  B2902B-as-registry-entry alongside the M81.

## Open questions — resolved against source

- **Separate transport vs combined?** Separate: `scpi.py` (transport, behind `Transport`
  Protocol) and `instruments.py` (B2902B command logic) are distinct. Lifts cleanly.
- **Raw socket vs pyvisa?** Raw `host` + `port` (5025). No pyvisa for the SMU. Host goes in
  `InstrumentSpec.resource`.
- **Interlock hook / per-instrument vs per-channel?** No hook exists; we add it in
  `engine._route()`. Safe-disable is **per-source-channel** (`SourceChannel.disable()`) —
  the only seam the engine uses.
- **`range` / `nplc` / `averaging` existing fields?** Only `nplc` exists (`MeterConfig`).
  `range`/`averaging` are not added; the adapter auto-ranges.
- **Single vs dual channel?** Design for both: channel = `port` (1/2). The canonical case
  is one channel as source+meter (gate+leakage); a second channel is just another binding.
