"""Routing tab — Keithley 7709 single-pole matrix (REDESIGN.md §7 Phase 4).

The user maps routed terminals to matrix rows and sample contacts to columns,
defines an ordered list of route steps (each a set of terminal→contact links),
and the acquisition engine iterates them — closing/opening relays between steps
(van der Pauw, contact rotation).  A read-only 6×8 grid previews the crosspoints
closed by the selected step.  Hall and van der Pauw presets fill everything in.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from measurements.routing import (
    MatrixLayout,
    RouteStep,
    hall_routing,
    vanderpauw_routing,
    xpt,
)

_ROWS, _COLS = 6, 8


def _links_to_text(links: list[tuple[str, str]]) -> str:
    return "; ".join(f"{t}={c}" for t, c in links)


def _text_to_links(text: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for part in text.replace(",", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        term, _, contact = part.partition("=")
        term, contact = term.strip(), contact.strip()
        if term and contact:
            links.append((term, contact))
    return links


class _MapTable(QTableWidget):
    """A two-column (name, integer) editable table with add/remove."""

    def __init__(self, key_header: str, val_header: str, val_max: int) -> None:
        super().__init__(0, 2)
        self._val_max = val_max
        self.setHorizontalHeaderLabels([key_header, val_header])
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.verticalHeader().setVisible(False)
        self.setMaximumHeight(150)

    def add_entry(self, key: str, value: int) -> None:
        r = self.rowCount()
        self.insertRow(r)
        self.setItem(r, 0, QTableWidgetItem(key))
        self.setItem(r, 1, QTableWidgetItem(str(value)))

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.selectedIndexes()}, reverse=True)
        for r in rows:
            self.removeRow(r)

    def clear_entries(self) -> None:
        self.setRowCount(0)

    def to_dict(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in range(self.rowCount()):
            k_item, v_item = self.item(r, 0), self.item(r, 1)
            if k_item is None or v_item is None:
                continue
            key = k_item.text().strip()
            if not key:
                continue
            try:
                out[key] = int(v_item.text())
            except ValueError:
                continue
        return out


class RoutingPanel(QWidget):
    """Matrix connection + layout + route steps, with a 6×8 preview and presets."""

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(8)

        # ── Matrix connection ────────────────────────────────────────────────────
        conn_box = QGroupBox("7709 matrix")
        cf = QFormLayout(conn_box)
        self._enabled = QCheckBox("Use the switching matrix")
        self._simulated = QCheckBox("Simulation (no hardware)")
        self._simulated.setChecked(True)
        self._resource = QLineEdit()
        self._resource.setPlaceholderText("TCPIP0::192.168.0.2::inst0::INSTR")
        self._settle = QDoubleSpinBox()
        self._settle.setRange(0.0, 5.0); self._settle.setDecimals(3)
        self._settle.setValue(0.05); self._settle.setSuffix(" s relay")
        cf.addRow(self._enabled)
        cf.addRow(self._simulated)
        cf.addRow("VISA resource:", self._resource)
        cf.addRow("Relay settle:", self._settle)
        outer.addWidget(conn_box)

        # ── Layout ────────────────────────────────────────────────────────────────
        lay_box = QGroupBox("Layout  (terminal → row 1-6,  contact → column 1-8)")
        lv = QVBoxLayout(lay_box)
        tbls = QHBoxLayout()
        self._terms = _MapTable("Terminal", "Row", _ROWS)
        self._contacts = _MapTable("Contact", "Col", _COLS)
        tbls.addWidget(self._terms)
        tbls.addWidget(self._contacts)
        lv.addLayout(tbls)
        tbtn = QHBoxLayout()
        for label, fn in (
            ("+ term", lambda: self._terms.add_entry("T", 1)),
            ("− term", self._terms.remove_selected),
            ("+ contact", lambda: self._contacts.add_entry("C", 1)),
            ("− contact", self._contacts.remove_selected),
        ):
            b = QPushButton(label); b.clicked.connect(fn); tbtn.addWidget(b)
        lv.addLayout(tbtn)
        outer.addWidget(lay_box)

        # ── Route steps ─────────────────────────────────────────────────────────────
        step_box = QGroupBox("Route steps  (link:  T=Cn; T=Cn …)")
        sv = QVBoxLayout(step_box)
        self._steps = QTableWidget(0, 2)
        self._steps.setHorizontalHeaderLabels(["Label", "Links"])
        self._steps.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._steps.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._steps.verticalHeader().setVisible(False)
        self._steps.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._steps.setMaximumHeight(150)
        self._steps.itemSelectionChanged.connect(self._update_preview)
        self._steps.itemChanged.connect(lambda *_: self._update_preview())
        sv.addWidget(self._steps)
        sbtn = QHBoxLayout()
        b_add = QPushButton("+ step"); b_add.clicked.connect(lambda: self._add_step("step", []))
        b_del = QPushButton("− step"); b_del.clicked.connect(self._remove_step)
        sbtn.addWidget(b_add); sbtn.addWidget(b_del)
        sv.addLayout(sbtn)
        outer.addWidget(step_box)

        # ── Preview 6×8 ──────────────────────────────────────────────────────────────
        prev_box = QGroupBox("Crosspoint preview  (selected step)")
        pv = QVBoxLayout(prev_box)
        self._grid = QTableWidget(_ROWS, _COLS)
        self._grid.setHorizontalHeaderLabels([f"c{c}" for c in range(1, _COLS + 1)])
        self._grid.setVerticalHeaderLabels([f"r{r}" for r in range(1, _ROWS + 1)])
        self._grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for r in range(_ROWS):
            self._grid.setRowHeight(r, 22)
            for c in range(_COLS):
                self._grid.setItem(r, c, QTableWidgetItem(""))
        self._grid.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._grid.setMaximumHeight(190)
        pv.addWidget(self._grid)
        self._preview_note = QLabel("—")
        self._preview_note.setStyleSheet("color: gray; font-size: 8pt;")
        pv.addWidget(self._preview_note)
        outer.addWidget(prev_box)

        # ── Cross-step analysis ───────────────────────────────────────────────────────
        self._vdp_sheet = QCheckBox("van der Pauw R_sheet  (cross-step, 'combined' row)")
        self._vdp_sheet.setToolTip(
            "Compute the sheet resistance from the first two route steps\n"
            "(R per step = V/I, combined with the van der Pauw equation).\n"
            "Emitted as one 'combined' row per cycle, on the first meter."
        )
        outer.addWidget(self._vdp_sheet)

        # ── Presets ──────────────────────────────────────────────────────────────────
        pbtn = QHBoxLayout()
        b_hall = QPushButton("⟳  Hall preset"); b_hall.clicked.connect(lambda: self._load_preset(hall_routing, False))
        b_vdp = QPushButton("⟳  van der Pauw preset"); b_vdp.clicked.connect(lambda: self._load_preset(vanderpauw_routing, True))
        pbtn.addWidget(b_hall); pbtn.addWidget(b_vdp)
        outer.addLayout(pbtn)
        outer.addStretch()

        self._load_preset(hall_routing, False)

    # ── step table helpers ──────────────────────────────────────────────────────

    def _add_step(self, label: str, links: list[tuple[str, str]]) -> None:
        r = self._steps.rowCount()
        self._steps.insertRow(r)
        self._steps.setItem(r, 0, QTableWidgetItem(label))
        self._steps.setItem(r, 1, QTableWidgetItem(_links_to_text(links)))

    def _remove_step(self) -> None:
        rows = sorted({i.row() for i in self._steps.selectedIndexes()}, reverse=True)
        for r in rows:
            self._steps.removeRow(r)
        self._update_preview()

    def _selected_step_index(self) -> int:
        rows = [i.row() for i in self._steps.selectedIndexes()]
        return rows[0] if rows else (0 if self._steps.rowCount() else -1)

    # ── preview ─────────────────────────────────────────────────────────────────

    def _update_preview(self) -> None:
        closed_color = QColor("#2e7d32")
        for r in range(_ROWS):
            for c in range(_COLS):
                item = self._grid.item(r, c)
                item.setText("")
                item.setBackground(QColor(0, 0, 0, 0))
        idx = self._selected_step_index()
        routes = self.routes()
        if idx < 0 or idx >= len(routes):
            self._preview_note.setText("—")
            return
        try:
            layout = self.layout()
            channels = routes[idx].channels(layout)
        except (KeyError, ValueError) as exc:
            self._preview_note.setText(f"⚠  {exc}")
            return
        for ch in channels:
            r, c = (ch - 1) // _COLS, (ch - 1) % _COLS
            if 0 <= r < _ROWS and 0 <= c < _COLS:
                item = self._grid.item(r, c)
                item.setText(str(ch))
                item.setBackground(closed_color)
                item.setForeground(QColor("white"))
        self._preview_note.setText(
            f"{routes[idx].label}:  channels {sorted(channels)}"
        )

    # ── presets ─────────────────────────────────────────────────────────────────

    def _load_preset(self, factory, vdp_sheet: bool) -> None:
        layout, steps = factory()
        self.restore(
            enabled=self._enabled.isChecked(),
            resource=self._resource.text(),
            simulated=self._simulated.isChecked(),
            settle_s=self._settle.value(),
            layout=layout,
            routes=steps,
            vdp_sheet=vdp_sheet,
        )

    # ── accessors for MainWindow ──────────────────────────────────────────────────

    @property
    def matrix_enabled(self) -> bool:
        return self._enabled.isChecked()

    @property
    def matrix_simulated(self) -> bool:
        return self._simulated.isChecked()

    @property
    def matrix_resource(self) -> str:
        return self._resource.text().strip()

    @property
    def matrix_settle_s(self) -> float:
        return self._settle.value()

    @property
    def vdp_sheet_enabled(self) -> bool:
        return self._vdp_sheet.isChecked()

    def layout(self) -> MatrixLayout:
        return MatrixLayout(
            terminal_row=self._terms.to_dict(),
            contact_col=self._contacts.to_dict(),
        )

    def routes(self) -> list[RouteStep]:
        out: list[RouteStep] = []
        for r in range(self._steps.rowCount()):
            label_item, links_item = self._steps.item(r, 0), self._steps.item(r, 1)
            if label_item is None:
                continue
            label = label_item.text().strip() or f"step{r}"
            links = _text_to_links(links_item.text() if links_item else "")
            out.append(RouteStep(label, links))
        return out

    def restore(
        self,
        enabled: bool,
        resource: str,
        simulated: bool,
        settle_s: float,
        layout: MatrixLayout,
        routes: list[RouteStep],
        vdp_sheet: bool = False,
    ) -> None:
        self._enabled.setChecked(enabled)
        self._resource.setText(resource)
        self._simulated.setChecked(simulated)
        self._settle.setValue(settle_s)
        self._vdp_sheet.setChecked(vdp_sheet)

        self._terms.clear_entries()
        for term, row in layout.terminal_row.items():
            self._terms.add_entry(term, row)
        self._contacts.clear_entries()
        for contact, col in layout.contact_col.items():
            self._contacts.add_entry(contact, col)

        self._steps.setRowCount(0)
        for step in routes:
            self._add_step(step.label, step.links)
        if self._steps.rowCount():
            self._steps.selectRow(0)
        self._update_preview()
