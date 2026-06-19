"""Single-pole matrix routing model (measurements/routing.py)."""

import pytest

from measurements.routing import (
    MatrixLayout,
    RouteStep,
    hall_routing,
    vanderpauw_routing,
    xpt,
)


def test_xpt_channel_formula():
    assert xpt(1, 1) == 1
    assert xpt(1, 8) == 8
    assert xpt(2, 1) == 9
    assert xpt(6, 3) == 43
    assert xpt(3, 1) == 17


def test_layout_validate_ok():
    MatrixLayout(terminal_row={"I+": 1, "I-": 2}, contact_col={"C1": 1, "C2": 2}).validate()


def test_layout_rejects_too_many_rows():
    layout = MatrixLayout(terminal_row={f"T{i}": i for i in range(1, 8)})
    with pytest.raises(ValueError, match="max 6 rows"):
        layout.validate()


def test_layout_rejects_too_many_columns():
    layout = MatrixLayout(contact_col={f"C{i}": i for i in range(1, 10)})
    with pytest.raises(ValueError, match="max 8 columns"):
        layout.validate()


def test_layout_rejects_row_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        MatrixLayout(terminal_row={"T": 7}).validate()


def test_layout_rejects_duplicate_rows():
    with pytest.raises(ValueError, match="same row"):
        MatrixLayout(terminal_row={"A": 1, "B": 1}).validate()


def test_layout_rejects_duplicate_columns():
    with pytest.raises(ValueError, match="same column"):
        MatrixLayout(contact_col={"C1": 1, "C2": 1}).validate()


def test_routestep_channels_resolution():
    layout = MatrixLayout(
        terminal_row={"I+": 3, "I-": 4}, contact_col={"C1": 1, "C4": 4}
    )
    step = RouteStep("s", [("I+", "C1"), ("I-", "C4")])
    assert step.channels(layout) == [xpt(3, 1), xpt(4, 4)]


def test_routestep_unrouted_terminal_raises():
    layout = MatrixLayout(terminal_row={"I+": 1}, contact_col={"C1": 1})
    with pytest.raises(KeyError, match="not routed"):
        RouteStep("s", [("I-", "C1")]).channels(layout)


def test_routestep_unmapped_contact_raises():
    layout = MatrixLayout(terminal_row={"I+": 1}, contact_col={"C1": 1})
    with pytest.raises(KeyError, match="not mapped"):
        RouteStep("s", [("I+", "C9")]).channels(layout)


def test_hall_routing_preset_resolves():
    layout, steps = hall_routing()
    layout.validate()
    assert len(steps) == 1
    chans = steps[0].channels(layout)
    assert len(chans) == 6
    assert all(1 <= c <= 48 for c in chans)


def test_vanderpauw_routing_two_steps():
    layout, steps = vanderpauw_routing()
    layout.validate()
    assert [s.label for s in steps] == ["R_12_43", "R_23_14"]
    for s in steps:
        assert len(s.channels(layout)) == 4
