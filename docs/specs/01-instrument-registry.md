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
`None` / `[]` when a capability is not offered.

> **Reconciled against `core/channels.py` (2026-06-21).** ELECMEAS composes channels
> *dynamically* from the GUI tabs, each with its own per-channel `SourceConfig` /
> `MeterConfig`. A static `sources() -> list[SourceChannel]` enumeration therefore does
> not fit: the instrument cannot know the user's ports/configs in advance. Instead the
> capability is a **factory** — `make_source(port, cfg)` / `make_meter(port, cfg, id)` —
> that builds the concrete Protocol on demand from a binding. The registry resolves a
> channel's `instrument_id` to its `LabInstrument`, then asks that instrument to make the
> channel. This keeps the assembly layer instrument-agnostic without forcing the engine's
> generic model into a fixed channel list.

```python
class LabInstrument(Protocol):
    id: str

    def connect(self, *, simulated: bool) -> None: ...
    def disconnect(self) -> None: ...

    # Capability factories -- return None / [] when not offered.
    def make_source(self, port: int, cfg: SourceConfig) -> "SourceChannel | None": ...
    def make_meter(self, port: int, cfg: MeterConfig, meter_id: str) -> "MeterChannel | None": ...
    def router(self) -> "Router | None": ...                  # the 7709 routing capability
    def environment(self) -> "EnvironmentReader | None": ...   # observe-only; None this step
```

- A `Registry` (or `InstrumentManager`) holds `list[LabInstrument]` and resolves a
  channel binding `(instrument_id, port)` to a concrete `SourceChannel` / `MeterChannel`
  via the owning instrument's factory.
- Concrete instruments for this step:
  - `M81Instrument` -> `make_source()` / `make_meter()` (its slots); `router()` /
    `environment()` = `None`.
  - `Keithley7709` -> `router()` only.
  - The gate SMU and the environment reader are **designed for** but not implemented here
    (see Non-goals). Leave a clean seam.

> **`role` is deferred.** The target architecture tags each channel with a semantic
> `role` (`excitation`, `gate`, `voltage`, `leakage`, …). That tag is a *sequencer*
> concern, not a registry one: nothing in this step consumes it. It is therefore **not**
> introduced here — it lands as an optional channel field with the executor tree
> (roadmap step 4).

## Session schema extension

Add an `instruments` block and a per-channel `instrument_id`. The current schema is
**v1** (`core/session.py::SCHEMA_VERSION`); bump to **v2** and keep loading v1 files.

The existing channel shape — flat `SourceSpec(port, config)` / `MeterSpec(port,
meter_id, config)` — is preserved; each gains an **optional `instrument_id`** that names
the binding's instrument (absent ⇒ the synthesized default M81, i.e. today's behavior).

> **`SourceSpec`/`MeterSpec` vs `SourceConfig`/`MeterConfig` (real code names).** These are
> two distinct layers, kept distinct: `SourceSpec` / `MeterSpec` (in `core/session.py`)
> are the *serialisable channel records* — they hold the binding (`port`, `meter_id`,
> `instrument_id`) **and wrap** a `config`. That `config` is a `SourceConfig` /
> `MeterConfig` (in `core/channels.py`) — the instrument-agnostic settings dataclass. The
> registry factory takes the *config*, not the spec: `make_source(port, cfg)` /
> `make_meter(port, cfg, meter_id)` receive the `SourceConfig` / `MeterConfig` unwrapped
> from the spec. The spec is persistence; the config is the channel's runtime settings.

> **One source of truth for the default M81's connection.** A v2 file carries the M81's
> connection in *two* visible places — top-level `connection` and the M81's `instruments`
> entry. The rule for this step: **top-level `connection` is authoritative** for the
> default M81 (it is what the GUI's connection panel edits and what the running session
> connects with); the default M81's `instruments` entry is a *synthesized mirror* of it
> (`synthesize_default_instruments` derives the entry from `connection`), not an
> independent value. Any **non-default** instrument (a second SMU, …) is declared *only*
> in `instruments`, which is its sole source of truth. Fully registry-driven connection
> (making `instruments` authoritative and demoting top-level `connection` to a pure v1
> fallback) is a later refinement, once the GUI gains a per-instrument connection editor.

```json
{
  "schema_version": 2,
  "connection": { "ip_address": "192.168.0.1", "simulated": true },
  "instruments": [
    { "id": "m81_main", "type": "lakeshore_m81",
      "connection": { "resource": "192.168.0.1", "simulated": true } },
    { "id": "matrix", "type": "keithley_7709",
      "connection": { "resource": "TCPIP0::192.168.0.2::inst0::INSTR", "simulated": true } }
  ],
  "sources": [
    { "port": 1, "config": { "func": "I_AC", "amplitude": 1e-05 } },
    { "port": 3, "instrument_id": "m81_main", "config": { "func": "V_DC" } }
  ],
  "meters": [
    { "port": 1, "meter_id": "Vxx", "config": { "lockin": true } }
  ]
}
```

The gate SMU (spec 02) would appear as another `instruments` entry
(`{"id": "gate_smu", "type": "keysight_b2902b", ...}`) with a source whose
`instrument_id` points at it — no schema change needed beyond this step.

Backward compatibility: when loading a `schema_version: 1` file (no `instruments`),
synthesize a default registry containing the M81 (from `connection`) and the 7709 (if
`matrix.enabled`), reproducing today's behavior so existing sessions keep working.
Channels without `instrument_id` bind to the synthesized M81.

## Migration (ordered)

1. Reconnaissance (above).
2. Add the `LabInstrument` Protocol and a `Registry` / `InstrumentManager` holding
   `list[LabInstrument]`.
3. Wrap the existing M81 facade as one `LabInstrument` implementation — no behavior
   change.
4. Expose the 7709 routing as a `router()` capability.
5. Extend the session schema with an `instruments` block + an optional per-channel
   `instrument_id`; bump `SCHEMA_VERSION` 1 → 2; add default-synthesis on load for v1
   files.
6. Redirect `_build_channels` to assemble channels from the registry (resolve each
   `(instrument_id, port)` binding to a concrete Protocol via the owning instrument's
   factory) instead of hard-coding the M81.
7. **Deferred this step:** the GUI per-channel instrument selector. Multi-instrument
   configuration is **file-driven** here; the selector lands with the executor-tree work
   (step 4). The existing Channels tabs must not regress — with no `instrument_id` set
   they behave exactly as today (single synthesized M81).

## Non-goals (this step)

- The executor tree / sequencer (roadmap step 4).
- The external SMU **driver implementation** — design the seam and reference
  `vdp-measure`'s B2902B SCPI/socket transport; full implementation is spec 02.
- Environment **control** (stays observe-only) and even the environment **reader**
  implementation — `environment()` may return `None` or a stub here.

## Acceptance criteria

- Existing Hall and van der Pauw presets run unchanged, in mock mode and against the M81,
  producing equivalent output to before the change.
- v2 session files round-trip (save → load) preserving the `instruments` block and each
  channel's `instrument_id`.
- v1 files (no `instruments`) load via a **synthesized default registry**: an M81 from
  top-level `connection`, plus the 7709 if `matrix.enabled`. Channels without an
  `instrument_id` bind to the synthesized M81 — reproducing today's behavior.
- A second instrument can be declared in a session and resolved by the registry, even if
  its driver is a stub.
- Mock mode works end-to-end.
- Tests cover: v2 schema round-trip; v1 → v2 load (default synthesis); registry resolution
  of a binding; and M81-as-registry-entry parity with the previous direct path (a GUI
  parity test asserting the registry-built channels drive the same engine columns).

## Open questions — resolved during reconnaissance (2026-06-21)

- **`SourceChannel` / `MeterChannel` signatures:** confirmed in `core/channels.py` —
  `SourceChannel(id, config, configure/enable/disable)`,
  `MeterChannel(id, config, configure/read)`. The registry returns these unchanged.
- **Session versioning:** `SCHEMA_VERSION = 1`; `load_session` only *rejects* a newer
  schema and has no migration hook. This is the **first** real version bump and the first
  default-synthesis-on-load (the matrix block was previously added without a bump, relying
  on missing-key defaults).
- **GUI seam:** `gui/main_window.py::_build_channels` builds `M81Source` / `M81Meter` /
  `M81SMUMeter` directly from the tabs — the single point to redirect through the registry.
- **7709 interface:** `instruments/matrix7709.py::Matrix7709` exposes `connect`,
  `open_all`, `close`, `settle_s`; it is already passed to the worker via `self._matrix`.
  A minimal `Router` Protocol wraps it so `router()` has a typed return.
