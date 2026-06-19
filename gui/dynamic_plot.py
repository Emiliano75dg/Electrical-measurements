"""Real-time plot with selectable series.

Generalises the old fixed 4-panel Hall plot: series are discovered from the
incoming row dicts (any numeric column except ``time_s``) and each gets a curve
plus a checkbox to toggle its visibility.  Raw lock-in/DC columns (…_X, …_Y,
…_DC) start hidden to reduce clutter; derived columns start visible.

Mixing units on one Y axis is intentional — the user selects which series to
show, so they can compare like with like.
"""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")
pg.setConfigOptions(antialias=True)


def _is_raw(name: str) -> bool:
    return name.endswith(("_X", "_Y", "_DC"))


class DynamicPlotWidget(QWidget):
    """Single live plot whose series are chosen at runtime via checkboxes."""

    def __init__(self) -> None:
        super().__init__()

        self._plot = pg.PlotWidget()
        self._plot.setLabel("bottom", "Time", units="s")
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.addLegend()

        self._checks_host = QWidget()
        self._checks = QVBoxLayout(self._checks_host)
        self._checks.setContentsMargins(4, 4, 4, 4)
        self._checks.addStretch()
        scroll = QScrollArea()
        scroll.setWidget(self._checks_host)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(140)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._plot, stretch=1)
        layout.addWidget(scroll)

        self._t: list[float] = []
        self._series: dict[str, list[float]] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._boxes: dict[str, QCheckBox] = {}

    # ── public API ───────────────────────────────────────────────────────────────

    def append_row(self, row: dict) -> None:
        t = row.get("time_s")
        if t is None:
            return
        self._t.append(float(t))
        for key, value in row.items():
            if key == "time_s" or not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            if key not in self._series:
                self._add_series(key)
            # back-fill so every series stays aligned with the time axis
            data = self._series[key]
            while len(data) < len(self._t) - 1:
                data.append(float("nan"))
            data.append(float(value))
        self._redraw()

    def clear(self) -> None:
        self._t.clear()
        for data in self._series.values():
            data.clear()
        for curve in self._curves.values():
            curve.setData([], [])

    @property
    def n_points(self) -> int:
        return len(self._t)

    # ── internals ─────────────────────────────────────────────────────────────────

    def _add_series(self, key: str) -> None:
        idx = len(self._series)
        self._series[key] = []
        pen = pg.mkPen(pg.intColor(idx, hues=9, values=2), width=2)
        curve = self._plot.plot(pen=pen, name=key)
        self._curves[key] = curve

        box = QCheckBox(key)
        box.setChecked(not _is_raw(key))
        box.toggled.connect(lambda _=None, k=key: self._apply_visibility(k))
        self._boxes[key] = box
        self._checks.insertWidget(self._checks.count() - 1, box)
        self._apply_visibility(key)

    def _apply_visibility(self, key: str) -> None:
        visible = self._boxes[key].isChecked()
        curve = self._curves[key]
        if visible:
            curve.setData(self._t, self._series[key])
        else:
            curve.setData([], [])

    def _redraw(self) -> None:
        for key, curve in self._curves.items():
            if self._boxes[key].isChecked():
                curve.setData(self._t, self._series[key])
