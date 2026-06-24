"""Generic acquisition engine.

A single QThread worker that drives an arbitrary set of typed source and meter
channels (core.channels Protocols) — no knowledge of "Hall".  It configures all
channels, enables sources, then loops: read every meter, flatten the readings
into dynamic columns, append any DerivedQuantity columns, emit the row and write
it to CSV in real time.

Phase 2 scope: a single static route (no matrix).  The loop is written so that
matrix RouteStep iteration (Phase 4) slots in without reshaping it.

Column layout (dynamic, depends on the active channels)
───────────────────────────────────────────────────────
  time_s
  <meter.id>_X, <meter.id>_Y    for each lock-in meter
  <meter.id>_DC                 for each DC meter
  <derived.name>                for each DerivedQuantity
"""

from __future__ import annotations

import csv
import threading
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.channels import MeterConfig, MeterChannel, Reading, SourceChannel, SourceConfig
from core.derived import CrossStepQuantity, DerivedContext, DerivedQuantity, Geometry
from measurements.executors import RunContext, build_executor
from measurements.routing import MatrixLayout, RouteStep
from measurements.sequence import NodeSpec, synthesize_default_sequence

# step label used for the per-cycle combined row (cross-step quantities)
COMBINED_LABEL = "combined"


class AcquisitionWorker(QThread):
    """Generic acquisition thread for an arbitrary source/meter set.

    Without a matrix it runs a single static route (Phase 2 behaviour).  Given a
    matrix + layout + steps it iterates the RouteStep list each cycle, opening
    and closing relays between steps and tagging every row with its step label
    (Phase 4 — van der Pauw, contact rotation).
    """

    sample_ready   = Signal(dict)
    status_changed = Signal(str)
    error_occurred = Signal(str)

    def __init__(
        self,
        sources: list[SourceChannel],
        meters: list[MeterChannel],
        save_path: Path,
        *,
        derived: list[DerivedQuantity] | None = None,
        geometry: Geometry | None = None,
        settle_s: float = 1.0,
        interval_s: float = 0.5,
        current_reversal: bool = False,
        matrix: object | None = None,
        layout: MatrixLayout | None = None,
        steps: list[RouteStep] | None = None,
        cross_derived: list[CrossStepQuantity] | None = None,
        sequence: NodeSpec | None = None,
        fixed_source_ids: set[str] | None = None,
        source_roles: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._sources = sources
        self._meters = meters
        self.save_path = save_path
        self._derived = derived or []
        self._cross = cross_derived or []
        self._geometry = geometry or Geometry()
        self._settle_s = settle_s
        self._interval_s = interval_s
        self._current_reversal = current_reversal
        self._matrix = matrix
        self._layout = layout
        self._steps = steps                    # None → static single route (no "step" column)
        # Orchestration tree.  None → synthesized from the flat fields at run()
        # time (byte-identical to the previous while/for); when present it drives
        # directly (spec 03, "Default-tree synthesis").
        self._sequence = sequence
        # Routed-only interlock (spec 03, increment 2): channel ids of sources NOT
        # routed through the matrix (FIXED).  _route never cycles these — neither
        # disables nor re-enables them (no yo-yo).  Empty (the default) → every
        # source is cycled → identical to step 3.  The engine stays agnostic: it
        # holds opaque ids, not a notion of gate/role/terminal.
        self._fixed_source_ids = fixed_source_ids or set()
        # Sweep resolution: channel id → role string, used to set the swept
        # source's amplitude by role at run time.
        self._source_roles = source_roles or {}
        self._running = False
        self._t0: float | None = None
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._running = False

    # ── column / row construction ───────────────────────────────────────────────

    def columns(self) -> list[str]:
        cols = ["time_s"]
        if self._steps is not None:
            cols.append("step")
        for m in self._meters:
            if m.config.lockin:
                cols += [f"{m.id}_X", f"{m.id}_Y"]
            else:
                cols += [f"{m.id}_DC"]
        cols += [q.name for q in self._derived]
        cols += [q.name for q in self._cross]
        return cols

    def _flatten(self, readings: dict[str, Reading]) -> dict[str, float]:
        row: dict[str, float] = {}
        for m in self._meters:
            rd = readings[m.id]
            if m.config.lockin:
                row[f"{m.id}_X"] = rd.x
                row[f"{m.id}_Y"] = rd.y
            else:
                row[f"{m.id}_DC"] = rd.dc
        return row

    def _context(self) -> DerivedContext:
        source_amplitudes = {s.id: s.config.amplitude for s in self._sources}
        default_amp = self._sources[0].config.amplitude if self._sources else 1.0
        meter_source: dict[str, str] = {}
        for m in self._meters:
            sid = self._resolve_meter_source(m)
            if sid is not None:
                meter_source[m.id] = sid
        return DerivedContext(
            source_amplitudes=source_amplitudes,
            meter_source=meter_source,
            geometry=self._geometry,
            meter_is_lockin={m.id: m.config.lockin for m in self._meters},
            default_amplitude=default_amp,
        )

    def _resolve_meter_source(self, meter: MeterChannel) -> str | None:
        """Which source normalises this meter's resistance.

        Lock-in meters are normalised by their reference source; any meter falls
        back to the single current source (the common single-excitation case),
        then to the reference, then to the first source.  Genuinely ambiguous
        setups (a DC meter with several current sources) are rejected upstream by
        validate_configuration().
        """
        src_ids = {s.id for s in self._sources}
        ref = meter.config.reference
        if meter.config.lockin and ref in src_ids:
            return ref
        current_ids = [s.id for s in self._sources if s.config.func.is_current]
        if len(current_ids) == 1:
            return current_ids[0]
        if ref in src_ids:
            return ref
        return self._sources[0].id if self._sources else None

    # ── live reconfiguration (from the main thread, same lock as reads) ──────────

    def update_meter_configs(self, cfg_by_id: dict[str, MeterConfig]) -> None:
        with self._lock:
            for m in self._meters:
                if m.id in cfg_by_id:
                    m.configure(cfg_by_id[m.id])

    def update_source_configs(self, cfg_by_id: dict[str, SourceConfig]) -> None:
        with self._lock:
            for s in self._sources:
                if s.id in cfg_by_id:
                    s.configure(cfg_by_id[s.id])
                    s.enable()

    # ── QThread entry ───────────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        self._t0 = time.monotonic()

        try:
            for s in self._sources:
                s.configure(s.config)
            for m in self._meters:
                m.configure(m.config)
            for s in self._sources:
                s.enable()
        except Exception as exc:
            self.error_occurred.emit(f"Setup error: {exc}")
            self.status_changed.emit("Stopped")
            return

        try:
            cols = self.columns()

            if self._settle_s > 0:
                self.status_changed.emit(f"Settling  ({self._settle_s:.1f} s)…")
                self.msleep(int(self._settle_s * 1000))
                if not self._running:
                    return

            self.status_changed.emit("Acquiring…")

            with open(self.save_path, "w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                writer.writeheader()

                def _publish(row: dict) -> None:
                    writer.writerow(row)
                    fh.flush()
                    self.sample_ready.emit(row)

                # Build (or synthesize) the orchestration tree and walk it.  The
                # old `while running` is now the root Loop(forever); a read error
                # propagates out of the tree and is surfaced here, exactly as the
                # per-step `except` did before.
                tree = self._sequence or synthesize_default_sequence(
                    self._steps,
                    settle_s=self._settle_s,
                    interval_s=self._interval_s,
                    current_reversal=self._current_reversal,
                    has_cross=bool(self._cross),
                )
                ctx = self._make_context(_publish)
                try:
                    build_executor(tree, ctx).run(ctx)
                except Exception as exc:
                    self.error_occurred.emit(f"Read error: {exc}")
                    self._running = False

        finally:
            for s in self._sources:
                try:
                    s.disable()
                except Exception:
                    pass
            if self._matrix is not None:
                try:
                    self._matrix.open_all()
                except Exception:
                    pass
            self.status_changed.emit("Stopped")

    # ── run context (the seam handed to the executors) ──────────────────────────

    def _make_context(self, publish) -> RunContext:
        """Bundle the shell's primitives + run-wide data for the executor tree.

        The acquisition primitives stay methods of this worker — the executors
        invoke them through the context, so the read still happens under the same
        lock as live reconfiguration (the guarantee is unchanged).
        """
        route_by_label = {s.label: s for s in (self._steps or [])}

        def _on_cross_error(exc: Exception) -> None:
            self.error_occurred.emit(f"Cross-step error: {exc}")
            self._running = False

        return RunContext(
            route=self._route,
            acquire=self._acquire_readings,
            build_row=self._build_row,
            build_cross_row=self._build_cross_row,
            publish=publish,
            sleep_ms=lambda ms: self.msleep(int(ms)),
            is_running=lambda: self._running,
            now=time.monotonic,
            resolve_route=route_by_label.get,
            cross=self._cross,
            has_step_column=self._steps is not None,
            default_settle_s=self._settle_s,
            interval_s=self._interval_s,
            on_cross_error=_on_cross_error,
            sweep_axis=self._set_sweep_axis,
        )

    def _set_sweep_axis(self, axis: str, value: float) -> None:
        """Set the amplitude of the source bound to ``axis`` (by role), under the lock.

        Resolved by role, not id, so the sequence speaks of function ("sweep the
        gate") and not wiring.  Reconfiguration happens under the same lock as the
        reads and live edits (the increment-1 guarantee).  ``validate_configuration``
        rejects 0 or >1 role matches before the run; this stays defensive.
        """
        matches = [s for s in self._sources if self._source_roles.get(s.id) == axis]
        if len(matches) != 1:
            raise ValueError(
                f"sweep axis '{axis}' resolves to {len(matches)} sources (need exactly one)"
            )
        src = matches[0]
        with self._lock:
            src.configure(replace(src.config, amplitude=value))
            src.enable()

    # ── matrix routing ────────────────────────────────────────────────────────────

    def _route(self, step: RouteStep) -> None:
        if self._matrix is None or self._layout is None:
            return
        channels = step.channels(self._layout)
        # Safety interlock (CLAUDE.md invariant 5): relays never move with sources
        # live.  Disable the routed sources *before* open_all, switch, then
        # re-enable once the new step's relays are closed.  Instrument-agnostic —
        # the engine only knows the SourceChannel Protocol, not what an SMU is, so
        # a failed disable propagates and aborts the run before any relay moves.
        #
        # Routed-only interlock (spec 03, increment 2): a source in
        # self._fixed_source_ids is wired *outside* the matrix (e.g. a gate that
        # does not pass through the 7709).  No relay moves on its path, so it is
        # left completely untouched here — neither disabled nor re-enabled — to
        # avoid changing the sample state between measurements (the gate yo-yo:
        # a re-enable could re-assert the setpoint just as a disable could zero it).
        # Empty set → every source is cycled → identical to step 3 (over-disabling
        # is always safe).
        for s in self._sources:
            if s.id not in self._fixed_source_ids:
                s.disable()
        self._matrix.open_all()
        self._matrix.close(channels)
        for s in self._sources:
            if s.id not in self._fixed_source_ids:
                s.enable()
        # Settle after re-enable: let the new path (relays + re-driven sources)
        # stabilise — same mechanism as the initial settle.
        settle = getattr(self._matrix, "settle_s", 0.0)
        if settle and settle > 0:
            self.msleep(int(settle * 1000))

    # ── single read ─────────────────────────────────────────────────────────────

    # ── reading acquisition (with optional DC current reversal) ───────────────────

    def _acquire_readings(self, current_reversal: bool | None = None) -> dict[str, Reading]:
        """Read every meter once, or as a +I/−I antisymmetrised pair if reversal is on.

        Current reversal (REDESIGN.md §5, §8) rejects current-independent offsets
        (thermal EMF, relay/junction series voltages): measure at +I and −I, then
        keep the odd part (V+ − V−)/2.  It is applied per route step, under the
        same lock as live reconfiguration.

        ``current_reversal`` is now per-step (carried by ``StepSpec``); ``None``
        falls back to the worker-wide default so ``read_single`` is unchanged.
        """
        reversal = self._current_reversal if current_reversal is None else current_reversal
        if not reversal:
            with self._lock:
                return {m.id: m.read() for m in self._meters}

        with self._lock:
            plus = {m.id: m.read() for m in self._meters}
            self._apply_source_sign(-1.0)
            delay = self._reversal_settle_s()
            if delay > 0:
                self.msleep(int(delay * 1000))
            minus = {m.id: m.read() for m in self._meters}
            self._apply_source_sign(+1.0)

        out: dict[str, Reading] = {}
        for m in self._meters:
            p, n = plus[m.id], minus[m.id]
            out[m.id] = Reading(
                x=(p.x - n.x) / 2.0,
                y=(p.y - n.y) / 2.0,
                dc=(p.dc - n.dc) / 2.0,
                unit=p.unit,
            )
        return out

    def _apply_source_sign(self, sign: float) -> None:
        """Flip only the current sources to sign·|amplitude| (keeps |amplitude|).

        Current reversal reverses the *current* direction, so voltage sources are
        left untouched — flipping a bias voltage is a different operation and would
        corrupt the antisymmetrisation.  Sources that are not reversed stay enabled
        from run()'s setup.
        """
        for s in self._sources:
            if not s.config.func.is_current:
                continue
            cfg = s.config
            s.configure(replace(cfg, amplitude=sign * abs(cfg.amplitude)))
            s.enable()

    def _reversal_settle_s(self) -> float:
        """Dwell between +I and −I so the meters re-integrate the flipped excitation."""
        delays = [
            m.config.time_constant_s * 5.0 if m.config.lockin else max(0.02, m.config.nplc / 50.0)
            for m in self._meters
        ]
        return max(delays) if delays else 0.02

    def _build_row(self, readings: dict[str, Reading], step_label: str | None = None) -> dict:
        t = time.monotonic() - self._t0
        row: dict = {"time_s": round(t, 4)}
        if step_label is not None:
            row["step"] = step_label
        row.update(self._flatten(readings))
        ctx = self._context()
        for q in self._derived:
            row[q.name] = q(readings, ctx)
        return row

    def _build_cross_row(self, cycle: dict[str, dict[str, Reading]]) -> dict:
        t = time.monotonic() - self._t0
        row: dict = {"time_s": round(t, 4)}
        if self._steps is not None:
            row["step"] = COMBINED_LABEL
        ctx = self._context()
        for q in self._cross:
            row[q.name] = q(cycle, ctx)
        return row

    def _read_row(self, step_label: str | None = None) -> dict:
        return self._build_row(self._acquire_readings(), step_label)

    def read_single(self) -> dict:
        """One-shot synchronous read: configure, enable, read once. Caller disables."""
        for s in self._sources:
            s.configure(s.config)
        for m in self._meters:
            m.configure(m.config)
        for s in self._sources:
            s.enable()
        if self._t0 is None:
            self._t0 = time.monotonic()
        if self._steps and self._cross:
            cycle: dict[str, dict[str, Reading]] = {}
            for step in self._steps:
                self._route(step)
                cycle[step.label] = self._acquire_readings()
            return self._build_cross_row(cycle)
        if self._steps:
            step = self._steps[0]
            self._route(step)
            return self._read_row(step.label)
        return self._read_row()

    def disable_sources(self) -> None:
        for s in self._sources:
            try:
                s.disable()
            except Exception:
                pass
