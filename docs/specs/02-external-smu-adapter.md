# Spec 02 — External SMU adapter (Keysight B2902B)

Second work item. Goal: add a Keysight B2902B SMU as a registry instrument, **reusing
`vdp-measure`'s existing SCPI/socket transport**, so it can apply a gate voltage and read
the gate leakage current. Depends on spec 01 (the registry and the `LabInstrument` seam).

> **This spec was written from the `vdp-measure` README and the ELECMEAS
> `ARCHITECTURE.md`, not from line-by-line source.** Class names, method names, and SCPI
> strings below are the intended shape, to be reconciled against the actual code. Where
> reality differs, follow the code and note the discrepancy — do not force the spec.

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

Keep three concerns separate (reconcile with how vdp layers them):

1. **Transport** — a reusable raw-socket SCPI client: `write` / `query`, timeouts,
   termination, `*OPC?` sync. Not B2902B-specific.
2. **Instrument logic** — the B2902B command set: set source mode (V/I) + compliance, set
   measure (V/I/R, range, NPLC, averaging), read, output on/off.
3. **Protocol wrapper** — a `B2902BInstrument` implementing `LabInstrument`, exposing its
   channels as `SourceChannel` / `MeterChannel`.

```python
# Illustrative -- reconcile against vdp-measure's driver and core/channels.py.

class ScpiSocket:
    """Reusable raw-socket SCPI transport (write / query, timeout, *OPC? sync)."""
    def __init__(self, host: str, port: int = 5025, *, simulated: bool = False) -> None: ...
    def write(self, cmd: str) -> None: ...
    def query(self, cmd: str) -> str: ...
    def close(self) -> None: ...

class B2902BInstrument:                # implements LabInstrument (spec 01)
    id: str
    def connect(self, *, simulated: bool) -> None: ...
    def disconnect(self) -> None: ...
    def sources(self) -> list[SourceChannel]: ...   # channels declared as sources
    def meters(self) -> list[MeterChannel]: ...     # channels declared as meters
    def router(self) -> None: ...                   # an SMU has no routing
    def environment(self) -> None: ...              # and no environment
    def safe_disable(self) -> None: ...             # outputs off / 0 V -- before relay switching
```

Whether `safe_disable` belongs on `LabInstrument` or on each `SourceChannel` must match
how spec 01 / the engine expresses the interlock. Reconcile.

### One channel can be source and meter at once

The B2902B has two channels (`ch1`, `ch2`). Each can be a source, a meter, or **both
simultaneously** — a single channel sourcing V and measuring I is the canonical
gate + leakage case. So the same `port: ch1` appears under both `sources` and `meters` in
the Session, and the adapter must support a channel being source and meter at the same
time without conflicting SCPI state.

## Connection & config (Session)

```yaml
instruments:
  - id: gate_smu
    type: keysight_b2902b
    connection: { host: "192.168.0.5", port: 5025, simulated: false }
    # or a VISA resource string if vdp uses pyvisa rather than raw sockets:
    # connection: { resource: "TCPIP0::192.168.0.5::inst0::INSTR", simulated: false }
channels:
  sources:
    - id: gate
      role: gate
      bind: { instrument: gate_smu, port: ch1 }
      source: { function: voltage, compliance_a: 1.0e-6 }   # current compliance = safety limit
  meters:
    - id: gate_leak
      role: leakage
      bind: { instrument: gate_smu, port: ch1 }             # SAME channel: source V, measure I
      measure: { function: current, range: auto, nplc: 1.0 }
```

Field names are illustrative — align with the existing readout-spec fields in ELECMEAS
where they already exist; add new fields only where missing.

## Compliance & safety

- Sourcing voltage requires a **current compliance** limit; sourcing current requires a
  **voltage compliance**. Treat compliance as a mandatory source parameter (a safety
  limit), not optional.
- The SMU is a source, so it participates in the interlock invariant: before any 7709
  relay switch, drive outputs off / to 0 via `safe_disable`. Plug into the same hook the
  engine uses for M81 sources. If no such hook exists yet, establishing a minimal one is
  in scope here — an SMU sourcing into switching relays is a real hazard, and this may be
  broader than the adapter alone.
- On disconnect or error, fail safe (output off).

## Mock mode

Provide a simulated transport (no socket) returning plausible values — e.g. leakage as a
small function of applied V plus noise — so the `simulated` flag from
`connect(*, simulated=True)` works end-to-end. Reuse vdp's mock if it has one.

## Migration (ordered)

1. Reconnaissance (above).
2. Lift vdp's transport into a reusable `ScpiSocket` (or reconcile with what exists);
   keep its B2902B command logic.
3. Implement `B2902BInstrument` as a `LabInstrument`, exposing channels as
   `SourceChannel` / `MeterChannel`, supporting source + meter on one channel.
4. Wire compliance as a mandatory source parameter; implement `safe_disable` and connect
   it to the interlock hook.
5. Register `type: keysight_b2902b` in the registry's instrument factory; extend the
   Session schema (`source` / `measure` blocks) only where those fields are not already
   present.
6. Provide the simulated transport for mock mode.
7. Minimal validation: a preset/sequence that holds a fixed gate voltage and reads
   leakage, in mock (and against hardware if available).

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

## Open questions (confirm against source)

- Does vdp separate socket transport from SCPI-command logic, or combine them? (Determines
  how cleanly it lifts.)
- Raw `host` + `port` socket vs a pyvisa `TCPIP` resource string? Match vdp's approach.
- Where does the engine drive sources to a safe state before routing (the interlock hook
  from spec 01), and is `safe_disable` per-instrument or per-source-channel?
- Are `range` / `nplc` / `averaging` already fields on the existing `MeterChannel` readout
  spec, or new ones to add?
- Single-channel (gate only) or dual-channel use in practice? (Affects default channels
  exposed — but design for both.)
