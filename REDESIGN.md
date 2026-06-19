# ELECMEAS — Redesign v2 (flexible channels + 7709 matrix)

> **Status: fully implemented (completed 2026-06-14).** This is the original
> design document — the plan and the decisions that drove the v2 rewrite. It is
> kept as a historical record of *why* the architecture looks the way it does.
> For the **as-built** structure and APIs, see `ARCHITECTURE.md`; the file layout
> proposed in §3.1 below differs in places from what was finally shipped (e.g.
> the GUI panels ended up in `config_panel.py` / `channels_tab.py` /
> `routing_tab.py` / `dynamic_plot.py`).

> Original preamble: design document, the starting point for implementation.
> Read it BEFORE writing code. The current app works and must stay working at
> every phase (mock backend always available, no hardware required for
> development).

---

## 1. Goal

Move the app from a layout **hard-wired to the Hall bar** (one source + exactly
two measure ports `Vxx`/`Vxy`) to a **declarative** model where the user
configures an arbitrary set of **sources** and **meters**, maps them to the
physical modules, and optionally routes them through the matrix. The Hall bar
becomes a simple *preset* of this generic engine, not a special case in the code.

---

## 2. Hardware reality (decided — do not assume otherwise)

### 2.1 M81-SSM — the one "rich" instrument
All the lab's modules live in **a single `SSMSystem` instance** (the `lakeshore`
driver, `pip install lakeshore`). The M81 hosts up to **3 source modules + 3
measure modules**. Inventory:

- 1 current source (BCS-10)
- 1 voltage source (VS-10)
- 2 voltage meters (VM-10)
- 1 SMU module (source-measure unit — sources AND measures together; it may
  appear as a source channel and/or a meter in the GUI). The `SMU-*` modes
  already exist in the current code.

Consequence: MeasureSync and the lock-in phase reference apply across **all**
modules (they are in the same M81). Multi-channel lock-in measurement is
therefore phase-coherent. There is no external instrument breaking that coherence.

### 2.2 Keithley 7709 matrix (inside a DAQ6510) — router only
The only external instrument, over **pyvisa** SCPI. Decided role: **pure router
of sample contacts**. The M81 sources AND measures; the DAQ6510's DMM is NOT
involved (no channels 49/50, no 2/4-wire paths).

Physical constraints of the 7709 (from the manual, to be respected in the model):

- The crosspoint is mechanically **2-pole** (it closes the HI and LO relays
  together). You cannot close just one pole.
- **Chosen usage scheme: only the HI pin of each port is wired** (the LO pins
  are left disconnected; the LO relay closes into nothing, harmless). This turns
  the 2-pole 6×8 into a **single-pole 6×8**: each port carries ONE conductor.
  - **Rows = single instrument conductors** (max 6 wires routed).
  - **Columns = single sample contacts** (max 8 contacts).
  - **Crosspoint (r, c) = links a wire to a contact.**
  - Shared contacts are trivial: several crosspoints closed on the same column.
- **LO handling: configurable per instrument.** Each terminal is either `ROUTED`
  (occupies a row, routable) or `FIXED` (wired outside the matrix: a shared
  common or a fixed contact). Typical: HI always ROUTED, LO as chosen.
  Constraint: total ROUTED terminals ≤ 6, contacts ≤ 8.
- Channel numbering: `channel = (row − 1) · 8 + column`.
  (check: ch 43 = row 6 / col 3; ch 17 = row 3 / col 1)
- Use **rows 3–6** (reserved for sources/external instruments). Close with
  `:ROUT:MULT:CLOS` — multiple-close closes ONLY the listed channels, so the DMM
  stays out. `:ROUT:OPEN:ALL` opens everything.
- The relays are latching, but `*RST`/`reset()` opens them after a few seconds.
  Always start from `open_all()` in setup and teardown.

---

## 3. Target architecture

### 3.1 Core abstractions

Three concepts solve most of it: **typed channels**, **hardware adapters**, a
**generic acquisition plan**.

```
core/
  channels.py    # Protocol SourceChannel/MeterChannel + SourceConfig/MeterConfig/Reading
  derived.py     # derived-quantity presets (hall, vdp, sheet R, …)
  session.py     # save/load the complete setup (JSON)
instruments/
  m81.py         # SSMSystem facade (connection, slot enumeration)
  m81_channels.py# per-module adapters (source: BCS-10/VS-10/SMU; measure: VM-10/SMU)
  matrix7709.py  # Matrix7709 (pyvisa): open_all / close(channels)
  mock.py        # simulated backend (reuse/adapt the existing one)
measurements/
  engine.py      # generic AcquisitionWorker (QThread, iterates the RouteSteps)
  routing.py     # MatrixLayout, RouteStep, xpt(), hall/vdp presets
gui/
  instruments_tab.py
  channels_tab.py
  routing_tab.py
  plot_widget.py # selectable series (raw channels + derived)
  main_window.py
```

### 3.2 Agreed interfaces (signatures — binding in spirit, not to the letter)

```python
# core/channels.py
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

class Func(str, Enum):
    I_AC = "I_AC"; I_DC = "I_DC"
    V_AC = "V_AC"; V_DC = "V_DC"

@dataclass
class SourceConfig:
    func: Func = Func.I_AC
    amplitude: float = 1e-6        # A if current source, V if voltage source
    frequency_Hz: float = 17.77    # AC only
    compliance: float = 1.0        # V if I-source, A if V-source

@dataclass
class MeterConfig:
    lockin: bool = True
    reference: str | None = "S1"   # which AC source provides the reference (M81)
    time_constant_s: float = 0.3
    rolloff: str = "R24"
    phase_shift_deg: float = 0.0
    use_fir: bool = True
    nplc: float = 1.0              # DC mode

@dataclass
class Reading:
    x: float = 0.0; y: float = 0.0     # AC (V)
    dc: float = 0.0                    # DC (V)
    unit: str = "V"

class SourceChannel(Protocol):
    id: str
    config: SourceConfig
    def configure(self, cfg: SourceConfig) -> None: ...
    def enable(self) -> None: ...
    def disable(self) -> None: ...

class MeterChannel(Protocol):
    id: str
    config: MeterConfig
    def configure(self, cfg: MeterConfig) -> None: ...
    def read(self) -> Reading: ...
```

> As-built note: the shipped `MeterConfig` also carries `harmonic` and `smu`,
> added during implementation.

```python
# measurements/routing.py
from dataclasses import dataclass
from enum import Enum

def xpt(row: int, col: int) -> int:            # crosspoint -> channel number
    return (row - 1) * 8 + col

class TermMode(str, Enum):
    ROUTED = "routed"     # occupies a row; routable to any column
    FIXED  = "fixed"      # wired outside the matrix (common or fixed contact)

@dataclass
class TerminalBinding:
    terminal_id: str                  # "Vxx+", "Vxx-", "I+", ...
    mode: TermMode = TermMode.ROUTED
    row: int | None = None            # if ROUTED: row 1-6
    fixed_to: str | None = None       # if FIXED: common or contact (doc only)

@dataclass
class MatrixLayout:
    terminal_row: dict[str, int]      # ROUTED terminals only -> row 1-6
    contact_col:  dict[str, int]      # contact -> column 1-8
    def validate(self) -> None:
        assert len(self.terminal_row) <= 6, "max 6 rows (ROUTED terminals)"
        assert len(self.contact_col)  <= 8, "max 8 columns (contacts)"

@dataclass
class RouteStep:
    label: str
    links: list[tuple[str, str]]      # (ROUTED terminal_id, contact_id) — one wire, one contact
    def channels(self, lay: MatrixLayout) -> list[int]:
        return [xpt(lay.terminal_row[t], lay.contact_col[c]) for t, c in self.links]

# Hall bar preset: C2 shared between Vxx and Vxy -> two crosspoints on the same column.
# FIXED terminals (e.g. a common LO) do NOT appear in the links: they are static wiring.
HALL = RouteStep("hall", [
    ("I+", "C1"),  ("I-", "C4"),
    ("Vxx+", "C2"), ("Vxx-", "C3"),
    ("Vxy+", "C2"), ("Vxy-", "C5"),
])
```

```python
# measurements/engine.py — loop sketch
class AcquisitionWorker(QThread):
    sample_ready   = Signal(dict)
    status_changed = Signal(str)
    error_occurred = Signal(str)

    def run(self):
        for s in self.sources: s.configure(s.config)
        for m in self.meters:  m.configure(m.config)
        steps = self.steps or [RouteStep("static", [])]
        while self._running:
            for step in steps:
                if self.matrix:
                    self.matrix.open_all()
                    self.matrix.close(step.channels(self.layout))
                for s in self.sources: s.enable()
                self._settle()
                readings = {m.id: m.read() for m in self.meters}
                row = {"t": t, "step": step.label}
                row.update(self._flatten(readings))          # Vxx_X, Vxx_Y, ...
                row.update(self.derived(readings, self.sources))  # Rxx, Rxy, rho...
                self.sample_ready.emit(row)
                self._write_csv(row)
        # finally: disable all sources + matrix.open_all()
```

The engine **knows nothing** about "Hall": geometry and formulas live in the
`derived.py` presets and in the `RouteStep`/`MatrixLayout`.

---

## 4. Lake Shore driver — API notes (verify the exact names in the driver)

Confirmed from the `lakeshore` driver documentation:

- `SSMSystem()` ; `ssm.get_source_module(n)` ; `ssm.get_measure_module(n)`
- SourceModule (BCS-10): `set_frequency(hz)`, `set_i_amplitude(a)`,
  `get_i_amplitude()`, `enable()`
- MeasureModule (VM-10): `setup_lock_in_measurement('S1', tc_s)`,
  `get_lock_in_r()`, `get_dc()`

To **verify in the installed driver** (the SourceModule/MeasureModule sections of
the docs, `dir(module)` at runtime) before using them:

- VS-10 voltage source: `set_v_amplitude(...)` (exact name?)
- lock-in X/Y components: `get_lock_in_x()` / `get_lock_in_y()` (do they exist?
  otherwise derive X, Y from R and θ)
- `disable()` on the source module
- compliance / range / phase shift / FIR: the module's exact methods
- lock-in reference and harmonic beyond the time constant

**Decision (2026-06-13): keep the `electrical_measurements` wrapper**
(`M81Controller`/`MockM81Controller`) behind the adapters. The adapter
(`m81_channels.py`) isolates the choice — migrating to the direct `lakeshore`
driver can be deferred. Note: the mock returns Ω; `M81Meter` must replicate the
current normalisation (×I in current mode) to return `Reading.x/y` in V.

---

## 5. Physical caveats (UI/docs, not logic)

- **Shared reference (common LO).** With LO `FIXED` on a common, meters that
  share it lose reference independence: the measurement is single-ended/referred,
  no longer floating. The Routing tab must flag this. (Shared contacts on the HI
  side, by contrast, are free: a multi-close on the column.)
- **Thermal EMF / relay series R.** The VM-10s measure nV: relays and junctions
  introduce offsets that can dominate. For DC, keep **current reversal** (the
  logic already exists, to be moved into the generic engine). For AC the lock-in
  rejects them.

---

## 6. What to preserve from the current app

- The **mock** backend with realistic physics (development without hardware).
- **Real-time CSV** writing with dynamic columns (depending on the active
  channels + derived). NB: the current CSV already includes `phi_xx_deg` /
  `phi_xy_deg`, undocumented in the README — align the docs.
- Real-time plotting (generalise it: selectable series instead of the 4 fixed plots).
- DC current reversal (`_read_dc_current_reversal_pair`).
- Live update of the lock-in parameters during acquisition (useful; move it into
  the engine under the same instrument-access serialisation lock).

Known bugs NOT to carry over:
- `configure_current_source_ac` silently ignores `compliance_V`: in the new
  adapter the compliance must really be applied, or the option removed.
- Documentation (`ARCHITECTURE.md`, `README.md`) out of sync: update it after the
  redesign (phases, phase computation in the worker, source modes, CSV columns).

---

## 7. Implementation order (keep the app working at every step)

### Phase 1 — Per-module M81 adapters
- `core/channels.py` (Protocols + dataclasses).
- `instruments/m81_channels.py`: `M81Source`, `M81Meter` against the driver.
- `m81.py` facade onto `get_source_module` / `get_measure_module`.
- Mock adapted to the same Protocols.
- **Acceptance:** the app runs with the old Hall behaviour, but internally goes
  through the new channels. Mock works without hardware.

### Phase 2 — Generic engine
- `measurements/engine.py` (`AcquisitionWorker`) on a single static route.
- `core/derived.py` with the `hall` preset (Rxx, Rxy, φ, ρ).
- GUI: a Channels tab to configure N sources / N meters of the M81.
- **Acceptance:** you can run a measurement with a source + ≥2 freely chosen
  meters; the Hall bar is a preset. Dynamic CSV/plot.

### Phase 3 — Session persistence
- `core/session.py`: save/load the complete setup (modules, channels, layout,
  routes, derived) as JSON.
- **Acceptance:** close and reopen, reloading an identical setup from a file.

### Phase 4 — Matrix
- `instruments/matrix7709.py` (pyvisa): `open_all`, `close(channels)`.
- `measurements/routing.py`: `MatrixLayout`, `RouteStep`, `xpt`, presets.
- Engine: iterate the `RouteStep`s (van der Pauw, contact rotation).
- GUI: a Routing tab with a 6×8 grid + presets.
- **Acceptance:** a multi-route measurement (e.g. vdP) that closes/opens routes
  between steps.

---

## 8. Decisions (resolved 2026-06-13)

- **Matrix/analysis stack:** reimplement the **single-pole** model from §3.1
  (`routing.py`, `MatrixLayout`, `RouteStep`, `xpt`) — do NOT reuse the external
  package's stack (`Matrix7709`/`ContactMap`), which is 2-pole HI+LO and
  contradicts the single-pole scheme of §2.2. Only the **analysis formulas**
  (`symmetrize`/`antisymmetrize`/`hall`/`vanderpauw` from
  `electrical_measurements.analysis`) may be imported rather than rewritten.
- **SMU** (source-measure unit, not "MSU"): sources AND measures together. The
  `SMU-*` modes already exist in the code. For now Phases 1–3 focus on
  BCS-10 / VS-10 / VM-10; exposing the SMU as a source+meter channel in the
  generic model is to be defined (how many source/measure slots it uses).
- **Measurement scope:** a **generic, arbitrary** engine — no privileged preset.
  The user defines N sources / N meters and any routes; the Hall bar and van der
  Pauw are just presets built on top. Size the matrix budget for the worst case
  (up to 8 columns / 6 rows).
- **Current reversal:** at the **RouteStep/state** level (+I and −I as two steps,
  recombined with `antisymmetrize`), not as a meter read mode. Consistent with
  van der Pauw and reuses the existing analysis.

### Consequences for the plan
- §4 resolved: start from the existing wrapper (see the note in §4).
- §3.1: `derived.py` may import the formulas from the external analysis;
  `routing.py` stays single-pole and new.
- Proposed default LO in the GUI: HI `ROUTED`, LO `FIXED` on a common
  (single-pole).
```
