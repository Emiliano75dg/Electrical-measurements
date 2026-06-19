"""Channels tab — declarative configuration of N sources and N meters.

This replaces the Hall-wired Source/Sense panels with the generic model of the
v2 redesign: the user adds an arbitrary set of source and meter channels, picks
which derived quantities to compute, and the Hall bar is just a preset button.

Exposes typed accessors so MainWindow can build M81Source/M81Meter adapters and
feed the generic AcquisitionWorker without knowing widget internals.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.channels import Func, MeterConfig, SourceConfig
from core.derived import (
    DerivedQuantity,
    Geometry,
    hall_preset,
    per_meter_generic,
)

_FUNC_LABELS = {
    "I  AC  (lock-in)": Func.I_AC,
    "I  DC": Func.I_DC,
    "V  AC  (lock-in)": Func.V_AC,
    "V  DC": Func.V_DC,
}
_ROLLOFFS = ["R6", "R12", "R18", "R24"]
# settle multipliers (×τ) for 1% lock-in settling per roll-off order
_SETTLE_MULT = {"R6": 4.6, "R12": 7.5, "R18": 10.0, "R24": 13.0}


class SourceRow(QFrame):
    """One source channel: port · function · amplitude · frequency · compliance."""

    changed = Signal()
    remove_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(4)

        self._port = QSpinBox()
        self._port.setRange(1, 3)
        self._port.setPrefix("S")

        self._func = QComboBox()
        self._func.addItems(list(_FUNC_LABELS))

        self._amp = QDoubleSpinBox()
        self._amp.setRange(0.001, 100_000.0)
        self._amp.setDecimals(3)
        self._amp.setValue(10.0)

        self._freq = QDoubleSpinBox()
        self._freq.setRange(0.01, 2_000.0)
        self._freq.setDecimals(3)
        self._freq.setValue(17.77)
        self._freq.setSuffix(" Hz")

        self._comp = QDoubleSpinBox()
        self._comp.setRange(0.001, 100_000.0)
        self._comp.setDecimals(3)
        self._comp.setValue(1.0)

        self._btn_del = QPushButton("✕")
        self._btn_del.setFixedWidth(24)
        self._btn_del.clicked.connect(lambda: self.remove_requested.emit(self))

        for w in (self._port, self._func, self._amp, self._freq, self._comp):
            row.addWidget(w)
        row.addWidget(self._btn_del)

        self._func.currentTextChanged.connect(self._on_func_changed)
        for w in (self._port, self._amp, self._freq, self._comp):
            (w.valueChanged if isinstance(w, QDoubleSpinBox) else w.valueChanged).connect(
                lambda *_: self.changed.emit()
            )
        self._func.currentTextChanged.connect(lambda *_: self.changed.emit())
        self._on_func_changed(self._func.currentText())

    def _on_func_changed(self, text: str) -> None:
        func = _FUNC_LABELS[text]
        if func.is_current:
            self._amp.setSuffix(" µA")
            self._comp.setSuffix(" V")
        else:
            self._amp.setSuffix(" mV")
            self._comp.setSuffix(" µA")
        self._freq.setEnabled(func.is_ac)

    def set_locked(self, locked: bool) -> None:
        """Lock structural fields (port, function, delete) during acquisition.

        Amplitude / frequency / compliance stay editable so they can be pushed
        live to the running worker.
        """
        for w in (self._port, self._func, self._btn_del):
            w.setEnabled(not locked)

    @property
    def source_id(self) -> str:
        return f"S{self._port.value()}"

    def spec(self) -> tuple[int, SourceConfig]:
        func = _FUNC_LABELS[self._func.currentText()]
        amp = self._amp.value() * (1e-6 if func.is_current else 1e-3)
        comp = self._comp.value() * (1.0 if func.is_current else 1e-6)
        return self._port.value(), SourceConfig(
            func=func, amplitude=amp, frequency_Hz=self._freq.value(), compliance=comp
        )

    def set_values(self, port: int, func: Func, amp_display: float, freq: float) -> None:
        self._port.setValue(port)
        for label, f in _FUNC_LABELS.items():
            if f is func:
                self._func.setCurrentText(label)
                break
        self._amp.setValue(amp_display)
        self._freq.setValue(freq)

    def apply_config(self, port: int, cfg: SourceConfig) -> None:
        """Restore every widget from a SourceConfig (inverse of spec(), SI → display)."""
        self._port.setValue(port)
        for label, f in _FUNC_LABELS.items():
            if f is cfg.func:
                self._func.setCurrentText(label)
                break
        is_current = cfg.func.is_current
        self._amp.setValue(cfg.amplitude / (1e-6 if is_current else 1e-3))
        self._freq.setValue(cfg.frequency_Hz)
        self._comp.setValue(cfg.compliance / (1.0 if is_current else 1e-6))


class MeterRow(QFrame):
    """One meter channel: name · port · detection · reference · tc/nplc · roll-off · FIR."""

    changed = Signal()
    remove_requested = Signal(object)

    def __init__(self, source_ids: list[str]) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(4)

        self._name = QLineEdit("M1")
        self._name.setFixedWidth(54)

        self._port = QSpinBox()
        self._port.setRange(1, 3)
        self._port.setPrefix("M")

        self._det = QComboBox()
        self._det.addItems(["Lock-in", "DC", "SMU"])
        self._det.setToolTip("Lock-in/DC: VM-10 on an M slot.  SMU: reads the source-measure\n"
                             "unit on its source slot S (current when V-sourcing, V when I-sourcing).")

        self._ref = QComboBox()
        self._ref.addItems(source_ids or ["S1"])

        self._tc = QDoubleSpinBox()
        self._tc.setRange(0.001, 1000.0)
        self._tc.setDecimals(3)
        self._tc.setValue(0.3)
        self._tc.setSuffix(" s")

        self._rolloff = QComboBox()
        self._rolloff.addItems(_ROLLOFFS)
        self._rolloff.setCurrentText("R24")

        self._harm = QSpinBox()
        self._harm.setRange(1, 3)
        self._harm.setPrefix("h")
        self._harm.setToolTip("Lock-in harmonic to detect (1 = fundamental).")

        self._phase = QDoubleSpinBox()
        self._phase.setRange(-360.0, 360.0)
        self._phase.setDecimals(1)
        self._phase.setSingleStep(1.0)
        self._phase.setSuffix(" °")
        self._phase.setToolTip("Lock-in reference phase shift (compensates cable/capacitive delays).")

        self._fir = QPushButton("FIR")
        self._fir.setCheckable(True)
        self._fir.setChecked(True)
        self._fir.setFixedWidth(36)

        self._btn_del = QPushButton("✕")
        self._btn_del.setFixedWidth(24)
        self._btn_del.clicked.connect(lambda: self.remove_requested.emit(self))

        for w in (self._name, self._port, self._det, self._ref, self._tc,
                  self._rolloff, self._harm, self._phase, self._fir):
            row.addWidget(w)
        row.addWidget(self._btn_del)

        self._det.currentTextChanged.connect(self._on_det_changed)
        self._det.currentTextChanged.connect(lambda *_: self.changed.emit())
        self._name.textChanged.connect(lambda *_: self.changed.emit())
        self._port.valueChanged.connect(lambda *_: self.changed.emit())
        # live-updatable detection params also notify (drive worker live + warning)
        self._tc.valueChanged.connect(lambda *_: self.changed.emit())
        self._rolloff.currentTextChanged.connect(lambda *_: self.changed.emit())
        self._fir.toggled.connect(lambda *_: self.changed.emit())
        self._ref.currentTextChanged.connect(lambda *_: self.changed.emit())
        self._harm.valueChanged.connect(lambda *_: self.changed.emit())
        self._phase.valueChanged.connect(lambda *_: self.changed.emit())
        self._on_det_changed(self._det.currentText())

    def _on_det_changed(self, text: str) -> None:
        lockin = text == "Lock-in"
        smu = text == "SMU"
        self._tc.setSuffix(" s" if lockin else " NPLC")
        # SMU reads a source slot (Sn); VM-10 modes read a measure slot (Mn)
        self._port.setPrefix("S" if smu else "M")
        for w in (self._ref, self._rolloff, self._fir, self._harm, self._phase):
            w.setEnabled(lockin)
        # the SMU has no integration window of its own here; tc is irrelevant
        self._tc.setEnabled(not smu)

    @property
    def lockin(self) -> bool:
        return self._det.currentText() == "Lock-in"

    @property
    def smu(self) -> bool:
        return self._det.currentText() == "SMU"

    def set_locked(self, locked: bool) -> None:
        """Lock structural fields (name, port, detection, delete) during acquisition.

        Reference / time-constant / roll-off / FIR stay editable for live updates.
        """
        for w in (self._name, self._port, self._det, self._btn_del):
            w.setEnabled(not locked)

    def set_reference_options(self, source_ids: list[str]) -> None:
        current = self._ref.currentText()
        self._ref.blockSignals(True)
        self._ref.clear()
        self._ref.addItems(source_ids or ["S1"])
        if current in source_ids:
            self._ref.setCurrentText(current)
        self._ref.blockSignals(False)

    def spec(self) -> tuple[int, MeterConfig, str]:
        lockin = self.lockin
        smu = self.smu
        cfg = MeterConfig(
            lockin=lockin,
            reference=self._ref.currentText() if lockin else None,
            harmonic=self._harm.value(),
            time_constant_s=self._tc.value() if lockin else 0.3,
            rolloff=self._rolloff.currentText(),
            phase_shift_deg=self._phase.value(),
            use_fir=self._fir.isChecked(),
            nplc=self._tc.value() if not (lockin or smu) else 1.0,
            smu=smu,
        )
        prefix = "SMU" if smu else "M"
        return self._port.value(), cfg, self._name.text().strip() or f"{prefix}{self._port.value()}"

    def set_values(self, name: str, port: int, lockin: bool, reference: str) -> None:
        self._name.setText(name)
        self._port.setValue(port)
        self._det.setCurrentText("Lock-in" if lockin else "DC")
        if reference:
            self._ref.setCurrentText(reference)

    def apply_config(self, port: int, cfg: MeterConfig, meter_id: str) -> None:
        """Restore every exposed widget from a MeterConfig (inverse of spec())."""
        self._name.setText(meter_id)
        self._det.setCurrentText("SMU" if cfg.smu else ("Lock-in" if cfg.lockin else "DC"))
        self._port.setValue(port)            # after det: prefix already switched
        if cfg.reference:
            self._ref.setCurrentText(cfg.reference)
        self._tc.setValue(cfg.time_constant_s if cfg.lockin else cfg.nplc)
        self._rolloff.setCurrentText(cfg.rolloff)
        self._harm.setValue(cfg.harmonic)
        self._phase.setValue(cfg.phase_shift_deg)
        self._fir.setChecked(cfg.use_fir)


class ChannelsPanel(QWidget):
    """Generic source/meter configuration with a Hall bar preset."""

    # live-update streams to the running worker: {channel id: config}
    source_configs_changed = Signal(dict)
    meter_configs_changed = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(8)

        self._sources: list[SourceRow] = []
        self._meters: list[MeterRow] = []

        # ── Sources ─────────────────────────────────────────────────────────────
        src_box = QGroupBox("Sources")
        sv = QVBoxLayout(src_box)
        self._src_list = QVBoxLayout()
        self._src_list.setSpacing(3)
        sv.addLayout(self._src_list)
        self._add_src_btn = QPushButton("+ source")
        self._add_src_btn.clicked.connect(lambda: self.add_source())
        sv.addWidget(self._add_src_btn)
        outer.addWidget(src_box)

        # ── Meters ──────────────────────────────────────────────────────────────
        met_box = QGroupBox("Meter")
        mv = QVBoxLayout(met_box)
        self._met_list = QVBoxLayout()
        self._met_list.setSpacing(3)
        mv.addLayout(self._met_list)
        self._add_met_btn = QPushButton("+ meter")
        self._add_met_btn.clicked.connect(lambda: self.add_meter())
        mv.addWidget(self._add_met_btn)
        outer.addWidget(met_box)

        # ── Derived + geometry ───────────────────────────────────────────────────
        der_box = QGroupBox("Derived quantities")
        dv = QVBoxLayout(der_box)
        self._derived_mode = QComboBox()
        self._derived_mode.addItems([
            "None",
            "R and φ per meter",
            "Hall preset (Rxx, Rxy, ρ)",
        ])
        dv.addWidget(self._derived_mode)

        geo_row = QHBoxLayout()
        self._w = QDoubleSpinBox(); self._w.setRange(0, 10_000); self._w.setSuffix(" µm w"); self._w.setDecimals(1)
        self._l = QDoubleSpinBox(); self._l.setRange(0, 10_000); self._l.setSuffix(" µm L"); self._l.setDecimals(1)
        self._t = QDoubleSpinBox(); self._t.setRange(0, 1_000); self._t.setSuffix(" nm t"); self._t.setDecimals(2)
        for w in (self._w, self._l, self._t):
            geo_row.addWidget(w)
        dv.addLayout(geo_row)
        note = QLabel("Geometry > 0 on all three for ρ (used by the Hall preset).")
        note.setStyleSheet("color: gray; font-size: 8pt;")
        note.setWordWrap(True)
        dv.addWidget(note)
        outer.addWidget(der_box)

        # ── Acquisition timing ───────────────────────────────────────────────────
        acq_box = QGroupBox("Acquisition")
        acq_v = QVBoxLayout(acq_box)
        av = QHBoxLayout()
        self._settle = QDoubleSpinBox(); self._settle.setRange(0, 120); self._settle.setValue(1.0); self._settle.setSuffix(" s settle")
        self._btn_auto = QPushButton("Auto"); self._btn_auto.setFixedWidth(48)
        self._btn_auto.setToolTip("Set the settle to the 1% settling time\n"
                                  "from the current lock-in meters' τ and roll-off.")
        self._btn_auto.clicked.connect(self._on_auto_settle)
        self._interval = QDoubleSpinBox(); self._interval.setRange(0.05, 60); self._interval.setValue(0.5); self._interval.setDecimals(2); self._interval.setSuffix(" s interval")
        av.addWidget(self._settle)
        av.addWidget(self._btn_auto)
        av.addWidget(self._interval)
        acq_v.addLayout(av)
        self._reversal = QPushButton("Current reversal  (+I / −I)")
        self._reversal.setCheckable(True)
        self._reversal.setToolTip(
            "Measures at +I and −I and keeps the odd part (V+ − V−)/2.\n"
            "Rejects current-independent offsets (thermal EMF,\n"
            "relay series voltages).  Most useful in DC."
        )
        acq_v.addWidget(self._reversal)
        self._warning = QLabel(""); self._warning.setWordWrap(True); self._warning.setVisible(False)
        self._warning.setStyleSheet(
            "color:#7d4000; background:#fff3cd; border:1px solid #e6a817;"
            "border-radius:4px; font-size:9pt; font-weight:bold; padding:5px 7px;"
        )
        acq_v.addWidget(self._warning)
        self._interval.valueChanged.connect(self._update_warning)
        outer.addWidget(acq_box)

        # ── Preset ───────────────────────────────────────────────────────────────
        self._preset_btn = QPushButton("⟳  Hall bar preset")
        self._preset_btn.clicked.connect(self.load_hall_preset)
        outer.addWidget(self._preset_btn)
        outer.addStretch()

        self.load_hall_preset()

    # ── source/meter management ─────────────────────────────────────────────────

    def add_source(self) -> SourceRow:
        roww = SourceRow()
        roww.remove_requested.connect(self._remove_source)
        roww.changed.connect(self._refresh_references)
        roww.changed.connect(self._emit_source_configs)
        self._sources.append(roww)
        self._src_list.addWidget(roww)
        self._refresh_references()
        return roww

    def add_meter(self) -> MeterRow:
        roww = MeterRow(self._source_ids())
        roww.remove_requested.connect(self._remove_meter)
        roww.changed.connect(self._emit_meter_configs)
        roww.changed.connect(self._update_warning)
        self._meters.append(roww)
        self._met_list.addWidget(roww)
        return roww

    # ── live-update + settling helpers ───────────────────────────────────────────

    def _emit_source_configs(self) -> None:
        self.source_configs_changed.emit({f"S{port}": cfg for port, cfg in self.source_specs()})

    def _emit_meter_configs(self) -> None:
        self.meter_configs_changed.emit({mid: cfg for _, cfg, mid in self.meter_specs()})

    def suggested_settle_s(self) -> float:
        """1% settling time: max over lock-in meters of τ·mult[roll-off] (DC ≈ nplc/50)."""
        delays = []
        for _, cfg, _ in self.meter_specs():
            if cfg.lockin:
                delays.append(cfg.time_constant_s * _SETTLE_MULT.get(cfg.rolloff, 13.0))
            else:
                delays.append(max(0.02, cfg.nplc / 50.0))
        return max(delays) if delays else 0.0

    def _on_auto_settle(self) -> None:
        self._settle.setValue(self.suggested_settle_s())
        self._update_warning()

    def _update_warning(self, *_) -> None:
        suggested = self.suggested_settle_s()
        if suggested > 0 and self._interval.value() < suggested:
            self._warning.setText(f"⚠  interval < settling time  —  {suggested:.2f} s suggested")
            self._warning.setVisible(True)
        else:
            self._warning.setText("")
            self._warning.setVisible(False)

    def _remove_source(self, roww: SourceRow) -> None:
        if roww in self._sources:
            self._sources.remove(roww)
            roww.setParent(None)
            self._refresh_references()

    def _remove_meter(self, roww: MeterRow) -> None:
        if roww in self._meters:
            self._meters.remove(roww)
            roww.setParent(None)

    def _source_ids(self) -> list[str]:
        return [s.source_id for s in self._sources]

    def _refresh_references(self) -> None:
        ids = self._source_ids()
        for m in self._meters:
            m.set_reference_options(ids)

    def _clear(self) -> None:
        for roww in list(self._sources):
            self._remove_source(roww)
        for roww in list(self._meters):
            self._remove_meter(roww)

    # ── presets ─────────────────────────────────────────────────────────────────

    def load_hall_preset(self) -> None:
        self._clear()
        s = self.add_source()
        s.set_values(1, Func.I_AC, 10.0, 17.77)
        mx = self.add_meter()
        mx.set_values("Vxx", 1, True, "S1")
        my = self.add_meter()
        my.set_values("Vxy", 2, True, "S1")
        self._derived_mode.setCurrentText("Hall preset (Rxx, Rxy, ρ)")
        self._update_warning()

    def set_acquisition_active(self, active: bool) -> None:
        """Lock structural controls during acquisition; live params stay editable."""
        for s in self._sources:
            s.set_locked(active)
        for m in self._meters:
            m.set_locked(active)
        for w in (self._add_src_btn, self._add_met_btn, self._preset_btn,
                  self._derived_mode, self._w, self._l, self._t,
                  self._settle, self._btn_auto, self._interval, self._reversal):
            w.setEnabled(not active)

    # ── typed accessors for MainWindow ───────────────────────────────────────────

    def source_specs(self) -> list[tuple[int, SourceConfig]]:
        return [s.spec() for s in self._sources]

    def meter_specs(self) -> list[tuple[int, MeterConfig, str]]:
        return [m.spec() for m in self._meters]

    def geometry(self) -> Geometry:
        return Geometry(self._w.value() * 1e-6, self._l.value() * 1e-6, self._t.value() * 1e-9)

    def set_geometry(self, g: Geometry) -> None:
        self._w.setValue(g.width_m * 1e6)
        self._l.setValue(g.length_m * 1e6)
        self._t.setValue(g.thickness_m * 1e9)

    @property
    def derived_mode(self) -> str:
        return self._derived_mode.currentText()

    def restore(
        self,
        sources: list[tuple[int, SourceConfig]],
        meters: list[tuple[int, MeterConfig, str]],
        derived_mode: str,
        geometry: Geometry,
        settle_s: float,
        interval_s: float,
        current_reversal: bool = False,
    ) -> None:
        """Rebuild the whole panel from a saved setup (inverse of the spec accessors)."""
        self._clear()
        for port, cfg in sources:
            self.add_source().apply_config(port, cfg)
        # sources exist first so each meter's reference combo is already populated
        for port, cfg, mid in meters:
            self.add_meter().apply_config(port, cfg, mid)
        self._refresh_references()
        # re-apply references now that all source ids are present
        for (_, cfg, _), roww in zip(meters, self._meters):
            if cfg.reference:
                roww.set_reference_options(self._source_ids())
                roww._ref.setCurrentText(cfg.reference)

        idx = self._derived_mode.findText(derived_mode)
        if idx >= 0:
            self._derived_mode.setCurrentIndex(idx)
        self.set_geometry(geometry)
        self._settle.setValue(settle_s)
        self._interval.setValue(interval_s)
        self._reversal.setChecked(current_reversal)
        self._update_warning()

    def derived(self) -> list[DerivedQuantity]:
        mode = self._derived_mode.currentText()
        metas = self.meter_specs()
        ids = [mid for _, _, mid in metas]
        lockin_ids = [mid for _, cfg, mid in metas if cfg.lockin]
        if mode.startswith("None") or not ids:
            return []
        if mode.startswith("R and φ"):
            return per_meter_generic(ids, lockin_ids)
        # Preset Hall — first two meters are Vxx, Vxy
        vxx = ids[0]
        vxy = ids[1] if len(ids) > 1 else ids[0]
        return hall_preset(vxx, vxy, self.geometry())

    @property
    def settle_s(self) -> float:
        return self._settle.value()

    @property
    def interval_s(self) -> float:
        return self._interval.value()

    @property
    def current_reversal(self) -> bool:
        return self._reversal.isChecked()
