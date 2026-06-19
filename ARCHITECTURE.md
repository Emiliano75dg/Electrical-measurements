# ELECMEAS architecture (v2)

A **declarative, generic** model: typed channels (Protocols) + an acquisition
engine that knows nothing about "Hall". Geometry, formulas and routing live in
the presets, not in the engine. See `REDESIGN.md` for the design decisions.

---

## File layout

```
ELECMEAS/
├── main.py                      # Entry point: QApplication → MainWindow
│
├── core/
│   ├── channels.py              # Func, SourceConfig, MeterConfig, Reading
│   │                            #   + Protocol SourceChannel / MeterChannel
│   ├── derived.py               # DerivedQuantity, CrossStepQuantity, presets,
│   │                            #   van der Pauw solver
│   └── session.py               # Session (save/load the whole setup as JSON)
│
├── instruments/
│   ├── m81.py                   # M81Instrument — M81-SSM facade (real + mock)
│   ├── m81_channels.py          # M81Source, M81Meter, M81SMUMeter (adapters)
│   ├── matrix7709.py            # Matrix7709 — single-pole router (pyvisa + mock)
│   └── _vendor/                 # vendored physics mock (mock.py, exceptions.py)
│
├── measurements/
│   ├── engine.py                # AcquisitionWorker (generic QThread)
│   └── routing.py               # MatrixLayout, RouteStep, xpt, presets
│
├── gui/
│   ├── main_window.py           # MainWindow — coordinates everything
│   ├── config_panel.py          # ConnectionPanel, MockPanel
│   ├── channels_tab.py          # ChannelsPanel (N sources / N meters)
│   ├── routing_tab.py           # RoutingPanel (matrix, layout, steps, preview)
│   └── dynamic_plot.py          # DynamicPlotWidget (selectable series)
│
└── tests/                       # pytest suite (headless, mock backend)
```

---

## Core abstractions (`core/channels.py`)

The engine and the GUI only ever see these types, never a concrete instrument:

```python
class Func(str, Enum):  I_AC, I_DC, V_AC, V_DC   # + is_ac, is_current

@dataclass SourceConfig:  func, amplitude, frequency_Hz, compliance
@dataclass MeterConfig:   lockin, reference, harmonic, time_constant_s,
                          rolloff, phase_shift_deg, use_fir, nplc, smu
@dataclass Reading:       x, y, dc, unit

class SourceChannel(Protocol):  id; config; configure(cfg); enable(); disable()
class MeterChannel(Protocol):   id; config; configure(cfg); read() -> Reading
```

Concrete adapters in `instruments/m81_channels.py`:

| Class | Slot | read() |
|-------|------|--------|
| `M81Source` | source Sn | — (configure/enable/disable) |
| `M81Meter` | measure Mn (VM-10) | X/Y (lock-in) or DC |
| `M81SMUMeter` | source Sn (SMU) | the SMU's own measurement: I when V-sourcing, V when I-sourcing |

---

## Data flow

```
MainWindow
  ├─► M81Instrument.connect()       (Mock or real TCP:7777)
  ├─► [Matrix7709.connect()]        (if routing enabled; mock or pyvisa SCPI)
  │
  ├─► _make_worker(): builds the adapters from the tabs and creates AcquisitionWorker
  │
  └─► AcquisitionWorker.start()  (QThread)
        configure(sources, meters); enable(sources); settle
        while running:
          for step in steps (or a single static route):
            matrix.open_all(); matrix.close(step.channels(layout))   # if matrix
            readings = _acquire_readings()       # +I/−I + antisym if reversal
            row = _build_row(readings, step.label)
            write CSV · emit sample_ready(dict)
          if cross_derived:                      # van der Pauw R_sheet …
            emit "combined" row
        finally: disable(sources); matrix.open_all()
```

`sample_ready(dict)` → `MainWindow._on_data` → `DynamicPlotWidget` + readout.

---

## `measurements/engine.py` — `AcquisitionWorker`

A generic QThread over an arbitrary set of channels. Without a matrix it runs a
single static route (Phase 2 behaviour). Given `matrix`/`layout`/`steps` it
iterates the RouteStep list.

**Constructor (main kwargs):** `derived`, `geometry`, `settle_s`, `interval_s`,
`current_reversal`, `matrix`, `layout`, `steps`, `cross_derived`.

**Signals:** `sample_ready(dict)`, `status_changed(str)`, `error_occurred(str)`.

**Dynamic columns** (`columns()`): `time_s` [, `step`] + per meter `X/Y`
(lock-in) or `DC` + the `DerivedQuantity` names + the `CrossStepQuantity` names.

**Current reversal** (`_acquire_readings` / `_apply_source_sign`): when active,
it reads at +I, flips the sign of the **current** sources only
(`replace(cfg, amplitude=-|amp|)` for `func.is_current`; voltage sources are left
alone), re-settles, reads at −I, restores +I and returns `(p−n)/2` per meter —
all under `self._lock`.

**Meter → source normalisation** (`_context` / `_resolve_meter_source`): each
meter's resistance is `signal / amplitude(its source)`.  The engine builds a
`DerivedContext` with `source_amplitudes` (per source id) and `meter_source`
(meter id → source id).  Resolution: lock-in meters use their `reference`
source; otherwise the single current source, then `reference`, then the first
source.  Ambiguous multi-source setups are rejected by `validate_configuration`
before a worker is built, so the "arbitrary N sources" model is honest rather
than silently normalising everything by `sources[0]`.

**Live update:** `update_source_configs(dict)` / `update_meter_configs(dict)`
reconfigure the channels under the same lock as the reads.

---

## `core/derived.py`

```python
@dataclass DerivedQuantity:   name; fn(readings, ctx) -> float
@dataclass CrossStepQuantity: name; fn(cycle, ctx) -> float   # cycle = {step: readings}
```

- Building blocks: `resistance`, `phase`, `resistivity_longitudinal/transverse`.
- Presets: `hall_preset` (Rxx, Rxy, φ, ρ), `per_meter_generic`.
- Cross-step: `vanderpauw_sheet` + `solve_vanderpauw_sheet_resistance`
  (the vdP equation solved by bisection, **without scipy**).

`DerivedContext` carries `source_amplitudes` (source id → A/V), `meter_source`
(meter id → normalising source id), `geometry` and `meter_is_lockin`;
`amplitude_for(meter_id)` does the per-meter lookup (falling back to the single
source when there is only one).

---

## `core/validation.py` — configuration gate

`validate_configuration(session) -> list[str]` is the single domain-level check
run before **Start** and **Single** (`MainWindow._make_worker`).  It turns a
`Session` into human-readable problems instead of cryptic runtime failures:
duplicate source slots, duplicate meter ids (which would collide into the same
CSV column), lock-in references to non-existent sources, current reversal with
no current source, ambiguous meter→source normalisation, and invalid/ambiguous
routing (delegating to `MatrixLayout.validate`).  Qt-free; an empty list means
runnable.

## `measurements/routing.py` — single-pole matrix

```python
xpt(row, col) = (row-1)*8 + col          # crosspoint → 7709 channel
MatrixLayout: terminal_row{T:1-6}, contact_col{C:1-8}; validate()
RouteStep:    label, links[(T, C)]; channels(layout) -> [int]
hall_routing(), vanderpauw_routing() -> (layout, steps)
```

`instruments/matrix7709.py`: `Matrix7709.open_all()` (`:ROUT:OPEN:ALL`) and
`close(channels)` (`:ROUT:MULT:CLOS (@…)`); `MatrixMock` for development. It
always starts and ends with `open_all()`.

---

## `instruments/m81.py` — `M81Instrument`

A facade that abstracts real hardware vs simulation (chosen at connect time).

| `simulated=True` | `simulated=False` |
|---|---|
| `MockM81Controller` | `M81Controller` (TCP:7777) |
| returns Ω → wrapper × I to give V | returns V |

The mock and the real driver are imported independently (`_MOCK_AVAILABLE` vs
`_HARDWARE_AVAILABLE`), so **simulation works even when `lakeshore` is not
installed** — `connect(simulated=True)` only needs the sibling mock, not the
hardware driver.

Relevant mock notes:
- **`current_sign`**: `_set_mock_current_sign()` tells the mock about the
  polarity in `configure_current_source_*`, so current reversal is reproducible
  (V flips with −I). `_source_amplitude()` always uses `|I|` (the sign lives in
  the mock).
- **`read_smu(port)`**: the SMU's measurement (mock = V/R or I·R with
  R_load = 100 Ω; real = best-effort getter on the source module).
- Mock physics: `Rxx(B,T) = 120 + 8·B² + f(T) Ω`, `Rxy(B) = −35·B Ω`.

---

## `gui/` — panels

| Component | Exposes / signals |
|-----------|-------------------|
| `ConnectionPanel` | `ip_address`, `simulated`; `simulation_toggled` |
| `MockPanel` | `field_T`, `temperature_K`; `field_changed`, `temperature_changed` |
| `ChannelsPanel` | `source_specs()`, `meter_specs()`, `geometry()`, `derived()`, `derived_mode`, `settle_s`, `interval_s`, `current_reversal`; `source_configs_changed`, `meter_configs_changed`; `restore(...)`, `set_acquisition_active(bool)`, `suggested_settle_s()` |
| `RoutingPanel` | `matrix_enabled/simulated/resource/settle_s`, `vdp_sheet_enabled`, `layout()`, `routes()`; `restore(...)` |
| `DynamicPlotWidget` | `append_row(dict)`, `clear()`, `n_points` |

`MainWindow` builds the adapters (`_build_channels`), assembles the worker
(`_make_worker`), forwards the live changes, and handles persistence
(`_capture_session` / `_apply_session`).

---

## Dependency on `M81_electr_meas`

The project is self-contained for everything except real hardware:

| Piece | Source | Needed for |
|-------|--------|-----------|
| `MockM81Controller` (physics mock) | **vendored** in `instruments/_vendor/mock.py` | simulation, tests, CI |
| `M81Controller` (real driver) | sibling `../M81_electr_meas/src` via `sys.path` | real hardware only |

`instruments/_vendor/mock.py` + `exceptions.py` are copied verbatim from the
sibling (only the exceptions import path changed), so they stay trivially
diff-able when the upstream mock physics is updated. `m81.py` imports the mock
from the vendored module (`_MOCK_AVAILABLE` is always true); the real driver is
still guarded behind a `sys.path` insert (`_HARDWARE_AVAILABLE`), so a missing
sibling never breaks simulation. The single-pole matrix model and the van der
Pauw solver are reimplemented locally (no scipy); see §8 of `REDESIGN.md`.

---

## Testing

`tests/` runs headless with the Qt platform forced to `offscreen` (see
`tests/conftest.py`) and the mock backend, so no display and no hardware are
needed:

- `test_derived.py` — building blocks, presets, van der Pauw solver.
- `test_routing.py` — `xpt`, `MatrixLayout.validate`, `RouteStep.channels`, presets.
- `test_session.py` — round-trip, schema versioning, backward compatibility.
- `test_validation.py` — invalid configs (duplicate ports/ids, bad references,
  reversal/normalisation/routing) and valid multi-source setups.
- `test_engine.py` — dynamic columns and current-reversal antisymmetrisation,
  driven through `read_single()` with fake channels (no QThread event loop).
- `test_backend_mock.py` — integration against the physics-aware M81 mock
  (auto-skipped when the `electrical_measurements` backend is unavailable).

Run with `python -m pytest`.

---

## Extending

- **New derived quantity:** add a `DerivedQuantity`/preset in `core/derived.py`
  and expose it in the `ChannelsPanel` combo. The engine picks it up as a column
  automatically.
- **New geometry/route:** add a `(layout, steps)` preset in `routing.py` and a
  button in `RoutingPanel`.
- **New instrument:** implement the `SourceChannel` / `MeterChannel` Protocols;
  the engine and the GUI are unchanged.
```
