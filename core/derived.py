"""Derived quantities — geometry/formulas computed from raw channel readings.

The acquisition engine (measurements/engine.py) stays ignorant of "Hall": it
only flattens raw meter readings.  Any computed column (resistance, phase,
resistivity, …) is a DerivedQuantity supplied to the engine.  Presets such as
the Hall bar live here, built on the same generic pieces a user could combine
for an arbitrary setup (decision 2026-06-13: generic-arbitrary engine).

A DerivedQuantity is just a name plus a function (readings, ctx) → float, where
`readings` maps meter id → Reading and `ctx` carries the source normalisation
and sample geometry.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Callable

from core.channels import Reading


@dataclass
class Geometry:
    """Sample dimensions in SI metres. All three > 0 enables resistivity."""

    width_m: float = 0.0
    length_m: float = 0.0
    thickness_m: float = 0.0

    @property
    def valid(self) -> bool:
        return self.width_m > 0 and self.length_m > 0 and self.thickness_m > 0


@dataclass
class DerivedContext:
    """Normalisation context for derived quantities.

    A meter's resistance is signal / (amplitude of the source that drives it).
    With several sources this must be resolved per meter, otherwise the
    "arbitrary N sources" engine silently normalises everything by one source:

      source_amplitudes : source id  → amplitude  (A for I-source, V for V-source)
      meter_source      : meter id   → the source id that normalises that meter
      default_amplitude : fallback when a meter has no resolved source

    `amplitude_for(meter_id)` does the lookup, falling back to the single source
    when there is exactly one (so single-source presets need no mapping).
    """

    source_amplitudes: dict[str, float] = field(default_factory=dict)
    meter_source: dict[str, str] = field(default_factory=dict)
    geometry: Geometry = field(default_factory=Geometry)
    meter_is_lockin: dict[str, bool] = field(default_factory=dict)
    default_amplitude: float = 1.0

    def amplitude_for(self, meter_id: str) -> float:
        sid = self.meter_source.get(meter_id)
        if sid is not None and sid in self.source_amplitudes:
            return self.source_amplitudes[sid]
        if len(self.source_amplitudes) == 1:
            return next(iter(self.source_amplitudes.values()))
        return self.default_amplitude


DerivedFn = Callable[[Mapping[str, Reading], DerivedContext], float]


@dataclass
class DerivedQuantity:
    name: str
    fn: DerivedFn

    def __call__(self, readings: Mapping[str, Reading], ctx: DerivedContext) -> float:
        return self.fn(readings, ctx)


def _signal(readings: Mapping[str, Reading], ctx: DerivedContext, meter_id: str) -> float:
    """In-phase lock-in value (X) for AC meters, DC value otherwise."""
    rd = readings[meter_id]
    return rd.x if ctx.meter_is_lockin.get(meter_id, True) else rd.dc


# ── generic building blocks ─────────────────────────────────────────────────

def resistance(name: str, meter_id: str) -> DerivedQuantity:
    """signal / source_amplitude → Ω (current source) or dimensionless (voltage source)."""

    def fn(readings: Mapping[str, Reading], ctx: DerivedContext) -> float:
        amp = ctx.amplitude_for(meter_id)
        return _signal(readings, ctx, meter_id) / amp if amp != 0.0 else 0.0

    return DerivedQuantity(name, fn)


def phase(name: str, meter_id: str) -> DerivedQuantity:
    """Lock-in phase atan2(Y, X) in degrees."""

    def fn(readings: Mapping[str, Reading], ctx: DerivedContext) -> float:
        rd = readings[meter_id]
        return math.degrees(math.atan2(rd.y, rd.x))

    return DerivedQuantity(name, fn)


def resistivity_longitudinal(name: str, meter_id: str) -> DerivedQuantity:
    """ρxx = Rxx · (w · t) / L  [Ω·m].  Returns 0 if geometry incomplete."""

    def fn(readings: Mapping[str, Reading], ctx: DerivedContext) -> float:
        g = ctx.geometry
        if not g.valid:
            return 0.0
        amp = ctx.amplitude_for(meter_id)
        r = _signal(readings, ctx, meter_id) / amp if amp != 0.0 else 0.0
        return r * (g.width_m * g.thickness_m) / g.length_m

    return DerivedQuantity(name, fn)


def resistivity_transverse(name: str, meter_id: str) -> DerivedQuantity:
    """ρxy = Rxy · t  [Ω·m].  Returns 0 if geometry incomplete."""

    def fn(readings: Mapping[str, Reading], ctx: DerivedContext) -> float:
        g = ctx.geometry
        if not g.valid:
            return 0.0
        amp = ctx.amplitude_for(meter_id)
        r = _signal(readings, ctx, meter_id) / amp if amp != 0.0 else 0.0
        return r * g.thickness_m

    return DerivedQuantity(name, fn)


# ── presets ─────────────────────────────────────────────────────────────────

def hall_preset(
    vxx_id: str = "Vxx",
    vxy_id: str = "Vxy",
    geometry: Geometry | None = None,
) -> list[DerivedQuantity]:
    """Hall bar quantities: Rxx, Rxy, φxx, φxy (+ ρxx, ρxy if geometry valid)."""
    quantities = [
        resistance("Rxx", vxx_id),
        resistance("Rxy", vxy_id),
        phase("phi_xx_deg", vxx_id),
        phase("phi_xy_deg", vxy_id),
    ]
    if geometry is not None and geometry.valid:
        quantities += [
            resistivity_longitudinal("rho_xx", vxx_id),
            resistivity_transverse("rho_xy", vxy_id),
        ]
    return quantities


def per_meter_generic(
    meter_ids: list[str],
    lockin_ids: list[str] | None = None,
    *,
    with_resistance: bool = True,
    with_phase: bool = True,
) -> list[DerivedQuantity]:
    """R_<id> for every meter and φ_<id>_deg for each lock-in meter."""
    lockin_ids = lockin_ids if lockin_ids is not None else meter_ids
    quantities: list[DerivedQuantity] = []
    if with_resistance:
        quantities += [resistance(f"R_{mid}", mid) for mid in meter_ids]
    if with_phase:
        quantities += [phase(f"phi_{mid}_deg", mid) for mid in lockin_ids]
    return quantities


# ── cross-step quantities (combine readings from several route steps) ─────────

CrossStepFn = Callable[[Mapping[str, Mapping[str, Reading]], DerivedContext], float]


@dataclass
class CrossStepQuantity:
    """A value computed from a whole cycle: {step_label: {meter_id: Reading}}.

    Unlike DerivedQuantity (one step's readings) these combine several steps —
    e.g. van der Pauw sheet resistance from the two rotated configurations.
    """

    name: str
    fn: CrossStepFn

    def __call__(
        self, cycle: Mapping[str, Mapping[str, Reading]], ctx: DerivedContext
    ) -> float:
        return self.fn(cycle, ctx)


def solve_vanderpauw_sheet_resistance(r_a: float, r_b: float) -> float:
    """Sheet resistance from the vdP equation exp(-πR_a/Rs)+exp(-πR_b/Rs)=1.

    Self-contained (no scipy): the left side is monotonic in Rs, so a bisection
    on the same bracket as the reference implementation converges to the root.
    """
    r_a, r_b = abs(float(r_a)), abs(float(r_b))
    if r_a == 0.0 and r_b == 0.0:
        return 0.0
    if math.isclose(r_a, r_b, rel_tol=1e-9, abs_tol=1e-12):
        return math.pi * r_a / math.log(2.0)

    def f(rs: float) -> float:
        return math.exp(-math.pi * r_a / rs) + math.exp(-math.pi * r_b / rs) - 1.0

    lo = max(min(r_a, r_b) * 0.1, 1e-12)
    hi = max(r_a, r_b) * 1000.0 + 1e-12
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0.0:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-12 * max(1.0, hi):
            break
    return 0.5 * (lo + hi)


def vanderpauw_sheet(
    name: str,
    step_a: str,
    step_b: str,
    meter_id: str,
) -> CrossStepQuantity:
    """R_sheet from two vdP steps: R = signal/I per step, combined via the vdP eq."""

    def fn(cycle: Mapping[str, Mapping[str, Reading]], ctx: DerivedContext) -> float:
        amp = ctx.amplitude_for(meter_id)
        if amp == 0.0 or step_a not in cycle or step_b not in cycle:
            return 0.0
        r_a = _signal(cycle[step_a], ctx, meter_id) / amp
        r_b = _signal(cycle[step_b], ctx, meter_id) / amp
        return solve_vanderpauw_sheet_resistance(r_a, r_b)

    return CrossStepQuantity(name, fn)
