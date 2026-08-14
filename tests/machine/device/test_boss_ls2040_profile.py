import asyncio
from unittest.mock import AsyncMock

import pytest

from rayforge.config import BUILTIN_DEVICES_DIR
from rayforge.machine.device.manager import DeviceProfileManager
from rayforge.machine.device.profile import export_machine_to_dir
from rayforge.machine.driver.ruida import RuidaSerialDriver
from rayforge.machine.models.axis import AxisDirection
from rayforge.machine.models.laser import LaserHead, LaserType
from rayforge.machine.models.machine import Origin
from rayforge.machine.transport import TransportStatus
from rayforge.shared.tasker.manager import TaskManager
from rayforge.shared.units.system import UnitSystem


async def _wait_for_tasks(task_mgr: TaskManager):
    if await asyncio.to_thread(task_mgr.wait_until_settled, 2000):
        return
    pytest.fail("Task manager did not become idle in time.")


def _assert_boss_machine(machine, port=""):
    assert machine.name == "Boss LS2040"
    assert machine.driver_name == "RuidaSerialDriver"
    assert machine.driver_args == {
        "port": port,
        "baudrate": 115200,
        "job_profile": "proven",
    }
    assert machine.auto_connect is True
    assert machine.axis_extents == pytest.approx(
        (991.1080322265625, 599.947998046875)
    )
    assert machine.origin == Origin.TOP_RIGHT
    assert machine.max_travel_speed == 10000
    assert machine.max_cut_speed == 6000
    assert machine.home_on_start is False
    assert machine.single_axis_homing_enabled is False
    assert machine.rotary_enabled_default is False
    assert machine.rotary_modules == {}
    assert machine.supports_arcs is False
    assert machine.supports_curves is False
    assert machine.unit_system == UnitSystem.METRIC

    assert len(machine.axes.configs) == 3
    for axis_config in machine.axes.configs:
        assert axis_config.direction == AxisDirection.NORMAL

    assert len(machine.heads) == 1
    head = machine.heads[0]
    assert isinstance(head, LaserHead)
    assert head.name == "CO2 Laser"
    assert head.tool_number == 0
    assert head.laser_type == LaserType.CO2
    assert head.frame_power_percent == 0.0
    assert head.focus_power_percent == 0.0
    assert head.frame_speed == 6000
    assert head.frame_repeat_count == 1
    assert head.frame_corner_pause == 0.0


@pytest.mark.asyncio
async def test_boss_profile_discovery_roundtrip_is_fail_closed(
    tmp_path, lite_context, task_mgr, monkeypatch
):
    connect = AsyncMock()
    monkeypatch.setattr(
        RuidaSerialDriver,
        "_connect_implementation",
        connect,
    )
    manager = DeviceProfileManager([BUILTIN_DEVICES_DIR])

    profiles = manager.discover()
    profile = manager.get("Boss LS2040")

    assert profile is not None
    assert profile in profiles
    assert profile.source_dir == BUILTIN_DEVICES_DIR / "boss-ls2040"

    machine = profile.create_machine(lite_context)
    await _wait_for_tasks(task_mgr)
    _assert_boss_machine(machine)

    assert isinstance(machine.driver, RuidaSerialDriver)
    assert machine.driver.state.error is not None
    assert machine.driver.state.error.title == (
        "Serial port must be configured."
    )
    assert machine.driver.resource_uri is None
    assert machine.driver._transport is None
    assert machine.connection_status == TransportStatus.DISCONNECTED
    connect.assert_not_awaited()

    exported = export_machine_to_dir(machine, tmp_path / "exported")
    assert exported.machine_config.auto_connect is True
    assert exported.machine_config.driver_args == machine.driver_args

    restored = exported.create_machine(lite_context)
    await _wait_for_tasks(task_mgr)
    _assert_boss_machine(restored)
    assert restored.driver.state.error is not None
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_boss_profile_connects_when_serial_port_is_configured(
    lite_context, task_mgr, monkeypatch
):
    connect = AsyncMock()
    monkeypatch.setattr(
        RuidaSerialDriver,
        "_connect_implementation",
        connect,
    )
    manager = DeviceProfileManager([BUILTIN_DEVICES_DIR])
    manager.discover()
    profile = manager.get("Boss LS2040")

    assert profile is not None
    machine = profile.create_machine(lite_context)
    await _wait_for_tasks(task_mgr)
    connect.assert_not_awaited()

    machine.set_driver(
        RuidaSerialDriver,
        {
            "port": "/dev/cu.test-ruida",
            "baudrate": 115200,
            "job_profile": "proven",
        },
    )
    await _wait_for_tasks(task_mgr)

    _assert_boss_machine(machine, port="/dev/cu.test-ruida")
    connect.assert_awaited_once_with()
