"""Connection and simulation panels.

After the v2 redesign these are the only config panels left here: the Hall-wired
Source/Sense/Sample/Acquisition panels were replaced by the generic ChannelsPanel
(gui/channels_tab.py) and RoutingPanel (gui/routing_tab.py).

Both panels expose typed @property accessors so MainWindow can read values
without knowing widget internals.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLineEdit,
)


class ConnectionPanel(QGroupBox):
    simulation_toggled = Signal(bool)

    def __init__(self) -> None:
        super().__init__("M81 Connection")
        layout = QFormLayout(self)

        self._ip = QLineEdit("192.168.0.1")
        self._ip.setPlaceholderText("e.g. 192.168.0.100")

        self._sim = QCheckBox("Simulation mode (no hardware)")
        self._sim.toggled.connect(self.simulation_toggled)

        layout.addRow("IP address:", self._ip)
        layout.addRow(self._sim)

    @property
    def ip_address(self) -> str:
        return self._ip.text().strip()

    @property
    def simulated(self) -> bool:
        return self._sim.isChecked()

    def set_ip_address(self, ip: str) -> None:
        self._ip.setText(ip)

    def set_simulated(self, simulated: bool) -> None:
        self._sim.setChecked(simulated)


class MockPanel(QGroupBox):
    """Simulation-only controls: magnetic field and temperature."""

    field_changed       = Signal(float)
    temperature_changed = Signal(float)

    def __init__(self) -> None:
        super().__init__("Simulation parameters")
        layout = QFormLayout(self)

        self._field = QDoubleSpinBox()
        self._field.setRange(-14.0, 14.0)
        self._field.setDecimals(3)
        self._field.setValue(0.0)
        self._field.setSingleStep(0.1)
        self._field.setSuffix(" T")
        self._field.setToolTip("Applied magnetic field (mock backend only).")

        self._temp = QDoubleSpinBox()
        self._temp.setRange(1.0, 400.0)
        self._temp.setDecimals(1)
        self._temp.setValue(300.0)
        self._temp.setSingleStep(5.0)
        self._temp.setSuffix(" K")
        self._temp.setToolTip("Sample temperature (mock backend only).")

        layout.addRow("B field:", self._field)
        layout.addRow("Temperature:", self._temp)

        self._field.valueChanged.connect(self.field_changed)
        self._temp.valueChanged.connect(self.temperature_changed)

    @property
    def field_T(self) -> float:
        return self._field.value()

    @property
    def temperature_K(self) -> float:
        return self._temp.value()
