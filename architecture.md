# Architecture

The target design for the configurable measurement app. This is the north star;
`CLAUDE.md` holds the invariants, the `docs/specs/` files hold concrete work orders.

## Goals

The app must let the user, at runtime:

1. Configure instruments — including ones not currently wired in (e.g. add a source to
   apply a gate voltage). Instruments come from a registry; the engine never hard-codes
   them.
2. Choose whether the contact matrix is used (`switched`) or bypassed (`static`).
3. Choose a measurement geometry (Hall bar, van der Pauw, custom) as a preset, not a
   code branch.
4. Compose a measurement sequence that can repeat indefinitely.

## The four layers

Four orthogonal concerns. The three predecessor repos each solved a different subset;
the task is composition, not invention.

```
+-----------------------------------------------+   +--------------+
|  Orchestration -- the sequencer               |   |              |
|  Loop . Sequence . Step                       |   |              |
+-----------------------------------------------+   |   Session    |
|  Topology / routing                           |   |              |
|  contact map . matrix_policy                  |   |  JSON / YAML |
+-----------------------------------------------+   |              |
|  Channels / roles                             |   |  serializes  |
|  Source / Meter, bound by role                |   |  every layer |
+-----------------------------------------------+   |              |
|  Instruments (registry)                       |   |              |
|  M81 . gate SMU . 7709 . environment          |   |              |
+-----------------------------------------------+   +--------------+
   config flows down   .   readings flow up
```

Status against ELECMEAS: the two middle layers (channels, topology) largely exist —
the `SourceChannel` / `MeterChannel` Protocols, the geometry presets, the 7709 routing.
The top and bottom layers are the work: the registry below, the sequencer above.

### Layer 1 — Instruments (registry)

A session holds a *set* of instruments, each declared by type + connection. Each
instrument advertises the capabilities it offers and returns `None` for the rest:

- `sources()` / `meters()` — the channels it provides (existing Protocols).
- `router()` — a routing capability (the 7709), or `None`.
- `environment()` — an observe-only environment reader, or `None`.

The current M81 facade becomes *one* entry in this registry. The 7709 becomes a routing
capability. The gate is a *new* entry. This is the generalization that makes "add an
instrument" a registry operation rather than an engine change. See
`docs/specs/01-instrument-registry.md`.

### Layer 2 — Channels / roles

Abstract sources and meters, decoupled from which instrument provides them. A channel is
a `(instrument_id, port)` binding plus a semantic `role` tag (`excitation`, `gate`,
`voltage`, `leakage`, ...). Everything downstream refers to a channel **by role**, never
by instrument — this is what lets the same configuration work whether the gate lives on
an M81 slot or on a separate SMU.

### Layer 3 — Topology / routing

The contact matrix. Keep `M81_electr_meas`'s separation of the *contact map* (logical
contact -> physical relay/pin) from the *measurement* (what / order / settings), so an
ordering change does not require re-describing the wiring. `matrix_policy` selects
`switched` vs `static`. The geometry preset defines which logical contacts exist and
which current/voltage pairings are valid.

### Layer 4 — Orchestration (the sequencer)

The piece none of the predecessors had in full. Modelled as an **executor tree** whose
nodes share one interface (`run(context) -> emits rows`) so they nest freely:

- `Step` — configure -> settle -> optional +-I reversal -> read -> derive -> emit. Reads
  the environment and stamps T/B onto the row. Two execution strategies, same interface:
  - `PointStep` — point-by-point (the existing ELECMEAS engine path).
  - `StreamStep` — delegates to the instrument's hardware trace buffer for high cadence
    (single channel, AC/lock-in). Mirrors `M81_electr_meas`'s stream-observe.
- `Sequence` — runs child steps in order; emits cross-step derived quantities per cycle
  (van der Pauw R_sheet, reciprocity).
- `Loop` — either `repeat: n | forever`, or `sweep: <parameter> over <values>`, where the
  parameter is a *reference to a setpoint* (a source amplitude, the gate, frequency) or,
  in the future control mode, an environment setpoint. Riding a ramp uses
  `repeat: forever` with environment stamping; it is not a sweep.

```
Loop (repeat infinity)           ride the ramp, stamp T/B on every row
+- Sequence (in order)           cross-step derived emitted per cycle
   +- Step -- Hall bar           switch relays . set excitation . read
   +- Step -- van der Pauw       switch relays . solve R_sheet
```

The user composes this tree (in the GUI or in the Session file). The two measurement
"modes" we discussed are just two shapes of it:

- **Sweep the gate:** wrap everything in an outer `Loop` with `sweep: gate over
  -40..+40 V`. Same tree, one extra level.
- **High-cadence streaming:** replace the `Sequence` with a single `StreamStep` (no relay
  switching) -> maximum cadence for one quantity.

There is no `mode` flag. New modes fall out of the same grammar.

**Derived quantities** are first-class and attached to steps or sequences: an
antisymmetrized resistance from a +I / -I pair within a step, or a van der Pauw R_sheet
and a reciprocity error computed across the steps of a sequence. They are configuration,
evaluated by the runner — not code branched per geometry. This is what keeps geometries
as presets (invariant 2).

## Riding the ramp

Temperature and field are driven by an external system; the app rides along. Therefore
T/B are **not** swept axes — they are a data source sampled and stamped onto each row.
The "sweep over B" happens physically outside the app; the app keeps acquiring and tags
each point with the B it read at that instant. Consequence for persistence: a ride is
captured by appending for the full duration (every point matters); a rolling window is
for the live plot only, not for the saved data.

`M81_electr_meas` already does this (its stream-observe / async-poll modes, with a
read-only environment client and an `allow_control: false` flag). The
`EnvironmentReader` Protocol exposes read only; an `EnvironmentController` extension
(set/ramp) is defined but deliberately not implemented yet.

## The gate

The gate needs no dedicated machinery. It is a `Source` bound by `role: gate`, and a
sweep axis is simply a reference to a source setpoint. So "hold the gate at 0 V" is that
setpoint set once inside a `Step`, and "sweep the gate -40..+40 V" is a `Loop` whose axis
points at the same setpoint. One binding, two uses, no special case. The same role binds
whether the gate is an M81 slot or a separate SMU.

## Session

A single serializable object (JSON / YAML) holds the whole stack: instruments + channels
+ topology + the executor tree + output config. The GUI edits it, the file stores and
shares it, the runner executes it. This unifies ELECMEAS's versioned JSON session with
the YAML-config discipline of the predecessors. Schema is versioned; older files load
with synthesized defaults.

## Relationship to predecessor repos

- **ELECMEAS** — the skeleton: the generic engine, the Protocols, the PySide6 GUI, the
  versioned session. We widen it.
- **M81_electr_meas** — lift the *concepts and pure logic*: sequence model, contact-map /
  sequence split, safety interlocks, environment abstraction, reciprocity. Most of it is
  domain functions independent of the M81 plumbing.
- **vdp-measure** — lift the B2902B SCPI/socket transport (first external SMU) and the
  data-quality checks (V/I linearity, reciprocity error, anisotropy, contact stability).
