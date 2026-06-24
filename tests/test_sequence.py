"""Sequence specs: synthesis parity + serialization round-trip (spec 03, incr. 1)."""

from measurements.routing import RouteStep
from measurements.sequence import (
    LoopSpec,
    SequenceSpec,
    StepSpec,
    sequence_from_dict,
    sequence_to_dict,
    synthesize_default_sequence,
)


# ── default-tree synthesis (the parity mechanism) ─────────────────────────────

def _seq(loop: LoopSpec) -> SequenceSpec:
    assert isinstance(loop, LoopSpec)
    assert loop.kind == "forever"
    assert isinstance(loop.child, SequenceSpec)
    return loop.child


def test_synthesize_static_single_step_no_column_no_resettle():
    loop = synthesize_default_sequence(
        None, settle_s=1.0, interval_s=0.5, current_reversal=True, has_cross=False
    )
    assert loop.interval_s == 0.5
    seq = _seq(loop)
    assert seq.cross_derived is False
    assert len(seq.children) == 1
    step = seq.children[0]
    assert step.route is None
    assert step.current_reversal is True
    # static -> no re-settle (the initial settle covers it)
    assert step.settle_s is None


def test_synthesize_single_route_has_no_resettle():
    # 1 route (Hall): multi == False -> settle_s MUST stay None, or a spurious
    # re-settle would creep in.  This is the silent-parity hinge.
    steps = [RouteStep("hall", [])]
    loop = synthesize_default_sequence(
        steps, settle_s=1.0, interval_s=0.5, current_reversal=False, has_cross=False
    )
    seq = _seq(loop)
    assert len(seq.children) == 1
    assert seq.children[0].route == "hall"
    assert seq.children[0].settle_s is None      # multi == False
    assert seq.cross_derived is False


def test_synthesize_multi_route_distributes_resettle():
    # >1 route (vdP): multi == True -> every step carries settle_s.
    steps = [RouteStep("R_a", []), RouteStep("R_b", [])]
    loop = synthesize_default_sequence(
        steps, settle_s=1.0, interval_s=0.5, current_reversal=True, has_cross=True
    )
    seq = _seq(loop)
    assert [s.route for s in seq.children] == ["R_a", "R_b"]
    assert all(s.settle_s == 1.0 for s in seq.children)   # multi == True
    assert all(s.current_reversal is True for s in seq.children)
    assert seq.cross_derived is True


def test_synthesize_multi_with_zero_settle_keeps_value_not_none():
    # multi but settle_s == 0.0: the field is set (0.0, not None) so the
    # "multi" branch is unambiguous; the executor still skips the 0-length sleep.
    steps = [RouteStep("a", []), RouteStep("b", [])]
    loop = synthesize_default_sequence(
        steps, settle_s=0.0, interval_s=0.0, current_reversal=False, has_cross=False
    )
    seq = _seq(loop)
    assert all(s.settle_s == 0.0 for s in seq.children)
    assert all(s.settle_s is not None for s in seq.children)


# ── serialization round-trip ──────────────────────────────────────────────────

def test_roundtrip_explicit_tree():
    tree = LoopSpec(
        kind="forever",
        interval_s=0.0,
        child=SequenceSpec(
            cross_derived=False,
            children=[
                StepSpec(route="Rxx", current_reversal=True, settle_s=0.5),
                StepSpec(route="Rxy", current_reversal=True),
            ],
        ),
    )
    assert sequence_from_dict(sequence_to_dict(tree)) == tree


def test_roundtrip_count_loop_and_static_step():
    tree = LoopSpec(
        kind="count",
        count=3,
        child=SequenceSpec(children=[StepSpec()]),   # static step, all defaults
    )
    assert sequence_from_dict(sequence_to_dict(tree)) == tree


def test_none_roundtrips_to_none():
    assert sequence_to_dict(None) is None
    assert sequence_from_dict(None) is None


def test_to_dict_omits_defaults():
    d = sequence_to_dict(StepSpec())
    assert d == {"type": "step"}          # nothing but the discriminant


def test_synthesized_tree_roundtrips():
    loop = synthesize_default_sequence(
        [RouteStep("a", []), RouteStep("b", [])],
        settle_s=1.0, interval_s=0.5, current_reversal=True, has_cross=True,
    )
    assert sequence_from_dict(sequence_to_dict(loop)) == loop


def test_sweep_loop_round_trips():
    tree = LoopSpec(
        kind="sweep", axis="gate", values=[-40.0, 0.0, 40.0],
        child=LoopSpec(
            kind="forever",
            child=SequenceSpec(children=[StepSpec(route="Rxx"), StepSpec(route="Rxy")]),
        ),
    )
    restored = sequence_from_dict(sequence_to_dict(tree))
    assert restored == tree
    assert restored.kind == "sweep" and restored.axis == "gate"
    assert restored.values == [-40.0, 0.0, 40.0]
