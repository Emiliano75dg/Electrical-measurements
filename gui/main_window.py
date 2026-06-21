"""
Main application window — generic channel-based measurements (redesign v2).

Layout
──────
  Left panel  : tabs "Connections" (connection + mock) and "Channels" (N sources /
                N meters, Hall preset)
  Right panel : DynamicPlotWidget (selectable series) + readout strip
  Toolbar     : Connect · Start · Stop · Single · Clear · Save folder

The acquisition runs through the generic AcquisitionWorker driven by the typed
channel adapters built from the Channels tab; the Hall bar is just a preset.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.derived import Geometry, vanderpauw_sheet
from core.session import (
    ConnectionSettings,
    DEFAULT_M81_ID,
    DEFAULT_MATRIX_ID,
    MatrixSettings,
    MeterSpec,
    Session,
    SourceSpec,
    load_session,
    save_session,
    synthesize_default_instruments,
)
from core.channels import MeterChannel, SourceChannel
from core.validation import validate_configuration
from instruments.m81 import M81Instrument
from instruments.matrix7709 import Matrix7709
from instruments.registry import (
    Keithley7709LabInstrument,
    M81LabInstrument,
    Registry,
)
from measurements.engine import AcquisitionWorker
from gui.channels_tab import ChannelsPanel
from gui.config_panel import ConnectionPanel, MockPanel
from gui.dynamic_plot import DynamicPlotWidget
from gui.routing_tab import RoutingPanel


def _is_raw(name: str) -> bool:
    return name.endswith(("_X", "_Y", "_DC"))


class _ReadoutStrip(QWidget):
    """Compact strip: latest derived values + point counter."""

    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        font = QFont()
        font.setPointSize(12)
        font.setBold(True)

        self._values = QLabel("—")
        self._values.setFont(font)
        self._n = QLabel("n = 0")
        self._n.setStyleSheet("color: gray;")

        layout.addWidget(self._values)
        layout.addStretch()
        layout.addWidget(self._n)

    def set_row(self, row: dict, n: int) -> None:
        parts = [
            f"{k} = {v:.4g}"
            for k, v in row.items()
            if k != "time_s" and not _is_raw(k) and isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        self._values.setText("   ".join(parts[:4]) if parts else "—")
        self._n.setText(f"n = {n}")

    def reset(self) -> None:
        self._values.setText("—")
        self._n.setText("n = 0")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ELECMEAS  —  Generic measurements")
        self.resize(1280, 720)

        self._instrument: M81Instrument | None = None
        self._matrix: Matrix7709 | None = None
        self._worker: AcquisitionWorker | None = None

        self._data_dir = Path.home() / "Documents" / "elecmeas_data"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._build_ui()
        self._refresh_buttons()
        self._mock.field_changed.connect(self._on_mock_field)
        self._mock.temperature_changed.connect(self._on_mock_temperature)
        self._channels.source_configs_changed.connect(self._on_source_live)
        self._channels.meter_configs_changed.connect(self._on_meter_live)

    # ── UI construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        # Left: config tabs
        self._connection = ConnectionPanel()
        self._mock = MockPanel()
        self._channels = ChannelsPanel()
        self._routing = RoutingPanel()

        self._mock.setVisible(self._connection.simulated)
        self._connection.simulation_toggled.connect(self._mock.setVisible)

        conn_inner = QWidget()
        cl = QVBoxLayout(conn_inner)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.addWidget(self._connection)
        cl.addWidget(self._mock)
        cl.addStretch()
        conn_scroll = QScrollArea()
        conn_scroll.setWidget(conn_inner)
        conn_scroll.setWidgetResizable(True)

        chan_scroll = QScrollArea()
        chan_scroll.setWidget(self._channels)
        chan_scroll.setWidgetResizable(True)

        rout_scroll = QScrollArea()
        rout_scroll.setWidget(self._routing)
        rout_scroll.setWidgetResizable(True)

        tabs = QTabWidget()
        tabs.addTab(conn_scroll, "Connections")
        tabs.addTab(chan_scroll, "Channels")
        tabs.addTab(rout_scroll, "Routing")
        tabs.setMinimumWidth(420)
        tabs.setMaximumWidth(520)
        splitter.addWidget(tabs)

        # Right: plot + readout
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 4, 4, 4)
        rl.setSpacing(4)
        self._plot = DynamicPlotWidget()
        self._readout = _ReadoutStrip()
        rl.addWidget(self._plot, stretch=1)
        rl.addWidget(self._readout)
        splitter.addWidget(right)
        splitter.setSizes([440, 840])
        splitter.setStretchFactor(1, 1)

        # Toolbar
        tb = QToolBar("Main toolbar")
        tb.setMovable(False)
        self.addToolBar(tb)

        self._btn_connect = QPushButton("Connect")
        self._btn_start   = QPushButton("▶  Start")
        self._btn_stop    = QPushButton("■  Stop")
        self._btn_single  = QPushButton("Single")
        self._btn_clear   = QPushButton("Clear plot")
        self._btn_savedir = QPushButton("Save folder…")
        self._btn_save_setup = QPushButton("Save setup…")
        self._btn_load_setup = QPushButton("Load setup…")

        self._btn_connect.clicked.connect(self._on_connect_toggle)
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_single.clicked.connect(self._on_single)
        self._btn_clear.clicked.connect(self._on_clear)
        self._btn_savedir.clicked.connect(self._on_choose_dir)
        self._btn_save_setup.clicked.connect(self._on_save_setup)
        self._btn_load_setup.clicked.connect(self._on_load_setup)

        for btn in (self._btn_connect, self._btn_start, self._btn_stop,
                    self._btn_single, self._btn_clear, self._btn_savedir):
            tb.addWidget(btn)
            btn.setMinimumWidth(90)
        tb.addSeparator()
        for btn in (self._btn_save_setup, self._btn_load_setup):
            tb.addWidget(btn)
            btn.setMinimumWidth(90)

        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Disconnected")

    # ── button state ──────────────────────────────────────────────────────────────

    def _refresh_buttons(self) -> None:
        connected = self._instrument is not None and self._instrument.connected
        running   = self._worker is not None and self._worker.isRunning()
        self._btn_connect.setText("Disconnect" if connected else "Connect")
        self._btn_start.setEnabled(connected and not running)
        self._btn_stop.setEnabled(running)
        self._btn_single.setEnabled(connected and not running)

    # ── connect / disconnect ────────────────────────────────────────────────────

    def _on_connect_toggle(self) -> None:
        if self._instrument and self._instrument.connected:
            self._do_disconnect()
        else:
            self._do_connect()

    def _do_connect(self) -> None:
        inst = M81Instrument(self._connection.ip_address, simulated=self._connection.simulated)
        try:
            idn = inst.connect()
        except Exception as exc:
            QMessageBox.critical(self, "Connection failed", str(exc))
            return
        self._instrument = inst

        matrix_msg = ""
        if self._routing.matrix_enabled:
            mtx = Matrix7709(
                resource=self._routing.matrix_resource,
                simulated=self._routing.matrix_simulated,
                settle_s=self._routing.matrix_settle_s,
            )
            try:
                midn = mtx.connect()
            except Exception as exc:
                inst.disconnect()
                self._instrument = None
                QMessageBox.critical(self, "Matrix connection failed", str(exc))
                return
            self._matrix = mtx
            matrix_msg = f"  +  {midn}"

        self._status.showMessage(f"Connected  ·  {idn}{matrix_msg}")
        self._refresh_buttons()

    def _do_disconnect(self) -> None:
        if self._worker and self._worker.isRunning():
            self._on_stop()
            self._worker.wait(3_000)
        if self._matrix:
            self._matrix.disconnect()
            self._matrix = None
        if self._instrument:
            self._instrument.disconnect()
            self._instrument = None
        self._status.showMessage("Disconnected")
        self._refresh_buttons()

    # ── channel construction ──────────────────────────────────────────────────────

    def _build_registry(self) -> Registry:
        """Assemble the instrument registry from the live facades.

        The GUI does not yet expose a per-channel instrument selector, so every
        channel binds to the default M81 (multi-instrument config is file-driven
        this step).  The registry wraps the same live ``M81Instrument`` /
        ``Matrix7709`` the connect flow created — no behaviour change.
        """
        registry = Registry()
        registry.add(M81LabInstrument(self._instrument, instrument_id=DEFAULT_M81_ID))
        if self._matrix is not None:
            registry.add(
                Keithley7709LabInstrument(self._matrix, instrument_id=DEFAULT_MATRIX_ID)
            )
        return registry

    def _build_channels(self) -> tuple[list[SourceChannel], list[MeterChannel]]:
        registry = self._build_registry()
        sources = [
            registry.resolve_source(None, port, cfg)
            for port, cfg in self._channels.source_specs()
        ]
        meters = [
            registry.resolve_meter(None, port, cfg, mid)
            for port, cfg, mid in self._channels.meter_specs()
        ]
        return sources, meters

    def _make_worker(self, save_path: Path) -> AcquisitionWorker | None:
        # single domain-level gate: clear messages instead of cryptic runtime errors
        errors = validate_configuration(self._capture_session())
        if errors:
            QMessageBox.warning(
                self, "Invalid configuration", "\n".join(f"•  {e}" for e in errors)
            )
            return None

        sources, meters = self._build_channels()

        matrix = layout = steps = None
        cross = None
        if self._matrix is not None and self._routing.matrix_enabled:
            layout = self._routing.layout()
            steps = self._routing.routes()
            if steps:
                matrix = self._matrix
                if self._routing.vdp_sheet_enabled and len(steps) >= 2:
                    meter_ids = [mid for _, _, mid in self._channels.meter_specs()]
                    cross = [vanderpauw_sheet("R_sheet", steps[0].label, steps[1].label, meter_ids[0])]

        return AcquisitionWorker(
            sources, meters, save_path,
            derived=self._channels.derived(),
            geometry=self._channels.geometry(),
            settle_s=self._channels.settle_s,
            interval_s=self._channels.interval_s,
            current_reversal=self._channels.current_reversal,
            matrix=matrix,
            layout=layout if matrix is not None else None,
            steps=steps if matrix is not None else None,
            cross_derived=cross,
        )

    # ── start / stop ──────────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self._data_dir / f"meas_{ts}.csv"
        worker = self._make_worker(path)
        if worker is None:
            return

        self._worker = worker
        self._worker.sample_ready.connect(self._on_data)
        self._worker.status_changed.connect(self._status.showMessage)
        self._worker.error_occurred.connect(self._on_worker_error)
        self._worker.finished.connect(self._on_run_finished)
        self._worker.start()
        self._channels.set_acquisition_active(True)

        self._status.showMessage(f"Acquiring  →  {path.name}")
        self._refresh_buttons()

    def _on_stop(self) -> None:
        if self._worker:
            self._worker.stop()

    def _on_run_finished(self) -> None:
        self._channels.set_acquisition_active(False)
        self._refresh_buttons()

    # ── live parameter updates (pushed to the running worker) ──────────────────────

    def _on_source_live(self, cfgs: dict) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.update_source_configs(cfgs)

    def _on_meter_live(self, cfgs: dict) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.update_meter_configs(cfgs)

    # ── single shot ───────────────────────────────────────────────────────────────

    def _on_single(self) -> None:
        worker = self._make_worker(Path("/dev/null"))
        if worker is None:
            return
        try:
            row = worker.read_single()
        except Exception as exc:
            QMessageBox.warning(self, "Single shot error", str(exc))
            return
        finally:
            worker.disable_sources()
        self._plot.append_row(row)
        self._readout.set_row(row, self._plot.n_points)

    # ── data callback ─────────────────────────────────────────────────────────────

    def _on_data(self, row: dict) -> None:
        self._plot.append_row(row)
        self._readout.set_row(row, self._plot.n_points)

    def _on_worker_error(self, msg: str) -> None:
        QMessageBox.warning(self, "Measurement error", msg)
        self._refresh_buttons()

    # ── misc actions ──────────────────────────────────────────────────────────────

    def _on_clear(self) -> None:
        self._plot.clear()
        self._readout.reset()

    def _on_choose_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select data folder", str(self._data_dir))
        if d:
            self._data_dir = Path(d)
            self._status.showMessage(f"Data folder: {d}")

    # ── session persistence (Phase 3) ─────────────────────────────────────────────

    def _capture_session(self) -> Session:
        """Snapshot the full setup from the panels into a serialisable Session."""
        connection = ConnectionSettings(
            ip_address=self._connection.ip_address,
            simulated=self._connection.simulated,
        )
        matrix = MatrixSettings(
            enabled=self._routing.matrix_enabled,
            resource=self._routing.matrix_resource,
            simulated=self._routing.matrix_simulated,
            settle_s=self._routing.matrix_settle_s,
            vdp_sheet=self._routing.vdp_sheet_enabled,
        )
        return Session(
            connection=connection,
            # GUI channels bind to the default M81 (no per-channel selector yet),
            # so the captured registry is the synthesized default — recorded so
            # saved files are self-describing v2 sessions.
            instruments=synthesize_default_instruments(connection, matrix),
            sources=[SourceSpec(port, cfg) for port, cfg in self._channels.source_specs()],
            meters=[MeterSpec(port, mid, cfg) for port, cfg, mid in self._channels.meter_specs()],
            derived_mode=self._channels.derived_mode,
            geometry=self._channels.geometry(),
            settle_s=self._channels.settle_s,
            interval_s=self._channels.interval_s,
            current_reversal=self._channels.current_reversal,
            matrix=matrix,
            layout=self._routing.layout(),
            routes=self._routing.routes(),
        )

    def _apply_session(self, s: Session) -> None:
        """Restore the panels from a loaded Session."""
        self._connection.set_ip_address(s.connection.ip_address)
        self._connection.set_simulated(s.connection.simulated)
        self._channels.restore(
            [(sp.port, sp.config) for sp in s.sources],
            [(mp.port, mp.config, mp.meter_id) for mp in s.meters],
            s.derived_mode,
            s.geometry,
            s.settle_s,
            s.interval_s,
            current_reversal=s.current_reversal,
        )
        self._routing.restore(
            enabled=s.matrix.enabled,
            resource=s.matrix.resource,
            simulated=s.matrix.simulated,
            settle_s=s.matrix.settle_s,
            layout=s.layout,
            routes=s.routes,
            vdp_sheet=s.matrix.vdp_sheet,
        )

    def _on_save_setup(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save setup", str(self._data_dir / "setup.json"), "Setup JSON (*.json)"
        )
        if not path:
            return
        if not path.endswith(".json"):
            path += ".json"
        try:
            save_session(self._capture_session(), path)
        except Exception as exc:
            QMessageBox.critical(self, "Save setup failed", str(exc))
            return
        self._status.showMessage(f"Setup saved  →  {Path(path).name}")

    def _on_load_setup(self) -> None:
        if self._worker and self._worker.isRunning():
            QMessageBox.warning(self, "Acquisition running",
                                "Stop the acquisition before loading a setup.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load setup", str(self._data_dir), "Setup JSON (*.json)"
        )
        if not path:
            return
        try:
            session = load_session(path)
        except Exception as exc:
            QMessageBox.critical(self, "Load setup failed", str(exc))
            return
        self._apply_session(session)
        self._status.showMessage(f"Setup loaded  ←  {Path(path).name}")

    # ── mock parameter forwarding ─────────────────────────────────────────────────

    def _on_mock_field(self, v: float) -> None:
        if self._instrument:
            self._instrument.set_mock_field(v)

    def _on_mock_temperature(self, v: float) -> None:
        if self._instrument:
            self._instrument.set_mock_temperature(v)

    # ── clean shutdown ────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3_000)
        if self._matrix:
            self._matrix.disconnect()
        if self._instrument:
            self._instrument.disconnect()
        event.accept()
