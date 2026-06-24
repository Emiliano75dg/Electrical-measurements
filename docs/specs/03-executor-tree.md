# Spec 03 — Executor tree (the sequencer)

Roadmap step 4. Goal: make the acquisition orchestration **composable**. Today
`AcquisitionWorker` runs one hardcoded shape — `Loop(forever) → Sequence[RouteStep…] →
Step` — with the loops written as a literal `while` / `for`. This spec turns that fixed
shape into a tree the worker *walks*, so the user can compose sequences (repeat
indefinitely, sweep a gate, multi-block runs) without touching the engine.

Two design decisions are settled and frame everything below:

- **The orchestration is composed, not hardcoded.** The per-step acquisition is extracted
  into a reusable executor; `Loop` / `Sequence` / `Step` become executor objects; the
  QThread becomes a thin *runner shell* that walks the tree.
- **The engine always walks a tree.** Even a simple Hall run goes through the new
  machinery — the simple case *is* the synthesized default tree. One orchestration path,
  no parallel `while` / `for`. The existing Hall / vdP parity tests are the regression
  guardrail.

> **Written against the actual code on `main`** (reconnaissance of `core/channels.py`,
> `core/session.py`, `measurements/routing.py`, `measurements/engine.py` — 2026-06-23),
> **not from a README.** Signatures below are the real ones; still verify they have not
> drifted before implementing, and follow the code where it differs.

This spec has **two increments**, implemented as **two separate commits**:

- **Increment 1 — the tree skeleton.** Specs + executors + `build_executor` + the runner
  shell + default-tree synthesis + parity. No gate, no sweep.
- **Increment 2 — gate, role, sweep, routed-only interlock.** The `role` / `routing`
  fields on the source, the routed-only interlock, the sweep loop.

Increment 2 has no meaning without increment 1: do increment 1, prove the parity tests
green, then increment 2.

## Current state (from reconnaissance)

`AcquisitionWorker(QThread)` (`measurements/engine.py`) already runs an executor tree —
just hardcoded into one shape. `run()` does: `configure(sources)` → `configure(meters)` →
`enable(all sources)` → settle → `while running:` → `for step in steps:` →
`_route(step)` → `_acquire_readings()` (±I antisymmetrisation via `_apply_source_sign`,
which filters `func.is_current`) → `_build_row()` → write CSV + emit `sample_ready` →
(per cycle) emit the cross-step row → `finally:` `disable(all)` + `open_all`.

Mapping to the tree: `while running` is a `Loop(forever)`; `for step in steps` is a
`Sequence` (with the cross-step derived emitted per cycle); the per-step body is a
`Step`; `configure` / `enable` / `settle` / `finally: disable` are the infrastructural
shell (QThread, signals, CSV, the lock under which `update_source_configs` /
`update_meter_configs` reconfigure channels).

The Session (`core/session.py`, `SCHEMA_VERSION = 2`) holds the orchestration as **flat
fields**: `sources`, `meters`, `matrix` (`MatrixSettings`), `layout` (`MatrixLayout`),
`routes` (`list[RouteStep]`), `settle_s`, `interval_s`, `current_reversal`, `geometry`,
`derived_mode`, and the cross-step derived (driven today by `matrix.vdp_sheet` /
`derived_mode`). There is no representation of *order beyond the flat route list* and no
*looping/sweeping* structure.

Two gaps the tree must close, both already visible in the code:
- `_route()` carries the step-3 safety interlock and an explicit guard comment (it
  disables **every** source before switching relays, marked `REVISIT at the executor-tree
  (step 4)` for the non-routed gate — the "gate yo-yo"). Increment 2 resolves it.
- A source is **completely decoupled from matrix terminals**: `SourceConfig` knows only
  `func` / `amplitude` / `frequency_Hz` / `compliance`; `RouteStep.links` ties terminals
  to *sample contacts*, never to sources; in a Session, `sources` and `routes` are
  parallel lists that never reference each other. So "routed vs direct" cannot be
  *derived* — it must be made explicit on the source. Increment 2 does this minimally.

---

# Increment 1 — the tree skeleton

## Target design

Two parallel hierarchies, exactly as for instruments (`SourceSpec` → the channel
Protocols, with `build_instrument` between them):

- **Serializable specs** (pure dataclasses, live in the Session): `StepSpec`,
  `SequenceSpec`, `LoopSpec`. A recursive tree with a `type` discriminant.
- **Executors** (runtime objects): `StepExecutor`, `SequenceExecutor`, `LoopExecutor`,
  sharing one interface `run(context)` that emits rows. Built from the spec tree by
  `build_executor(spec, context)` — the orchestration counterpart of `build_instrument`.

```python
# Illustrative -- reconcile field names against the real Session/RouteStep.

@dataclass
class StepSpec:
    route: str | None = None          # references a RouteStep by label; None -> static route
    current_reversal: bool = False    # per-step (was a global worker kwarg)
    settle_s: float | None = None     # per-step override; None -> context default
    strategy: str = "point"           # "point" (today) | "stream" (later increment)

@dataclass
class SequenceSpec:
    children: list["NodeSpec"]        # run in order
    cross_derived: bool = False       # emit cross-step derived (vdP R_sheet, reciprocity) per cycle

@dataclass
class LoopSpec:
    child: "NodeSpec"                 # the body (usually a Sequence)
    kind: str = "forever"             # "forever" | "count" | "sweep"  (sweep -> increment 2)
    count: int | None = None          # for kind == "count"
    interval_s: float = 0.0           # inter-iteration wait (was a global worker kwarg)

NodeSpec = StepSpec | SequenceSpec | LoopSpec
```

Knobs move to where they belong: `current_reversal` / `settle_s` are per-`Step`;
the cross-step derived is per-`Sequence`; `interval_s` and the forever/count/sweep are
per-`Loop`. A `StepSpec` *references* a `RouteStep` by label — the routing content stays
in `matrix` / `routes`, it does not move into the tree.

### Executors and the runner shell

- `StepExecutor` wraps the **lifted** per-step logic (`_route` + `_acquire_readings` +
  `_build_row`), barely changed. `SequenceExecutor` runs its children in order and emits
  the cross-step row. `LoopExecutor` repeats its child (forever / N / → sweep in
  increment 2), waiting `interval_s` between iterations and checking the running flag.
- The **runner shell** is the QThread, reduced to: connect/teardown, the `sample_ready` /
  `status_changed` / `error_occurred` signals, CSV writing, and **the lock**. It assembles
  a `context` (resolved channels, matrix, the emit/write callbacks, the lock, the
  `DerivedContext` bits, the environment reader — a `None` stub for now) and calls
  `tree.run(context)`. The old `while running` becomes the root `LoopExecutor(forever)`.
- **Live config update is re-homed, not lost.** The shell keeps owning
  `update_source_configs` / `update_meter_configs` and the lock; the `StepExecutor`
  configures and reads channels **under that same lock**, reading the current configs. The
  guarantee from before — live edits apply under the same lock as the reads — must hold.

### Default-tree synthesis (the parity mechanism)

`Session.sequence` is a new **optional** field (`NodeSpec | None`, default `None`). When
absent — every v2 file and every simple GUI setup — the runner **synthesizes** the
default tree from the flat fields:

```
Loop(forever, interval_s) → Sequence(children=[Step per RouteStep], cross_derived)
```

with `current_reversal` / `settle_s` distributed onto the Steps. Behaviour is
**byte-identical** to today; the 97 existing tests stay green. When `sequence` is present
(advanced setups), it drives directly. This is the same pattern as step 1's
`instrument_id`: keep the tested flat structure, add a richer optional layer, synthesize
the default when it is absent.

Geometry presets emit a `SequenceSpec` of `StepSpec`, **not** a flat list — the preset
produces a sub-tree (invariant 2: geometries are presets, not engine branches).

### Schema: additive, no version bump

Unlike step 1, **no `SCHEMA_VERSION` bump**. `sequence` defaults to `None` and the tree is
synthesized at **runtime** (in `_make_worker` / the runner), not transformed at **load**
time — so `from_dict` needs no migration hook, and a v2 file loads with `sequence=None`
and behaves exactly as before. This follows the codebase's own additive precedent (the
matrix layout / routes were added without a bump; older files simply lack the keys and
load with defaults). A bump is only warranted when old data must be *transformed* on load,
which is not the case here.

### Named migration debt — `interval_s` is plumbed per-step (increment 1)

`LoopSpec.interval_s` is described above as an *inter-iteration* (per-cycle) wait. The
**current code does not pace that way**: `AcquisitionWorker.run()` pads **each step** to
`interval_s` (it captures `loop_start` before the route and sleeps the remainder after the
row is published — the `for step in steps` body), so a multi-step cycle today takes
≈ `n_steps × interval_s`, not one wait per cycle. For a single-step or static run the two
are identical; only multi-step (vdP) differs.

To keep increment 1 **byte-identical** (and, in the worst case, to keep the *readings*
unchanged — a different dwell can shift a lock-in's integration), the pacing stays
**per-step** in this increment: `LoopExecutor.run` threads its `interval_s` onto the
`RunContext`, and `StepExecutor` consumes it to pad each step exactly as the old loop did.
`LoopSpec.interval_s` is serialized as a per-loop field but is *applied* per-step.

**Planned migration:** make `interval_s` a genuine per-iteration wait owned by
`LoopExecutor` (and add a per-`StepSpec` dwell if a per-step pad is still wanted), once the
GUI sequence builder exists to author it and the parity baseline no longer pins the
per-step semantics. This is a named, forward-compatible debt — the same shape as the
`routing` stored→derived debt in increment 2 — recorded here, not only in the commit
message. Until then, treat per-loop `interval_s` semantics as not-yet-implemented.

### Serialization example

```json
"sequence": {
  "type": "loop", "kind": "forever", "interval_s": 0.0,
  "child": {
    "type": "sequence", "cross_derived": false,
    "children": [
      { "type": "step", "route": "Rxx", "current_reversal": true, "settle_s": 0.5 },
      { "type": "step", "route": "Rxy", "current_reversal": true }
    ]
  }
}
```

## Migration (ordered)

1. Add `StepSpec` / `SequenceSpec` / `LoopSpec` and the optional `Session.sequence` field
   (serialise/deserialise the recursive tree by `type` discriminant).
2. Add `StepExecutor` / `SequenceExecutor` / `LoopExecutor` with a shared `run(context)`,
   and `build_executor(spec, context)`.
3. Lift the per-step logic (`_route` + `_acquire_readings` + `_build_row`) into
   `StepExecutor` with minimal change.
4. Reduce `AcquisitionWorker` to the runner shell: assemble `context`, build the tree,
   walk it; re-home live config update under the existing lock.
5. Add default-tree synthesis from the flat fields when `Session.sequence is None`.
6. Make geometry presets emit a `SequenceSpec`.

## Acceptance criteria (increment 1)

- Hall and van der Pauw, run through the **synthesized** tree, produce output identical to
  before — the 97 existing tests stay green, and a test asserts the synthesized tree
  equals the previous flat-field behaviour.
- A v2 file (no `sequence`) loads and runs with byte-identical behaviour.
- An explicit `sequence` round-trips (write → read) and runs.
- Live config update still applies under the shell's lock during a run.
- Mock path works end-to-end.
- Tests: tree round-trip; default-synthesis parity; `build_executor` for each node type;
  a multi-step `Sequence` walked in order; a `forever` loop that runs and stops; a `count`
  loop.

---

# Increment 2 — gate, role, sweep, routed-only interlock

## Target design

Two fields onto `SourceSpec` (the structural metadata sits with `instrument_id`, not in
the electrical `SourceConfig`):

```python
@dataclass
class SourceSpec:
    port: int
    config: SourceConfig
    instrument_id: str | None = None        # existing (step 1)
    role: str | None = None                 # NEW -- for the sweep ("gate", "excitation", ...)
    routing: TermMode = TermMode.ROUTED     # NEW -- reuse the existing enum; default = today
```

`TermMode` (`ROUTED` / `FIXED`) already exists in `measurements/routing.py` — this is a
lift, not an invention.

### Routed-only interlock (the guard comment becomes code)

The engine stays instrument-agnostic. As `_resolve_meter_source` is distilled into a
derived map (`DerivedContext.meter_source`) and handed to the engine, the routed-ness is
distilled at build time into a set and handed to the engine:

```python
# in _make_worker, from the SourceSpecs:
fixed_source_ids = {s.id for s in source_specs if s.routing is TermMode.FIXED}

# in _route(), the minimal change:
for s in self._sources:
    if s.id not in self._fixed_source_ids:   # today: for ALL sources, no guard
        s.disable()
```

The engine learns "do not cycle these ids" — purely mechanical, no concept of gate, role
or terminal. Default `routing = ROUTED` → `fixed_source_ids` empty → disables all →
**identical to step 3** (97 green). A `FIXED` gate stays enabled across the Sequence's
steps — no yo-yo.

### Sweep by role

```python
# LoopSpec(kind="sweep", axis="gate", values=[-40, ..., +40])
gate = resolve_source_by_role(sources, spec.axis)     # validated: 0 or >1 match is an error
# each iteration: gate.configure(replace(gate.config, amplitude=v))
```

Naming the axis **by role** (not by raw id) means the sequence speaks of *function*
("sweep the gate"), not wiring ("sweep channel 3 of gate_smu") — change which instrument
is the gate and the sequence is untouched. `role` is a free string, resolved tolerantly
like `MeterConfig.reference`, with the 0/>1-match check added to
`validate_configuration`.

### Serialization example

```json
"sources": [
  { "port": 1, "role": "excitation", "config": { "func": "I_AC", "amplitude": 1e-05 } },
  { "port": 1, "role": "gate", "routing": "FIXED",
    "instrument_id": "gate_smu", "config": { "func": "V_DC" } }
],
"sequence": {
  "type": "loop", "kind": "sweep", "axis": "gate", "values": [-40, 0, 40],
  "child": {
    "type": "loop", "kind": "forever",
    "child": { "type": "sequence", "children": [
      { "type": "step", "route": "Rxx" }, { "type": "step", "route": "Rxy" }
    ] }
  }
}
```

## Named migration debt (read before implementing)

`routing` on `SourceSpec` is a **minimal proxy**. `TermMode` is conceptually a property
of the *terminal* (it was defined for `TerminalBinding`); putting it on the source is the
smallest representation that the interlock can consume without the source→terminal link.

**Planned migration:** when the wiring panel lands (a dedicated follow-up — see below) and
populates `TerminalBinding`s with their `TermMode`, the source's `routing` becomes
**redundant** with the terminal's mode. At that point `routing` migrates from a *stored*
field to a *derived* value (source → terminal → `TerminalBinding.mode`). This is a named,
forward-compatible debt — the down-payment on the panel, not a hidden shortcut. Do not
build the full source→terminal topology now: its only consumer is the panel, which does
not exist yet.

## Acceptance criteria (increment 2)

- `role` / `routing` round-trip; absent → `role=None`, `routing=ROUTED` (identical to
  today).
- The interlock disables only `ROUTED` sources; a test with a `FIXED` source asserts it is
  **not** disabled in `_route` across steps (no yo-yo).
- With all sources `ROUTED` (default), interlock behaviour is identical to step 3 (97
  green).
- `LoopSpec(kind="sweep", axis=<role>)` resolves the source by role and sweeps its
  amplitude; `validate_configuration` rejects 0 or >1 role matches.
- The stored→derived migration is documented in the spec and the commit.

---

## Bridge notes (not increments)

- **PointStep vs StreamStep.** `StepSpec.strategy` carries the execution strategy. Only
  `"point"` (the lifted acquisition) is implemented in step 4. `"stream"` (the M81
  lock-in hardware trace buffer, single channel, high cadence — mirrors
  `M81_electr_meas`'s stream-observe) is a later increment. The B2902B is point-only.
- **Wiring panel.** A graphical panel where the user lays out the physical
  source→terminal→contact wiring is a dedicated follow-up. It is the authoring UI for the
  source→terminal link that today "lives only in the head of whoever wires the cables."
  Increment 2's `routing` field is its forward-compatible down-payment; the panel later
  makes `routing` a derived view (see the migration debt above).

## Non-goals (this step)

- The `"stream"` step strategy / hardware trace (later increment).
- The wiring panel and the full source→terminal→contact topology (dedicated follow-up).
- Environment **control** — the environment reader stays observe-only and is a `None`
  stub in the context; wiring real T/B stamping per row is the step-5 port from
  `M81_electr_meas`.
- A per-channel GUI instrument selector and a GUI sequence builder (GUI work matures with,
  but after, this step).

## Open questions (confirm against source)

- Exact `Session` field names and the `to_dict` / `from_dict` structure (we have them from
  reconnaissance — verify they have not drifted).
- Where `_make_worker` builds the worker, and the cleanest seam to insert tree synthesis +
  `build_executor` + context assembly.
- The exact lock object in `AcquisitionWorker`, to re-home live config update onto the
  shell without changing its guarantee.
- The import path / current usage of `TermMode` and `RouteStep.label` to reference routes
  from a `StepSpec`.
