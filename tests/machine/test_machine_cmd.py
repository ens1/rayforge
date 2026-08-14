import asyncio
import json
from contextlib import nullcontext
from functools import partial
from unittest.mock import MagicMock, PropertyMock

import pytest
import pytest_asyncio
from raygeo.ops import Ops
from raygeo.ops.axis import Axis

from rayforge.core.config import ConfigManager
from rayforge.machine.cmd import MachineCmd
from rayforge.machine.driver.driver import (
    DeviceConnectionError,
    ExecutionCompletionUnknownError,
)
from rayforge.machine.driver.ruida.ruida_encoder import (
    RuidaEncoder,
    RuidaEncodingError,
)
from rayforge.machine.models.machine import Machine
from rayforge.pipeline.artifact import JobArtifact
from rayforge.shared.tasker.manager import TaskManager


@pytest_asyncio.fixture(autouse=True)
async def task_mgr(monkeypatch):
    """
    Provides a test-isolated TaskManager, configured to bridge its main-thread
    callbacks to the asyncio event loop. This instance replaces the global
    task_mgr for the duration of the tests in this module.
    """
    main_loop = asyncio.get_running_loop()

    def asyncio_scheduler(callback, *args, **kwargs):
        # Use call_soon_threadsafe because the TaskManager runs on a separate
        # thread and schedules callbacks onto this main loop.
        main_loop.call_soon_threadsafe(partial(callback, *args, **kwargs))

    # Instantiate the TaskManager with our custom scheduler
    tm = TaskManager(main_thread_scheduler=asyncio_scheduler)

    # Patch the global singleton where it is imported and used by other modules
    monkeypatch.setattr("rayforge.machine.models.machine.task_mgr", tm)

    yield tm

    # Properly shut down the manager and its thread after tests are done
    tm.shutdown()


@pytest.fixture(autouse=True)
def test_config_manager(tmp_path, monkeypatch):
    """Provides a test-isolated ConfigManager."""
    mock_config_mgr = MagicMock(spec=ConfigManager)
    yield mock_config_mgr


@pytest.fixture
def machine(lite_context):
    """Provides a default Machine instance with NoDeviceDriver."""
    m = Machine(lite_context)
    lite_context.machine_mgr.add_machine(m)
    return m


@pytest.fixture
def machine_cmd(doc_editor):
    """Provides a MachineCmd instance."""
    return MachineCmd(doc_editor)


@pytest.fixture
def simple_ops():
    """Creates a simple Ops object with a few commands."""
    ops = Ops()
    ops.move_to(10, 10, 0)
    ops.line_to(20, 10, 0)
    ops.line_to(20, 20, 0)
    return ops


@pytest.fixture
def job_artifact(simple_ops, machine):
    """Creates a JobArtifact containing simple_ops with encoded G-code."""
    encoded = machine.driver.get_encoder().encode(simple_ops, machine, None)
    return JobArtifact(
        ops=simple_ops,
        distance=simple_ops.distance(),
        generation_id=1,
        encoded_output=encoded,
    )


async def wait_for_tasks_to_finish(task_mgr: TaskManager):
    """
    Asynchronously waits for the task manager to become idle.
    """
    # Yield to the loop to ensure pending callbacks (like adding tasks) run
    # first
    await asyncio.sleep(0)

    # Use the now-correct, thread-safe wait_until_settled in a non-blocking way
    if await asyncio.to_thread(task_mgr.wait_until_settled, 2000):
        return
    pytest.fail("Task manager did not become idle in time.")


class TestMachineCmdJobMonitoring:
    """Test suite for the job monitoring orchestration in MachineCmd."""

    @pytest.mark.asyncio
    async def test_send_job_granular_progress(
        self, machine_cmd, machine, simple_ops, job_artifact, mocker
    ):
        """
        Tests the full monitoring flow for a driver that reports
        granular progress.
        """
        assert machine.driver.reports_granular_progress is True

        # --- Arrange ---
        job_started_spy = MagicMock()
        progress_updated_spy = MagicMock()
        job_finished_spy = MagicMock()

        machine_cmd.job_started.connect(job_started_spy)
        machine.job_finished.connect(job_finished_spy)

        # --- Act ---
        # Setup progress spy AFTER the job starts and monitor is created
        def on_job_started(sender):
            monitor = machine_cmd._current_monitor
            assert monitor is not None
            monitor.progress_updated.connect(progress_updated_spy)

        job_started_spy.side_effect = on_job_started

        await machine_cmd._run_send_action(
            job_artifact, machine, on_progress=lambda metrics: None
        )

        # Yield control to the event loop to allow signal handlers
        # (like cleanup_monitor) that were scheduled with `call_soon`
        # to run before we proceed with assertions.
        await asyncio.sleep(0)

        # --- Assert ---
        # 1. Verify job lifecycle signals
        job_started_spy.assert_called_once()
        job_finished_spy.assert_called_once()
        assert machine_cmd._current_monitor is None  # Check cleanup

        # 2. Verify granular progress updates
        # The JobMonitor sends updates for all commands, including those with
        # zero distance (like MoveToCommand).
        # We must calculate the expected number of calls by counting all cmds.
        expected_call_count = len(simple_ops)
        assert progress_updated_spy.call_count == expected_call_count

    @pytest.mark.asyncio
    async def test_send_job_non_granular_progress(
        self, machine_cmd, machine, simple_ops, job_artifact, mocker
    ):
        """
        Tests the monitoring flow for a driver that does not report
        granular progress.
        """
        # --- Arrange ---
        mocker.patch.object(
            type(machine),
            "reports_granular_progress",
            new_callable=PropertyMock,
            return_value=False,
        )
        assert not machine.reports_granular_progress

        async def mock_run_and_finish(*args, **kwargs):
            # Simulate work and signal finish
            await asyncio.sleep(0)
            machine.driver.job_finished.send(machine.driver)

        run_mock = mocker.patch.object(
            machine.driver, "run", side_effect=mock_run_and_finish
        )

        # Use an asyncio.Event for robust synchronization
        job_finished_event = asyncio.Event()
        job_finished_spy = MagicMock(
            side_effect=lambda *a, **kw: job_finished_event.set()
        )

        machine.job_finished.connect(job_finished_spy)

        # --- Act ---
        await machine_cmd._run_send_action(
            job_artifact, machine, on_progress=lambda metrics: None
        )

        # Explicitly wait for the job_finished signal
        # handler to run. This eliminates the race condition.
        await asyncio.wait_for(job_finished_event.wait(), timeout=1)

        # --- Assert ---
        # 1. Verify driver was called correctly
        run_mock.assert_called_once()
        assert run_mock.call_args.kwargs["on_command_done"] is None

        # 2. Verify job lifecycle signals fired
        job_finished_spy.assert_called_once()

        # 3. Verify cleanup happened
        assert machine_cmd._current_monitor is None

    @pytest.mark.asyncio
    async def test_transfer_only_job_does_not_claim_execution_completion(
        self, machine_cmd, machine, job_artifact, mocker
    ):
        mocker.patch.object(
            type(machine.driver),
            "confirms_execution_completion",
            new_callable=PropertyMock,
            return_value=False,
        )
        run_mock = mocker.patch.object(
            machine.driver,
            "run",
            new_callable=mocker.AsyncMock,
        )
        hours_mock = mocker.patch.object(machine, "add_machine_hours")
        completion_mock = mocker.patch(
            "rayforge.machine.cmd.JobMonitor.mark_as_complete"
        )
        progress_mock = MagicMock()
        transferred_mock = MagicMock()
        machine_cmd.job_transferred.connect(transferred_mock)

        await machine_cmd._run_send_action(
            job_artifact, machine, on_progress=progress_mock
        )
        await asyncio.sleep(0)

        run_mock.assert_awaited_once()
        assert run_mock.await_args.kwargs["on_command_done"] is None
        hours_mock.assert_not_called()
        completion_mock.assert_not_called()
        progress_mock.assert_not_called()
        transferred_mock.assert_called_once_with(machine_cmd, machine=machine)
        assert machine_cmd.execution_completion_unknown is True
        assert machine_cmd._current_monitor is None

    def test_driver_defaults_to_no_manual_execution_confirmation(
        self, machine
    ):
        assert machine.driver.reports_device_status is True
        assert machine.driver.manual_execution_confirmation_required is False

    @pytest.mark.asyncio
    async def test_confirmed_idle_reconnect_clears_unknown_after_success(
        self, machine_cmd, machine, mocker
    ):
        machine_cmd._execution_confirmation_machine_ids.add(machine.id)
        reconnect_mock = mocker.patch.object(
            machine,
            "reconnect",
            new_callable=mocker.AsyncMock,
        )
        run_mock = mocker.patch.object(
            machine.driver,
            "run",
            new_callable=mocker.AsyncMock,
        )

        await machine_cmd.reconnect_after_confirmed_idle(machine)

        reconnect_mock.assert_awaited_once_with()
        run_mock.assert_not_awaited()
        assert machine_cmd.execution_completion_unknown is False

    @pytest.mark.asyncio
    async def test_failed_confirmed_idle_reconnect_retains_unknown(
        self, machine_cmd, machine, mocker
    ):
        mocker.patch.object(
            type(machine.driver),
            "manual_execution_confirmation_required",
            new_callable=PropertyMock,
            return_value=True,
        )
        reconnect_mock = mocker.patch.object(
            machine,
            "reconnect",
            new_callable=mocker.AsyncMock,
            side_effect=DeviceConnectionError("reopen failed"),
        )
        run_mock = mocker.patch.object(
            machine.driver,
            "run",
            new_callable=mocker.AsyncMock,
        )

        with pytest.raises(DeviceConnectionError, match="reopen failed"):
            await machine_cmd.reconnect_after_confirmed_idle(machine)

        reconnect_mock.assert_awaited_once_with()
        run_mock.assert_not_awaited()
        assert machine_cmd.execution_completion_unknown is True
        assert machine_cmd.execution_confirmation_required(machine) is True

    @pytest.mark.asyncio
    async def test_confirmed_idle_reconnect_only_clears_target_machine(
        self, machine_cmd, machine, lite_context, mocker
    ):
        other = Machine(lite_context)
        lite_context.machine_mgr.add_machine(other)
        machine_cmd._execution_confirmation_machine_ids.update(
            (machine.id, other.id)
        )
        reconnect_mock = mocker.patch.object(
            machine,
            "reconnect",
            new_callable=mocker.AsyncMock,
        )

        await machine_cmd.reconnect_after_confirmed_idle(machine)

        reconnect_mock.assert_awaited_once_with()
        assert machine_cmd.execution_confirmation_required(machine) is False
        assert machine_cmd.execution_confirmation_required(other) is True
        assert machine_cmd.execution_completion_unknown is True

    @pytest.mark.asyncio
    async def test_duplicate_confirmed_idle_reconnect_is_rejected(
        self, machine_cmd, machine, mocker
    ):
        machine_cmd._execution_confirmation_machine_ids.add(machine.id)
        reconnect_started = asyncio.Event()
        reconnect_release = asyncio.Event()

        async def reconnect():
            reconnect_started.set()
            await reconnect_release.wait()

        reconnect_mock = mocker.patch.object(
            machine,
            "reconnect",
            side_effect=reconnect,
        )
        first = asyncio.create_task(
            machine_cmd.reconnect_after_confirmed_idle(machine)
        )
        await reconnect_started.wait()

        with pytest.raises(DeviceConnectionError, match="already in progress"):
            await machine_cmd.reconnect_after_confirmed_idle(machine)

        assert machine_cmd.execution_confirmation_required(machine) is True
        reconnect_release.set()
        await first

        reconnect_mock.assert_awaited_once_with()
        assert machine_cmd.execution_confirmation_required(machine) is False

    @pytest.mark.asyncio
    async def test_machine_latch_blocks_rebuilt_driver_until_reconnect(
        self, machine_cmd, machine, job_artifact, mocker
    ):
        pipeline = machine_cmd._editor.pipeline
        handle = MagicMock()
        mocker.patch.object(
            pipeline,
            "generate_job_artifact_async",
            new_callable=mocker.AsyncMock,
            return_value=handle,
        )
        mocker.patch.object(
            pipeline.artifact_store,
            "checkout_handle",
            return_value=nullcontext(job_artifact),
        )
        action = mocker.AsyncMock()
        machine_cmd._execution_confirmation_machine_ids.add(machine.id)

        with pytest.raises(
            ExecutionCompletionUnknownError,
            match="may still be executing",
        ):
            await machine_cmd._start_job(machine, "send", action)

        action.assert_not_awaited()
        reconnect_mock = mocker.patch.object(
            machine,
            "reconnect",
            new_callable=mocker.AsyncMock,
        )
        await machine_cmd.reconnect_after_confirmed_idle(machine)
        await machine_cmd._start_job(machine, "send", action)

        reconnect_mock.assert_awaited_once_with()
        action.assert_awaited_once()

    def test_confirmation_query_does_not_leak_between_machines(
        self, machine_cmd, machine, lite_context
    ):
        other = Machine(lite_context)
        lite_context.machine_mgr.add_machine(other)
        machine_cmd._execution_confirmation_machine_ids.add(machine.id)

        assert machine_cmd.execution_confirmation_required(machine) is True
        assert machine_cmd.execution_confirmation_required(other) is False

    @pytest.mark.asyncio
    async def test_ambiguous_start_failure_sets_confirmation_latch(
        self, machine_cmd, machine, job_artifact, mocker
    ):
        pipeline = machine_cmd._editor.pipeline
        handle = MagicMock()
        mocker.patch.object(
            pipeline,
            "generate_job_artifact_async",
            new_callable=mocker.AsyncMock,
            return_value=handle,
        )
        mocker.patch.object(
            pipeline.artifact_store,
            "checkout_handle",
            return_value=nullcontext(job_artifact),
        )
        action = mocker.AsyncMock(
            side_effect=ExecutionCompletionUnknownError("execution unknown")
        )

        with pytest.raises(
            ExecutionCompletionUnknownError,
            match="execution unknown",
        ):
            await machine_cmd._start_job(machine, "send", action)

        action.assert_awaited_once()
        assert machine_cmd.execution_completion_unknown is True

    @pytest.mark.asyncio
    async def test_ambiguous_cancellation_sets_confirmation_latch(
        self, machine_cmd, machine, job_artifact, mocker
    ):
        pipeline = machine_cmd._editor.pipeline
        handle = MagicMock()
        mocker.patch.object(
            pipeline,
            "generate_job_artifact_async",
            new_callable=mocker.AsyncMock,
            return_value=handle,
        )
        mocker.patch.object(
            pipeline.artifact_store,
            "checkout_handle",
            return_value=nullcontext(job_artifact),
        )
        mocker.patch.object(
            type(machine.driver),
            "manual_execution_confirmation_required",
            new_callable=PropertyMock,
            side_effect=(False, True),
        )
        action = mocker.AsyncMock(side_effect=asyncio.CancelledError)

        with pytest.raises(asyncio.CancelledError):
            await machine_cmd._start_job(machine, "send", action)

        action.assert_awaited_once()
        assert machine_cmd.execution_completion_unknown is True

    @pytest.mark.asyncio
    async def test_cancelled_transfer_uses_captured_driver_latch(
        self, machine_cmd, machine, job_artifact, mocker
    ):
        old_driver = machine.driver
        run_started = asyncio.Event()

        async def run_until_cancelled(*args, **kwargs):
            run_started.set()
            await asyncio.Event().wait()

        mocker.patch.object(
            type(old_driver),
            "manual_execution_confirmation_required",
            new_callable=PropertyMock,
            return_value=True,
        )
        mocker.patch.object(
            old_driver,
            "run",
            side_effect=run_until_cancelled,
        )
        replacement = MagicMock()
        replacement.manual_execution_confirmation_required = False

        task = asyncio.create_task(
            machine_cmd._run_send_action(job_artifact, machine, None)
        )
        await run_started.wait()
        machine.controller.driver = replacement
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            machine.controller.driver = old_driver

        assert machine_cmd.execution_confirmation_required(machine) is True


class TestMachineCmdFrame:
    @staticmethod
    def _configure_frame(machine, corner_pause=0.0):
        head = machine.get_default_laser_head()
        assert head is not None
        head.set_frame_power(0.1)
        head.set_frame_repeat_count(1)
        head.set_frame_corner_pause(corner_pause)
        return head

    @pytest.mark.asyncio
    async def test_ruida_frame_has_explicit_vector_process_contract(
        self, machine_cmd, machine, job_artifact, mocker
    ):
        head = self._configure_frame(machine)
        mocker.patch(
            "rayforge.machine.cmd._create_driver_encoder",
            return_value=RuidaEncoder(),
        )
        execute_mock = mocker.patch.object(
            machine_cmd,
            "_execute_monitored_job",
            new_callable=mocker.AsyncMock,
        )

        await machine_cmd._run_frame_action(
            job_artifact, machine, on_progress=None
        )

        execute_mock.assert_awaited_once()
        frame_ops = execute_mock.await_args.args[0]
        encoded = execute_mock.await_args.kwargs["encoded"]
        starts = [
            index
            for index in range(frame_ops.len())
            if frame_ops.command_type(index).name == "PROCESS_START"
        ]
        assert len(starts) == 1
        start = starts[0]
        assert frame_ops.process_uid(start) == "rayforge.frame"
        metadata = json.loads(frame_ops.process_params(start))
        assert metadata["schema"] == "rayforge.process"
        assert metadata["version"] == 1
        assert metadata["kind"] == "vector"
        assert metadata["head_uid"] == head.uid
        assert metadata["power"]["value"] == pytest.approx(0.1)
        assert encoded.payload

    @pytest.mark.asyncio
    async def test_ruida_frame_with_corner_dwell_fails_closed(
        self, machine_cmd, machine, job_artifact, mocker
    ):
        self._configure_frame(machine, corner_pause=0.25)
        mocker.patch(
            "rayforge.machine.cmd._create_driver_encoder",
            return_value=RuidaEncoder(),
        )
        execute_mock = mocker.patch.object(
            machine_cmd,
            "_execute_monitored_job",
            new_callable=mocker.AsyncMock,
        )

        with pytest.raises(RuidaEncodingError, match="DWELL"):
            await machine_cmd._run_frame_action(
                job_artifact, machine, on_progress=None
            )

        execute_mock.assert_not_awaited()


class TestMachineCmdJog:
    """Test suite for the jogging functionality in MachineCmd."""

    @pytest.mark.asyncio
    async def test_jog_with_deltas(
        self, machine_cmd, machine, mocker, task_mgr
    ):
        """
        Test jogging using the dictionary of deltas.
        """
        # --- Arrange ---
        # Use a full AsyncMock replacement for machine.jog to avoid
        # dependency on Machine logic or driver state, and ensure correct
        # awaitable return for TaskManager.
        jog_mock = mocker.patch.object(
            machine, "jog", new_callable=mocker.AsyncMock
        )

        # --- Act ---
        # Jog X axis by 10mm at 1000mm/min
        deltas = {Axis.X: 10.0}
        machine_cmd.jog(machine, deltas, 1000)

        await wait_for_tasks_to_finish(task_mgr)

        # --- Assert ---
        # MachineCmd.jog should delegate to Machine.jog
        jog_mock.assert_called_once_with(deltas, 1000)

    @pytest.mark.asyncio
    async def test_jog_multi_axis(
        self, machine_cmd, machine, mocker, task_mgr
    ):
        """
        Test jogging multiple axes.
        """
        # --- Arrange ---
        jog_mock = mocker.patch.object(
            machine, "jog", new_callable=mocker.AsyncMock
        )

        # --- Act ---
        deltas = {Axis.X: 5.0, Axis.Y: -5.0}
        machine_cmd.jog(machine, deltas, 1500)

        await wait_for_tasks_to_finish(task_mgr)

        # --- Assert ---
        # Verify call arguments to Machine.jog
        jog_mock.assert_called_once_with(deltas, 1500)


class TestMachineCmdLaserPower:
    """Test suite for manual laser power commands."""

    @pytest.mark.asyncio
    async def test_set_focus_power_uses_explicit_machine(
        self, machine_cmd, machine, mocker, task_mgr
    ):
        head = machine.get_default_head()
        set_focus_power_mock = mocker.patch.object(
            machine, "set_focus_power", new_callable=mocker.AsyncMock
        )

        machine_cmd.set_focus_power(head, 0.25, machine)

        await wait_for_tasks_to_finish(task_mgr)

        set_focus_power_mock.assert_called_once_with(head, 0.25)

    @pytest.mark.asyncio
    async def test_set_power_uses_explicit_machine(
        self, machine_cmd, machine, mocker, task_mgr
    ):
        head = machine.get_default_head()
        set_power_mock = mocker.patch.object(
            machine, "set_power", new_callable=mocker.AsyncMock
        )

        machine_cmd.set_power(head, 0.5, machine)

        await wait_for_tasks_to_finish(task_mgr)

        set_power_mock.assert_called_once_with(head, 0.5)
