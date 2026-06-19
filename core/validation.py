"""Domain-level configuration validation.

A single, Qt-free gate that turns a Session (the same domain model used for
persistence) into a list of human-readable problems.  The GUI calls it before
Start and Single so the user gets a clear message instead of a cryptic runtime
failure once channels are built — e.g. duplicate meter ids silently colliding
into the same CSV column, or an "arbitrary N sources" setup whose meters cannot
be unambiguously normalised.

`validate_configuration(session) -> list[str]` returns an empty list when the
setup is runnable; otherwise one short sentence per problem.
"""

from __future__ import annotations

from collections import Counter

from core.channels import Func
from core.session import Session


def _source_ids(session: Session) -> set[str]:
    return {f"S{s.port}" for s in session.sources}


def validate_configuration(session: Session) -> list[str]:
    errors: list[str] = []

    # ── presence ──────────────────────────────────────────────────────────────
    if not session.sources:
        errors.append("At least one source is required.")
    if not session.meters:
        errors.append("At least one meter is required.")

    # ── duplicate source slots ────────────────────────────────────────────────
    dup_ports = [f"S{p}" for p, n in Counter(s.port for s in session.sources).items() if n > 1]
    for sid in sorted(dup_ports):
        errors.append(f"Two sources share slot {sid} (one module per source slot).")

    # ── duplicate meter ids (would collide into the same CSV columns) ──────────
    dup_ids = [mid for mid, n in Counter(m.meter_id for m in session.meters).items() if n > 1]
    for mid in sorted(dup_ids):
        errors.append(f"Duplicate meter id '{mid}' (meter ids must be unique).")

    src_ids = _source_ids(session)
    current_ids = {f"S{s.port}" for s in session.sources if s.config.func.is_current}

    # ── lock-in references must point to an existing source ───────────────────
    for m in session.meters:
        ref = m.config.reference
        if m.config.lockin and ref is not None and ref not in src_ids:
            errors.append(
                f"Meter '{m.meter_id}' references source {ref}, which does not exist."
            )

    # ── current reversal must target exactly one current source ───────────────
    if session.current_reversal and not current_ids:
        errors.append(
            "Current reversal is enabled but there is no current source to reverse."
        )
    if session.current_reversal and len(current_ids) > 1:
        errors.append(
            "Current reversal requires exactly one current source; "
            "disable the extra current sources or turn reversal off."
        )

    # ── ambiguous meter → source normalisation (multi-source setups) ──────────
    if len(session.sources) > 1:
        for m in session.meters:
            ref = m.config.reference
            resolved = m.config.lockin and ref in src_ids
            if resolved or len(current_ids) == 1:
                continue
            errors.append(
                f"Meter '{m.meter_id}' cannot be unambiguously normalised: with "
                f"{len(session.sources)} sources, set its reference to a single "
                "current source (lock-in) or keep exactly one current source."
            )

    # ── routing (only when the matrix is in play) ─────────────────────────────
    if session.matrix.enabled:
        try:
            session.layout.validate()
        except ValueError as exc:
            errors.append(f"Routing layout: {exc}")
        for step in session.routes:
            try:
                step.channels(session.layout)
            except (KeyError, ValueError) as exc:
                msg = exc.args[0] if exc.args else exc
                errors.append(f"Route step '{step.label}': {msg}")
        if session.matrix.vdp_sheet and len(session.routes) < 2:
            errors.append("van der Pauw R_sheet needs at least two route steps.")

    return errors
