# Spec 01 — Instrument registry

First work item on the roadmap. Goal: decouple channels from the M81 so that any
instrument — including a separate SMU used as a gate — is a first-class entry in a
registry, without touching the acquisition engine.

> **This spec was written from the ELECMEAS README and `ARCHITECTURE.md`, not from
> line-by-line source.** Class and method names below are the intended shape, to be
> reconciled against the actual code. Where reality differs, follow the code and note
> the discrepancy — do not force the spec.

## Reconnaissance (read first)

Before writing anything, read and summarize:

- `core/channels.py` — the exact `SourceChannel` / `MeterChannel` Protocol definitions
  (method names, signatures). The registry returns these unchanged.
- `core/session.py` — the session schema, how it (de)serializes, and the existing
  versioning / migration mechanism (if any).
- `gui/main_window.py` — specifically `_build_channels` (or its equivalent): how tabs map
  to M81 adapters today. This is the seam to redirect to the registry.
- The M81 adapter module — what to wrap as a single registry entry.
- The 7709 routing module — whether routing already sits behind an interface or is called
  directly.

Report what you found and where this spec diverges from it before implementing.

## Current state

Channels are implicitly M81: `_build_channels` constructs M81 source/meter adapters
directly from the GUI tabs. Adding any non-M81 instrument today means touching the
engine, the schema, and the GUI. The engine itself is already generic (it consumes
arbitrary `SourceChannel` / `MeterChannel` lists), so the fix is an *assembly-layer*
change upstream of the engine — the acquisition worker does not need to change.

## Target design

A capability interface every instrument implements, returning the existing Protocols (or
`None` / `[]` when a capability is not offered):

```python
# Illustrative -- reconcile against core/channels.py before implementing.

class LabInstrument(Protocol):
    id: str

    def connect(self, *, simulated: bool) -> None: ...
    def disconnect(self) -> None: ...

    # Capabilities -- empty / None when not offered.
    def sources(self) -> list[SourceChannel]: ...          # existing Protocol
    def meters(self) -> list[MeterChannel]: ...            # existing Protocol
    def router(self) -> "Router | None": ...               # the 7709 routing capability
    def environment(self) -> "EnvironmentReader | None": ...  # observe-only; may be None for this step
```

- A `Registry` (or `InstrumentManager`) holds `list[LabInstrument]` and resolves a
  channel binding `(instrument_id, port)` to a concrete `SourceChannel` / `MeterChannel`.
- Concrete instruments for this step:
  - `M81Instrument` -> `sources()` / `meters()` (its slots); `router()` /
    `environment()` = `None`.
  - `Keithley7709` -> `router()` only.
  - The gate SMU and the environment reader are **designed for** but not implemented here
    (see Non-goals). Leave a clean seam.

## Session schema extension

Add an `instruments` block and channel `bind`s. Bump the schema version and keep loading
older files.

```yaml
schema_version: 3            # bumped; loader MUST still accept v2 (no `instruments`)
instruments:
  - id: m81_main
    type: lakeshore_m81
    connection: { resource: "GPIB0::12::INSTR", simulated: false }
  - id: matrix
    type: keithley_7709
    connection: { resource: "GPIB0::16::INSTR", simulated: false }
  # gate SMU added in spec 02; shown here only to fix the shape:
  # - id: gate_smu
  #   type: keysight_b2902b
  #   connection: { resource: "TCPIP0::192.168.0.5::INSTR", simulated: false }
channels:
  sources:
    - id: exc
      role: excitation
      bind: { instrument: m81_main, port: S1 }
    - id: gate
      role: gate
      bind: { instrument: m81_main, port: S3 }   # or { instrument: gate_smu, port: ch1 }
  meters:
    - id: vmeas
      role: voltage
      bind: { instrument: m81_main, port: M1 }
```

Backward compatibility: when loading a `schema_version: 2` file (no `instruments`),
synthesize a default registry containing the M81 (and the 7709 if it was referenced),
reproducing today's behavior so existing sessions keep working.

The version numbers above are illustrative. Use ELECMEAS's actual current schema version
and the next increment, established during reconnaissance.

## Migration (ordered)

1. Reconnaissance (above).
2. Add the `LabInstrument` Protocol and a `Registry` / `InstrumentManager` holding
   `list[LabInstrument]`.
3. Wrap the existing M81 facade as one `LabInstrument` implementation — no behavior
   change.
4. Expose the 7709 routing as a `router()` capability.
5. Extend the session schema with `instruments` + channel `bind`; bump to the next schema
   version; add default-synthesis on load for files written by the previous version.
6. Redirect `_build_channels` to assemble channels from the registry (resolve each
   binding to a concrete Protocol) instead of hard-coding the M81.
7. (Optional this step) GUI: a per-channel instrument selector. If it bloats the change,
   defer the GUI to the executor-tree work and keep this step headless + file-driven.

## Non-goals (this step)

- The executor tree / sequencer (roadmap step 4).
- The external SMU **driver implementation** — design the seam and reference
  `vdp-measure`'s B2902B SCPI/socket transport; full implementation is spec 02.
- Environment **control** (stays observe-only) and even the environment **reader**
  implementation — `environment()` may return `None` or a stub here.

## Acceptance criteria

- Existing Hall and van der Pauw presets run unchanged, in mock mode and against the M81,
  producing equivalent output to before the change.
- `schema_version: 2` session files load without error (synthesized default registry).
- A second instrument can be declared in a session and resolved by the registry, even if
  its driver is a stub.
- Mock mode works end-to-end.
- Tests cover: v3 schema round-trip; v2 -> v3 load; registry resolution of a binding;
  M81-as-registry-entry parity with the previous direct path.

## Open questions (confirm against source)

- Exact method names / signatures on the existing `SourceChannel` / `MeterChannel`
  Protocols.
- How `core/session.py` versions and migrates — is there an existing upgrade hook to
  extend, or is this the first version bump?
- The simplest seam in `gui/main_window.py` to redirect channel assembly to the registry.
- Whether 7709 routing already sits behind an interface that `router()` can wrap, or
  needs one introduced.
