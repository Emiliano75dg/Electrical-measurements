"""Vendored third-party code.

`mock.py` (the physics-aware M81-SSM mock) and `exceptions.py` are copied
verbatim from the sibling **M81_electr_meas** project
(`electrical_measurements.instruments.mock` / `electrical_measurements.exceptions`,
same author).  They are vendored so that ELECMEAS is self-contained: simulation,
the test suite and CI run with no sibling checkout and no hardware driver — see
ARCHITECTURE.md › "Dependency on M81_electr_meas".

The only edit applied on import is the exceptions import path (`..exceptions` →
`.exceptions`), so the files stay trivially diff-able against upstream when the
mock physics is updated there.  The real-hardware backend (`M81Controller`) is
NOT vendored — it still comes from the sibling and is only needed on the lab
machine.
"""
