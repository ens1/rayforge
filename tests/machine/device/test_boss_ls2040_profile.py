import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rayforge.config import BUILTIN_DEVICES_DIR
from rayforge.machine.device.manager import DeviceProfileManager
from rayforge.machine.device.profile import export_machine_to_dir
from rayforge.machine.driver.ruida import RuidaSerialDriver, program_driver
from rayforge.machine.driver.ruida.program_driver import (
    BOSS_LS2040_DA000400_STATUS_PROFILE,
    RUIDA_MACHINE_STATUS_PROFILE_KEY,
)
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
    assert machine.driver_config == {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (BOSS_LS2040_DA000400_STATUS_PROFILE)
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
    assert machine.driver.config == machine.driver_config
    assert machine.driver.reports_device_status is True
    assert machine.driver.confirms_execution_completion is True
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
    assert restored.driver.config == restored.driver_config
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
    assert machine.driver.config == machine.driver_config
    assert machine.driver.confirms_execution_completion is True
    connect.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_boss_profile_validates_status_api_during_driver_setup(
    lite_context, task_mgr, monkeypatch
):
    class ControllerClientWithoutStatus:
        def __init__(self, transport):
            self.transport = transport

        def stop_process(self):
            pass

    api = SimpleNamespace(
        ControllerClient=ControllerClientWithoutStatus,
        SerialTransport=lambda device, *, baudrate: object(),
    )
    monkeypatch.setattr(
        program_driver.RuidaProgramDriver,
        "_status_semantics_validated",
        False,
    )
    monkeypatch.setattr(program_driver, "_load_ruida_re", lambda: api)
    manager = DeviceProfileManager([BUILTIN_DEVICES_DIR])
    manager.discover()
    profile = manager.get("Boss LS2040")

    assert profile is not None
    machine = profile.create_machine(lite_context)
    await _wait_for_tasks(task_mgr)

    machine.set_driver(
        RuidaSerialDriver,
        {
            "port": "/dev/cu.test-ruida",
            "baudrate": 115200,
            "job_profile": "proven",
        },
    )
    await _wait_for_tasks(task_mgr)

    driver = machine.driver
    assert isinstance(driver, RuidaSerialDriver)
    assert driver.config == machine.driver_config
    assert driver.state.error is not None
    assert driver.state.error.title == (
        "Installed ruida-re lacks required API: "
        "ControllerClient.read_machine_status"
    )
    assert driver._transport is None
