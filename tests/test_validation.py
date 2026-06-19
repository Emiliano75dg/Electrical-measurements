"""Domain configuration validation (core/validation.py)."""

from core.channels import Func, MeterConfig, SourceConfig
from core.session import (
    MatrixSettings,
    MeterSpec,
    Session,
    SourceSpec,
)
from core.validation import validate_configuration
from measurements.routing import MatrixLayout, RouteStep


def _valid() -> Session:
    """One current source + one lock-in meter referencing it — runnable."""
    return Session(
        sources=[SourceSpec(1, SourceConfig(func=Func.I_AC, amplitude=1e-3))],
        meters=[MeterSpec(1, "Vxx", MeterConfig(lockin=True, reference="S1"))],
    )


def test_valid_returns_no_errors():
    assert validate_configuration(_valid()) == []


def test_empty_requires_source_and_meter():
    errs = validate_configuration(Session())
    assert any("source is required" in e for e in errs)
    assert any("meter is required" in e for e in errs)


def test_duplicate_source_slot():
    s = _valid()
    s.sources.append(SourceSpec(1, SourceConfig(func=Func.I_AC, amplitude=1e-3)))
    assert any("slot S1" in e for e in validate_configuration(s))


def test_duplicate_meter_id():
    s = _valid()
    s.meters.append(MeterSpec(2, "Vxx", MeterConfig(lockin=True, reference="S1")))
    assert any("Duplicate meter id 'Vxx'" in e for e in validate_configuration(s))


def test_reference_to_missing_source():
    s = _valid()
    s.meters = [MeterSpec(1, "Vxx", MeterConfig(lockin=True, reference="S9"))]
    assert any("does not exist" in e for e in validate_configuration(s))


def test_current_reversal_without_current_source():
    s = Session(
        sources=[SourceSpec(1, SourceConfig(func=Func.V_DC, amplitude=0.1))],
        meters=[MeterSpec(1, "V", MeterConfig(lockin=False))],
        current_reversal=True,
    )
    assert any("no current source" in e for e in validate_configuration(s))


def test_current_reversal_rejects_multiple_current_sources():
    s = Session(
        sources=[
            SourceSpec(1, SourceConfig(func=Func.I_AC, amplitude=1e-3)),
            SourceSpec(2, SourceConfig(func=Func.I_DC, amplitude=2e-3)),
        ],
        meters=[MeterSpec(1, "V", MeterConfig(lockin=False))],
        current_reversal=True,
    )
    assert any("exactly one current source" in e for e in validate_configuration(s))


def test_ambiguous_dc_meter_with_two_current_sources():
    s = Session(
        sources=[
            SourceSpec(1, SourceConfig(func=Func.I_AC, amplitude=1e-3)),
            SourceSpec(2, SourceConfig(func=Func.I_DC, amplitude=1e-3)),
        ],
        meters=[MeterSpec(1, "V", MeterConfig(lockin=False))],
    )
    assert any("unambiguously normalised" in e for e in validate_configuration(s))


def test_multi_source_lockin_with_references_is_valid():
    s = Session(
        sources=[
            SourceSpec(1, SourceConfig(func=Func.I_AC, amplitude=1e-3)),
            SourceSpec(2, SourceConfig(func=Func.I_AC, amplitude=2e-3)),
        ],
        meters=[
            MeterSpec(1, "A", MeterConfig(lockin=True, reference="S1")),
            MeterSpec(2, "B", MeterConfig(lockin=True, reference="S2")),
        ],
    )
    assert validate_configuration(s) == []


def test_matrix_duplicate_column():
    s = _valid()
    s.matrix = MatrixSettings(enabled=True)
    s.layout = MatrixLayout(terminal_row={"I+": 1, "I-": 2}, contact_col={"C1": 1, "C2": 1})
    assert any("same column" in e for e in validate_configuration(s))


def test_matrix_vdp_needs_two_steps():
    s = _valid()
    s.matrix = MatrixSettings(enabled=True, vdp_sheet=True)
    s.layout = MatrixLayout(terminal_row={"I+": 1}, contact_col={"C1": 1})
    s.routes = [RouteStep("only", [("I+", "C1")])]
    assert any("at least two route steps" in e for e in validate_configuration(s))


def test_matrix_disabled_skips_routing_checks():
    # a broken layout is ignored while the matrix is off
    s = _valid()
    s.layout = MatrixLayout(terminal_row={"A": 1, "B": 1})   # duplicate row
    assert validate_configuration(s) == []
