"""
Tests for the MachineController class.

This module tests the MachineController which handles:
- Driver lifecycle management (connect/disconnect/shutdown)
- Command execution (jog, home, run_raw, etc.)
- Signal emissions for state changes

The MachineController is the logic layer that owns and manages the driver.
"""

import pytest

from rayforge.machine.models.controller import MachineController
from rayforge.machine.models.machine import Machine
from rayforge.machine.transport import TransportStatus
from rayforge.shared.tasker import task_mgr


@pytest.mark.usefixtures("lite_context")
class TestMachineController:
    """Test suite for the MachineController class."""

    def test_controller_initialization(self, lite_context):
        """Test that MachineController can be initialized."""
        machine = Machine(lite_context)
        lite_context.machine_mgr.add_machine(machine)
        controller = MachineController(
            machine, lite_context, task_mgr.schedule_on_main_thread
        )
        assert controller is not None
        assert controller.machine == machine
        assert controller.context == lite_context
        assert controller.driver is not None

    def test_controller_driver_property(self, lite_context):
        """Test that the controller has a driver property."""
        machine = Machine(lite_context)
        lite_context.machine_mgr.add_machine(machine)
        controller = machine.controller
        assert controller.driver is not None

    def test_controller_signals_exist(self, lite_context):
        """Test that controller has all required signals."""
        machine = Machine(lite_context)
        lite_context.machine_mgr.add_machine(machine)
        controller = machine.controller
        assert hasattr(controller, "connection_status_changed")
        assert hasattr(controller, "state_changed")
        assert hasattr(controller, "job_finished")
        assert hasattr(controller, "command_status_changed")
        assert hasattr(controller, "wcs_updated")

    @pytest.mark.asyncio
    async def test_connecting_driver_is_not_started_twice(
        self, machine, mocker
    ):
        controller = machine.controller
        driver = controller.driver

        async def start_background_connection():
            driver.connection_status_changed.send(
                driver,
                status=TransportStatus.CONNECTING,
                message=None,
            )

        connect_mock = mocker.patch.object(
            driver,
            "connect",
            side_effect=start_background_connection,
        )

        await controller.connect()
        await controller.connect()

        connect_mock.assert_awaited_once_with()

    def test_driver_config_update_does_not_trigger_rebuild(
        self, sync_machine, mocker
    ):
        controller = sync_machine.controller
        driver = controller.driver
        driver.config = {"rx_buffer_size": 256}
        add_coroutine = mocker.patch.object(task_mgr, "add_coroutine")

        driver.config_changed.send(driver)
        sync_machine.changed.send(sync_machine)

        assert sync_machine.driver_config == {"rx_buffer_size": 256}
        assert controller._last_driver_config == {"rx_buffer_size": 256}
        assert controller._active_driver_config == {"rx_buffer_size": 256}
        rebuild_key = (sync_machine.id, "rebuild-driver-on-change")
        assert all(
            call.kwargs.get("key") != rebuild_key
            for call in add_coroutine.call_args_list
        )
