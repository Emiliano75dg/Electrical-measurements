# CLAUDE.md

Project context for Claude Code. Read this first, every session.

## What this is

A configurable instrument-control application for electrical transport measurements
(Hall bar, van der Pauw, and beyond). At runtime the user composes: which instruments
are used (including ones not currently wired in, e.g. a source for a gate voltage),
whether the contact matrix is switched or static, the measurement geometry, and a
measurement sequence that can repeat indefinitely.

This repo **extends ELECMEAS** (the v2 declarative-engine redesign). It is **not** a
rewrite — the clean core already exists and we are widening it. Two predecessor repos
inform the design and contain reusable, already-tested domain logic:

- `M81_electr_meas` — the mature framework. Source of concepts and pure logic: the
  sequence model, the contact-map / measurement-sequence separation, environment
  control, safety interlocks, reciprocity as a first-class quantity.
- `vdp-measure` — the earliest, narrowest attempt: van der Pauw, a Keysight B2902B SMU
  driven over SCPI/socket, and data-quality checks. Source of the first external-SMU
  transport and the analysis discipline.

## Language & conventions

- **All repo content is in English**: code, identifiers, comments, docstrings,
  documentation, diagram labels, commit messages. No exceptions.
- Project discussion with the user happens in Italian, but nothing in Italian lands in
  the repo.
- Python with type hints and Protocol-based interfaces. Match the existing ELECMEAS
  style; do not introduce a competing one.

## Architecture invariants (do not violate)

1. **The engine is instrument-agnostic.** The acquisition engine consumes arbitrary
   lists of `SourceChannel` / `MeterChannel` (Protocols). It must never reference a
   concrete instrument. Adding an instrument = implementing the Protocol; the engine and
   GUI stay unchanged.
2. **Geometries are presets, not special cases.** Hall bar / van der Pauw are
   configurations on top of the generic engine — `(layout, steps)` plus derived
   quantities. Never add a `geometry == "hall"` branch into the engine.
3. **Routing is a capability.** The contact matrix (Keithley 7709) is optional;
   `matrix_policy` selects switched vs static. Every code path must work with the matrix
   absent.
4. **Environment is observe-only (for now).** Temperature/field are read and stamped onto
   each row; the app does not drive them — an external system does ("riding the ramp").
   The control interface is a deliberately-unimplemented extension point.
5. **Safety interlock ordering is mandatory.** Any relay switch follows: open all →
   disable sources → switch → re-enable. Enforce at the executor level so every path is
   safe.
6. **Session is versioned and backward-compatible.** Adding schema fields must not break
   loading of older session files; bump the version and provide defaults for missing
   blocks.
7. **The sequencer has no "mode" enum.** Measurement modes are *shapes* of a composable
   executor tree (`Loop` / `Sequence` / `Step`, with point vs stream step strategies),
   not branches in code.

## Roadmap (build order)

1. **Instrument registry** — current task. Spec: `docs/specs/01-instrument-registry.md`.
2. Session schema extension for multi-instrument (folded into step 1).
3. External SMU adapter (gate drive + leakage read), reusing `vdp-measure`'s B2902B SCPI
   transport. Spec: `docs/specs/02-external-smu-adapter.md`.
4. Executor tree (the sequencer): `Loop` / `Sequence` / `Step`, `PointStep` /
   `StreamStep`.
5. Port from `M81_electr_meas`: environment reader, reciprocity, data-quality checks.

Specs `01` and `02` are written; the rest follow. Begin at the reconnaissance step of
spec `01`.

## How to work here

- **Read the actual source before changing it.** The design docs were written from the
  READMEs and `ARCHITECTURE.md`, not from line-by-line source. Reconcile names and
  signatures against the code, and flag discrepancies rather than forcing the spec.
- Preserve **mock mode** — every change keeps the simulated path working.
- Preserve the existing **Hall / vdP presets** — they are the regression baseline.
- Keep changes scoped to the current roadmap item.
- **The predecessor repos must be reachable.** The specs lift tested code from
  `M81_electr_meas` and `vdp-measure`. If they are not available to you (local clones, git
  remotes, or vendored under this repo), resolve that first — it is a prerequisite, not an
  optional reference.
- Definition of done for each item: the change ships with tests and a working mock path.

See `docs/architecture.md` for the full target design.
