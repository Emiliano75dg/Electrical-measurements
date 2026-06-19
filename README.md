# ELECMEAS

Python GUI for electronic transport measurements with the Lake Shore M81-SSM.

The app is built on a **generic, declarative measurement engine**: the user
configures an arbitrary set of **sources** and **meters**, picks the derived
quantities, and optionally routes the sample contacts through a Keithley 7709
matrix. The **Hall bar** and **van der Pauw** geometries are simply *presets*
built on top of this engine, not special cases in the code.

A built-in simulation backend means no hardware is required for development and
testing.

---

## Requirements

| Package | Minimum version | Notes |
|---------|----------------|-------|
| Python | 3.10+ | |
| PySide6 | 6.4+ | |
| pyqtgraph | 0.13+ | |
| numpy | 1.21+ | |
| pyvisa | 1.13+ | real Keithley 7709 matrix only |
| pytest | 7.0+ | development / running the test suite (`.[dev]`) |
| lakeshore | 1.5+ | **real hardware only** (`.[hardware]`) |

The project is **self-contained**: the physics-aware M81 mock is vendored in
`instruments/_vendor`, so simulation, the test suite and CI need no extra
checkout and no hardware driver.

```bash
pip install -e .            # app + console script `elecmeas`
pip install -e ".[dev]"     # + pytest
pip install -e ".[hardware]"  # + lakeshore, for the real M81-SSM
# or, without packaging:    pip install -r requirements.txt
```

**Real hardware only:** the real-hardware driver (`M81Controller`) is *not*
vendored — it is imported from the sibling `M81_electr_meas` project, which must
be present at `../M81_electr_meas/src/` (added to `sys.path` at startup). Its
absence never affects simulation.

---

## Running

```bash
elecmeas                # after `pip install -e .`
# or
python main.py
```

On a remote machine without a display: `DISPLAY=:0 python main.py`.

---

## Tests

The test suite lives in `tests/` and runs fully headless (Qt offscreen, mock
backend — no hardware needed):

```bash
python -m pytest
```

It covers the van der Pauw solver, derived quantities, the single-pole routing
model, session round-trip / schema versioning / backward compatibility, the
acquisition engine (dynamic columns and current-reversal antisymmetrisation),
and an integration test against the physics-aware M81 mock. The integration
test is skipped automatically when the `electrical_measurements` backend is not
on the path.

---

## Interface

```
┌──────────────────────┬────────────────────────────────────┐
│  Tabs:               │   Dynamic plot (series selectable    │
│   Connections        │   via checkboxes; raw traces hidden  │
│   Channels           │   by default)                        │
│   Routing            │                                      │
│                      ├──────────────────────────────────────┤
│                      │   Readout: latest derived · n points │
└──────────────────────┴────────────────────────────────────┘
```

### Toolbar

| Button | Function |
|--------|----------|
| **Connect / Disconnect** | Connect the M81 (+ matrix if enabled) or the mock backend |
| **▶ Start** | Start continuous acquisition, create a new CSV |
| **■ Stop** | Stop the loop; sources are turned off, the matrix is opened |
| **Single** | Acquire a single point |
| **Clear plot** | Clear the plots (not the CSV) |
| **Save folder…** | Destination folder for the CSV files |
| **Save setup… / Load setup…** | Save/load the whole setup as JSON (see *Persistence*) |

---

## Connections tab

- **IP address** — IPv4 address of the M81 (TCP port 7777).
- **Simulation mode** — mock backend with realistic physics; reveals the
  *Simulation parameters* (**B field** −14…14 T, **Temperature** 1…400 K),
  editable in real time even during acquisition.

## Channels tab

Generic model: add N **sources** and N **meters** with `+ source` / `+ meter`;
`✕` removes one.

### Sources
`port S1–S3 · function · amplitude · frequency · compliance`

- **Function**: `I AC` / `I DC` / `V AC` / `V DC`. SMU sources fit here (voltage
  source = `V *`, current source = `I *`); the compliance is applied.
- Amplitude in µA (current) or mV (voltage); compliance in V (current source)
  or µA (voltage source).

### Meters
`name · port · detection · reference · τ/NPLC · roll-off · h(armonic) · phase · FIR`

- **Detection**:
  - **Lock-in** — VM-10 on an M slot; reads X/Y. Parameters: reference (which AC
    source provides the reference), τ, roll-off, harmonic, phase shift, FIR.
  - **DC** — VM-10 on an M slot; reads a DC voltage (the τ field is used as NPLC).
  - **SMU** — reads the *source-measure unit* on its **source slot S** (current
    when voltage-sourcing, voltage when current-sourcing). Does not use a VM-10.

> Source port numbering (S_n) and measure port numbering (M_n) are independent.

### Derived quantities
- **None** · **R and φ per meter** · **Hall preset (Rxx, Rxy, ρ)**.
- **Geometry** (w, L, t): if all > 0, the Hall preset adds `rho_xx` / `rho_xy`.

### Acquisition
- **Settle** — initial pause after turning the sources on. **Auto** sets the
  settling time to the 1% value computed from the lock-in meters' τ and roll-off.
- **Interval** — target period between samples. A **warning** appears if the
  interval is shorter than the settling time.
- **Current reversal (+I / −I)** — measures at +I and −I and keeps the odd part
  `(V+ − V−)/2`, rejecting current-independent offsets (thermal EMF, relay series
  voltages). Only the **current** sources are reversed (voltage sources are left
  alone). Most useful in DC; in AC the lock-in already rejects them.

> Before each Start/Single the setup is validated: duplicate source slots or
> meter names, lock-in references to missing sources, current reversal without a
> current source, ambiguous meter→source normalisation, and invalid routing are
> reported up front instead of failing mid-run.

## Routing tab *(Keithley 7709 matrix, single-pole)*

The 7709 is used as a **pure router of sample contacts**: only the HI pin of each
port is wired, so the mechanically 2-pole 6×8 crosspoint behaves as single-pole
(rows = instrument conductors, columns = sample contacts).

- **7709 matrix** — enable/disable, simulation, VISA resource, relay settle.
- **Layout** — maps *terminal → row* (1–6) and *contact → column* (1–8).
- **Route steps** — an ordered list of steps; each step is a set of links
  `T=Cn; T=Cn …`. The engine opens/closes the relays between steps.
- **6×8 preview** — highlights the closed crosspoints of the selected step.
- **van der Pauw R_sheet (cross-step)** — combines the first two steps
  (R per step = V/I) through the van der Pauw equation; emitted on a `combined`
  row once per cycle.
- **Hall / van der Pauw presets** — populate the layout, steps and (for vdP)
  R_sheet.

---

## Live update

During acquisition the **structural** parameters are locked (source
port/function; meter name/port/detection; add/remove/preset/derived/geometry/
timing/reversal). These remain editable and are applied **in real time** to the
running worker:

- source: amplitude, frequency, compliance;
- lock-in meter: reference, τ, roll-off, harmonic, phase shift, FIR.

---

## Setup persistence

**Save setup… / Load setup…** save/load the whole setup as JSON: connection,
sources, meters, derived quantities, geometry, timing, reversal, and the matrix
configuration (layout, routes, R_sheet). The schema is versioned and backward
compatible: older files missing new keys load with defaults.

---

## CSV output

Saved to `~/Documents/elecmeas_data/` as `meas_YYYYMMDD_HHMMSS.csv`. The
**columns are dynamic** — they depend on the active channels and derived
quantities:

| Column | When | Description |
|--------|------|-------------|
| `time_s` | always | Time since start (s) |
| `step` | with routing | Route step label (or `combined`) |
| `<meter>_X`, `<meter>_Y` | lock-in meter | In-phase / quadrature components (V) |
| `<meter>_DC` | DC or SMU meter | DC value (V) or SMU reading (V/A) |
| `<derived>` | per derived quantity | e.g. `Rxx`, `Rxy`, `phi_xx_deg`, `rho_xx` |
| `R_sheet` | vdP cross-step | Sheet resistance, on the `combined` rows |

With routing, per-step rows (meter columns filled, `R_sheet` empty) and
`combined` rows (cross-step only) coexist; the complementary cells stay empty.

---

## Formulas

```
R          = signal / I_source            (signal = lock-in X, or DC)
φ          = atan2(Y, X) · 180/π          (lock-in phase, in degrees)
ρxx        = Rxx · (w · t) / L            (longitudinal resistivity)
ρxy        = Rxy · t                       (transverse resistivity)
R_sheet    : exp(-π·R_a/Rs) + exp(-π·R_b/Rs) = 1   (van der Pauw)
reversal   : (V(+I) − V(−I)) / 2          (offset rejection)
```
