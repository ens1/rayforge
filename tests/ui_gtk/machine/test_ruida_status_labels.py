from unittest.mock import MagicMock

import pytest

from rayforge.machine.driver.driver import DeviceState, DeviceStatus
from rayforge.machine.driver.ruida.program_driver import (
    BOSS_LS2040_DA000400_STATUS_PROFILE,
    RUIDA_MACHINE_STATUS_PROFILE_KEY,
)
from rayforge.machine.driver.ruida.ruida_serial_driver import (
    RuidaSerialDriver,
)
from rayforge.machine.models.machine import Machine

pytestmark = pytest.mark.ui


def _display_machine(ui_context_initializer):
    driver_machine = Machine(ui_context_initializer)
    driver = RuidaSerialDriver(ui_context_initializer, driver_machine)
    machine = MagicMock()
    machine.driver = driver
    machine.device_state = DeviceState(status=DeviceStatus.IDLE)
    return machine, driver


def test_scoped_ruida_status_labels_describe_program_state(
    ui_context_initializer,
):
    from rayforge.ui_gtk.machine.machine_dropdown import (
        _get_status_text,
        _get_status_tooltip,
    )
    from rayforge.ui_gtk.machine.status_widget import MachineStatusWidget

    machine, driver = _display_machine(ui_context_initializer)
    driver.config[RUIDA_MACHINE_STATUS_PROFILE_KEY] = (
        BOSS_LS2040_DA000400_STATUS_PROFILE
    )

    expected = {
        DeviceStatus.IDLE: "Program idle",
        DeviceStatus.RUN: "Program running",
        DeviceStatus.HOLD: "Program paused",
    }
    for status, label in expected.items():
        machine.device_state = DeviceState(status=status)
        assert _get_status_text(machine) == label
        tooltip = _get_status_tooltip(machine)
        assert tooltip is not None
        assert "panel motion is not detected" in tooltip

    widget = MachineStatusWidget()
    widget.machine = machine
    widget._update_display(machine.device_state)
    assert widget.label.get_label() == "Program paused"
    tooltip = widget.get_tooltip_text()
    assert tooltip is not None
    assert "panel motion is not detected" in tooltip


def test_unscoped_ruida_status_labels_remain_generic(
    ui_context_initializer,
):
    from rayforge.ui_gtk.machine.machine_dropdown import (
        _get_status_text,
        _get_status_tooltip,
    )
    from rayforge.ui_gtk.machine.status_widget import MachineStatusWidget

    machine, driver = _display_machine(ui_context_initializer)
    machine.device_state = DeviceState(status=DeviceStatus.IDLE)

    assert _get_status_text(machine) == "Idle"
    assert _get_status_tooltip(machine) is None

    widget = MachineStatusWidget()
    widget.machine = machine
    widget._update_display(machine.device_state)
    assert widget.label.get_label() == "Idle"
    assert widget.get_tooltip_text() is None
    assert driver.get_device_status_label(DeviceStatus.RUN) == "Run"
