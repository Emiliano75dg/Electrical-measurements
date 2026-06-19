"""Derived quantities and the van der Pauw solver (core/derived.py)."""

import math

import pytest

from core.channels import Reading
from core.derived import (
    DerivedContext,
    Geometry,
    hall_preset,
    per_meter_generic,
    phase,
    resistance,
    resistivity_longitudinal,
    resistivity_transverse,
    solve_vanderpauw_sheet_resistance,
    vanderpauw_sheet,
)


def _ctx(amp=1e-3, geometry=None, lockin=None):
    # single source → amplitude_for() falls back to it for any meter
    return DerivedContext(
        source_amplitudes={"S1": amp},
        geometry=geometry or Geometry(),
        meter_is_lockin=lockin or {},
    )


# ── building blocks ──────────────────────────────────────────────────────────

def test_resistance_lockin_uses_x_over_amplitude():
    readings = {"Vxx": Reading(x=0.12, y=0.0)}
    q = resistance("Rxx", "Vxx")
    assert q(readings, _ctx(amp=1e-3, lockin={"Vxx": True})) == pytest.approx(120.0)


def test_resistance_dc_uses_dc_value():
    readings = {"V": Reading(dc=0.05)}
    q = resistance("R", "V")
    assert q(readings, _ctx(amp=1e-3, lockin={"V": False})) == pytest.approx(50.0)


def test_resistance_zero_amplitude_is_safe():
    q = resistance("R", "V")
    assert q({"V": Reading(x=1.0)}, _ctx(amp=0.0, lockin={"V": True})) == 0.0


def test_amplitude_for_resolves_per_meter():
    ctx = DerivedContext(
        source_amplitudes={"S1": 1e-3, "S2": 2e-3},
        meter_source={"A": "S1", "B": "S2"},
    )
    assert ctx.amplitude_for("A") == 1e-3
    assert ctx.amplitude_for("B") == 2e-3


def test_resistance_normalised_by_mapped_source():
    # two sources, two meters: each meter divided by its OWN source amplitude
    ctx = DerivedContext(
        source_amplitudes={"S1": 1e-3, "S2": 2e-3},
        meter_source={"A": "S1", "B": "S2"},
        meter_is_lockin={"A": True, "B": True},
    )
    readings = {"A": Reading(x=0.1), "B": Reading(x=0.1)}
    assert resistance("RA", "A")(readings, ctx) == pytest.approx(100.0)   # 0.1 / 1e-3
    assert resistance("RB", "B")(readings, ctx) == pytest.approx(50.0)    # 0.1 / 2e-3


def test_phase_degrees():
    q = phase("phi", "V")
    val = q({"V": Reading(x=1.0, y=1.0)}, _ctx())
    assert val == pytest.approx(45.0)


def test_resistivity_requires_complete_geometry():
    readings = {"Vxx": Reading(x=0.12)}
    incomplete = Geometry(width_m=1e-3, length_m=1e-3, thickness_m=0.0)
    q = resistivity_longitudinal("rho_xx", "Vxx")
    assert q(readings, _ctx(geometry=incomplete, lockin={"Vxx": True})) == 0.0


def test_resistivity_longitudinal_formula():
    readings = {"Vxx": Reading(x=0.12)}
    geo = Geometry(width_m=2e-3, length_m=5e-3, thickness_m=1e-6)
    q = resistivity_longitudinal("rho_xx", "Vxx")
    r = 0.12 / 1e-3                       # 120 Ω
    expected = r * (geo.width_m * geo.thickness_m) / geo.length_m
    assert q(readings, _ctx(geometry=geo, lockin={"Vxx": True})) == pytest.approx(expected)


def test_resistivity_transverse_formula():
    readings = {"Vxy": Reading(x=-0.035)}
    geo = Geometry(width_m=2e-3, length_m=5e-3, thickness_m=1e-6)
    q = resistivity_transverse("rho_xy", "Vxy")
    r = -0.035 / 1e-3
    assert q(readings, _ctx(geometry=geo, lockin={"Vxy": True})) == pytest.approx(r * geo.thickness_m)


# ── presets ──────────────────────────────────────────────────────────────────

def test_hall_preset_names_without_geometry():
    names = [q.name for q in hall_preset()]
    assert names == ["Rxx", "Rxy", "phi_xx_deg", "phi_xy_deg"]


def test_hall_preset_adds_resistivity_with_valid_geometry():
    geo = Geometry(width_m=1e-3, length_m=1e-3, thickness_m=1e-6)
    names = [q.name for q in hall_preset(geometry=geo)]
    assert "rho_xx" in names and "rho_xy" in names


def test_per_meter_generic_resistance_and_phase():
    qs = per_meter_generic(["A", "B"], lockin_ids=["A"])
    names = [q.name for q in qs]
    assert names == ["R_A", "R_B", "phi_A_deg"]


# ── van der Pauw solver ──────────────────────────────────────────────────────

def test_vdp_symmetric_closed_form():
    # R_a == R_b → R_sheet = pi*R/ln2
    r = 120.0
    rs = solve_vanderpauw_sheet_resistance(r, r)
    assert rs == pytest.approx(math.pi * r / math.log(2.0), rel=1e-9)


def test_vdp_zero_zero():
    assert solve_vanderpauw_sheet_resistance(0.0, 0.0) == 0.0


def test_vdp_asymmetric_satisfies_equation():
    r_a, r_b = 100.0, 180.0
    rs = solve_vanderpauw_sheet_resistance(r_a, r_b)
    residual = math.exp(-math.pi * r_a / rs) + math.exp(-math.pi * r_b / rs) - 1.0
    assert abs(residual) < 1e-9


def test_vdp_uses_absolute_values():
    # sign of the measured resistance must not matter
    assert solve_vanderpauw_sheet_resistance(-120.0, 120.0) == pytest.approx(
        solve_vanderpauw_sheet_resistance(120.0, 120.0)
    )


def test_vanderpauw_sheet_cross_step_quantity():
    q = vanderpauw_sheet("R_sheet", "a", "b", "V")
    cycle = {
        "a": {"V": Reading(dc=0.12)},   # R_a = 0.12 / 1e-3 = 120
        "b": {"V": Reading(dc=0.12)},
    }
    ctx = _ctx(amp=1e-3, lockin={"V": False})
    assert q(cycle, ctx) == pytest.approx(math.pi * 120.0 / math.log(2.0), rel=1e-9)


def test_vanderpauw_sheet_missing_step_returns_zero():
    q = vanderpauw_sheet("R_sheet", "a", "b", "V")
    assert q({"a": {"V": Reading(dc=0.1)}}, _ctx(amp=1e-3, lockin={"V": False})) == 0.0
