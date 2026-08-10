import pytest

from rayforge.machine.driver import GrblSerialDriver
from rayforge.machine.models.laser import LaserHead
from rayforge.machine.models.machine import Machine
from rayforge.machine.models.spindle import SpindleHead
from rayforge.ui_gtk.machine.head_preferences_page import HeadPreferencesPage


def _machine(context, driver_name: str, tool_number: int = 0) -> Machine:
    machine = Machine(context)
    machine.driver_name = driver_name
    machine.heads[0].tool_number = tool_number
    return machine


@pytest.mark.ui
@pytest.mark.parametrize(
    "driver_name",
    ("RuidaSerialDriver", "RuidaUdpProgramDriver", "RuidaDriver"),
)
def test_new_ruida_laser_uses_free_second_channel(
    ui_context_initializer,
    driver_name,
):
    machine = _machine(ui_context_initializer, driver_name)
    page = HeadPreferencesPage(machine)
    added = LaserHead()

    page.head_list_editor._add_head(added, "New Laser")

    assert machine.heads[-1] is added
    assert added.tool_number == 1


@pytest.mark.ui
def test_new_ruida_laser_uses_first_free_channel(ui_context_initializer):
    machine = _machine(
        ui_context_initializer,
        "RuidaSerialDriver",
        tool_number=1,
    )
    page = HeadPreferencesPage(machine)
    added = LaserHead()

    page.head_list_editor._add_head(added, "New Laser")

    assert added.tool_number == 0


@pytest.mark.ui
def test_non_ruida_and_spindle_defaults_are_unchanged(ui_context_initializer):
    grbl = _machine(ui_context_initializer, GrblSerialDriver.__name__)
    grbl_page = HeadPreferencesPage(grbl)
    grbl_laser = LaserHead()
    grbl_page.head_list_editor._add_head(grbl_laser, "New Laser")

    ruida = _machine(ui_context_initializer, "RuidaSerialDriver")
    ruida_page = HeadPreferencesPage(ruida)
    spindle = SpindleHead()
    ruida_page.head_list_editor._add_head(spindle, "New Spindle")

    assert grbl_laser.tool_number == 0
    assert spindle.tool_number == 0


@pytest.mark.ui
def test_tool_number_subtitle_tracks_ruida_driver(ui_context_initializer):
    machine = _machine(ui_context_initializer, GrblSerialDriver.__name__)
    page = HeadPreferencesPage(machine)
    row = page.laser_widget.tool_number_row

    assert row.get_subtitle() == "G-code tool number (e.g., T0, T1)"

    machine.driver_name = "RuidaSerialDriver"
    machine.changed.send(machine)

    assert row.get_subtitle() == (
        "Ruida mapping: tool 0 is laser channel 1; tool 1 is laser channel 2"
    )

    machine.driver_name = GrblSerialDriver.__name__
    machine.changed.send(machine)

    assert row.get_subtitle() == "G-code tool number (e.g., T0, T1)"
