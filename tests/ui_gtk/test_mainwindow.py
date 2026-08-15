# flake8: noqa: E402
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

import pytest

# Platform-Specific Setup
if sys.platform.startswith("linux"):
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    if not os.environ.get("DISPLAY"):
        pytest.skip(
            "DISPLAY not set on Linux, skipping UI tests. Run with xvfb-run.",
            allow_module_level=True,
        )


# Gtk imports must happen AFTER the platform setup and display check.
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, GLib

from rayforge.machine.driver.driver import DeviceState, DeviceStatus
from rayforge.machine.driver.ruida import RuidaSerialDriver
from rayforge.machine.driver.ruida.program_driver import (
    BOSS_LS2040_DA000400_STATUS_PROFILE,
    RUIDA_MACHINE_STATUS_PROFILE_KEY,
)
from rayforge.machine.models.machine import Machine
from rayforge.machine.transport import TransportStatus
from rayforge.ui_gtk.mainwindow import MainWindow

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.ui


# Helper functions adapted for robust testing


def process_events_for_duration(duration_sec: float):
    """
    Processes all pending GTK events for a given duration without blocking.
    """
    end_time = time.monotonic() + duration_sec
    context = GLib.main_context_default()
    while time.monotonic() < end_time:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def wait_for_document_to_settle(window: MainWindow, timeout: int = 45) -> bool:
    """
    Waits for the 'document_settled' signal in a thread-safe manner.
    """
    settled_event = threading.Event()

    def on_settled(sender):
        logger.info("Received 'document_settled' signal.")
        settled_event.set()

    handler_id = window.doc_editor.document_settled.connect(on_settled)

    logger.info("Waiting for document to settle...")
    start_time = time.monotonic()

    while not settled_event.is_set():
        process_events_for_duration(0.1)
        if time.monotonic() - start_time > timeout:
            logger.error("Timeout waiting for document_settled signal.")
            window.doc_editor.document_settled.disconnect(handler_id)
            return False

    window.doc_editor.document_settled.disconnect(handler_id)
    return window.doc_editor.doc.has_result()


@pytest.fixture
def assets_path() -> Path:
    return Path(__file__).parent.parent


@pytest.fixture
def test_file_path(assets_path: Path) -> Path:
    path = assets_path / "image" / "png" / "color.png"
    assert path.exists()
    return path


@pytest.fixture
def app_and_window(ui_context_initializer, request):
    """Sets up the Adw.Application and MainWindow without blocking."""
    from rayforge.ui_gtk import sim3d

    sim3d.initialize()
    assert sim3d.initialized, "Canvas3D failed to initialize"

    win = None

    class TestApp(Adw.Application):
        def do_activate(self):
            nonlocal win
            win = MainWindow(application=self)
            win.set_default_size(1280, 800)
            self.win = win

    test_name = re.sub(r"[^a-z0-9-]", "-", request.node.name.lower())
    app_id = f"org.rayforge.rayforge.test.{test_name}"
    app = TestApp(application_id=app_id)
    app.register(None)
    app.activate()
    process_events_for_duration(0.5)

    assert hasattr(app, "win") and app.win is not None
    win = app.win
    win.present()
    process_events_for_duration(0.5)

    yield app, win

    # Teardown
    if win:
        win.doc_editor.cleanup()
        win.close()
        app.quit()
    process_events_for_duration(0.2)


@pytest.fixture
def window_with_machine(app_and_window, ui_context_initializer):
    _, win = app_and_window
    machine = Machine(ui_context_initializer)
    ui_context_initializer.machine_mgr.add_machine(machine)
    ui_context_initializer.config.set_machine(machine)
    process_events_for_duration(0.1)
    return win, machine


def test_manual_confirmation_gate_accepts_driver_or_command_state(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    other = Machine(machine.context)
    machine.context.machine_mgr.add_machine(other)

    assert win._manual_execution_confirmation_required(machine) is False

    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    assert win._manual_execution_confirmation_required(machine) is True
    assert win._manual_execution_confirmation_required(other) is False

    win.machine_cmd._execution_confirmation_machine_ids.clear()
    mocker.patch.object(
        type(machine.driver),
        "manual_execution_confirmation_required",
        new_callable=PropertyMock,
        return_value=True,
    )
    assert win._manual_execution_confirmation_required(machine) is True


def test_pending_confirmation_keeps_send_and_frame_actions_available(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=False
    )

    win._update_actions_and_ui()

    assert win.action_manager.get_action("machine-send").get_enabled()
    assert win.action_manager.get_action("machine-frame").get_enabled()
    expected = "Confirm that the controller is idle before reconnecting"
    assert win.toolbar.send_button.get_tooltip_text() == expected
    assert win.toolbar.frame_button.get_tooltip_text() == expected


def test_pending_confirmation_blocks_other_machine_actions(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    driver = MagicMock()
    driver.state.error = None
    driver.reports_device_status = False
    driver.manual_execution_confirmation_required = False
    mocker.patch.object(machine.controller, "driver", driver)
    machine.set_connection_status(TransportStatus.CONNECTED)
    mocker.patch.object(win.doc_editor.doc, "has_result", return_value=True)
    mocker.patch.object(machine, "can_frame", return_value=True)
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=False
    )

    win._update_actions_and_ui()

    assert win.action_manager.get_action("machine-home").get_enabled()
    assert win.action_manager.get_action("execute-macro").get_enabled()
    assert win.action_manager.get_action("machine-send").get_enabled()
    assert win.action_manager.get_action("machine-frame").get_enabled()

    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    win._update_actions_and_ui()

    assert not win.action_manager.get_action("machine-home").get_enabled()
    assert not win.action_manager.get_action("execute-macro").get_enabled()
    assert win.action_manager.get_action("machine-send").get_enabled()
    assert win.action_manager.get_action("machine-frame").get_enabled()


def test_unknown_status_blocks_status_reporting_driver(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    driver = MagicMock()
    driver.state.error = None
    driver.reports_device_status = True
    driver.manual_execution_confirmation_required = False
    mocker.patch.object(machine.controller, "driver", driver)
    machine.set_connection_status(TransportStatus.CONNECTED)
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=False
    )

    win._update_actions_and_ui()

    assert not win.action_manager.get_action("machine-home").get_enabled()
    assert not win.action_manager.get_action("execute-macro").get_enabled()


def test_unscoped_ruida_unknown_status_preserves_transfer_and_manual_gate(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    driver = RuidaSerialDriver(machine.context, machine)
    driver._status_semantics_validated = False
    mocker.patch.object(machine.controller, "driver", driver)
    machine.set_connection_status(TransportStatus.CONNECTED)
    machine.set_device_state(DeviceState(status=DeviceStatus.UNKNOWN))
    mocker.patch.object(win.doc_editor.doc, "has_result", return_value=True)
    mocker.patch.object(machine, "can_frame", return_value=True)
    mocker.patch.object(
        type(win.doc_editor.pipeline),
        "is_data_stale",
        new_callable=PropertyMock,
        return_value=False,
    )
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=False
    )

    win._update_actions_and_ui()

    assert driver.reports_device_status is False
    assert win.action_manager.get_action("machine-send").get_enabled()
    assert win.action_manager.get_action("machine-frame").get_enabled()

    driver._execution_unconfirmed = True
    win._update_actions_and_ui()

    assert win._manual_execution_confirmation_required(machine) is True
    assert win.action_manager.get_action("machine-send").get_enabled()
    assert win.toolbar.send_button.get_tooltip_text() == (
        "Confirm that the controller is idle before reconnecting"
    )


def test_boss_natural_completions_reenable_send_action(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    driver = RuidaSerialDriver(machine.context, machine)
    driver.config = {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (
            BOSS_LS2040_DA000400_STATUS_PROFILE
        ),
    }
    mocker.patch.object(machine.controller, "driver", driver)
    machine.set_connection_status(TransportStatus.CONNECTED)
    mocker.patch.object(win.doc_editor.doc, "has_result", return_value=True)
    mocker.patch.object(
        type(win.doc_editor.pipeline),
        "is_data_stale",
        new_callable=PropertyMock,
        return_value=False,
    )
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=False
    )

    for _ in range(2):
        machine.set_device_state(DeviceState(status=DeviceStatus.RUN))
        win._update_actions_and_ui()
        assert not win.action_manager.get_action("machine-send").get_enabled()

        machine.set_device_state(DeviceState(status=DeviceStatus.IDLE))
        win._update_actions_and_ui()
        assert win.action_manager.get_action("machine-send").get_enabled()
        assert win.machine_cmd.execution_confirmation_required(machine) is (
            False
        )


@pytest.mark.parametrize("response_id", ["cancel", "close"])
def test_manual_confirmation_dialog_cancel_is_safe(
    window_with_machine, mocker, response_id
):
    win, machine = window_with_machine
    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    dialog = MagicMock()
    dialog_cls = mocker.patch(
        "rayforge.ui_gtk.mainwindow.Adw.MessageDialog",
        return_value=dialog,
    )
    reconnect = mocker.patch.object(win, "_reconnect_after_confirmed_idle")

    assert win._confirm_idle_before_next_job(machine) is True

    dialog_cls.assert_called_once()
    dialog.add_response.assert_any_call("cancel", "Cancel")
    dialog.add_response.assert_any_call(
        "reconnect", "Controller Is Visibly Idle — Reconnect"
    )
    dialog.set_default_response.assert_called_once_with("cancel")
    dialog.set_close_response.assert_called_once_with("cancel")
    response_handler = dialog.connect.call_args.args[1]
    response_handler(dialog, response_id)

    dialog.destroy.assert_called_once_with()
    reconnect.assert_not_called()


def test_manual_confirmation_only_schedules_reconnect(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    dialog = MagicMock()
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.Adw.MessageDialog",
        return_value=dialog,
    )
    reconnect = mocker.patch.object(win, "_reconnect_after_confirmed_idle")
    send_job = mocker.patch.object(win.machine_cmd, "send_job")
    frame_job = mocker.patch.object(win.machine_cmd, "frame_job")

    assert win._confirm_idle_before_next_job(machine) is True
    response_handler = dialog.connect.call_args.args[1]
    response_handler(dialog, "reconnect")

    reconnect.assert_called_once_with(machine)
    send_job.assert_not_called()
    frame_job.assert_not_called()


@pytest.mark.parametrize("stale_driver", [False, True])
def test_manual_confirmation_rechecks_machine_and_driver(
    window_with_machine, mocker, stale_driver
):
    win, machine = window_with_machine
    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    dialog = MagicMock()
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.Adw.MessageDialog",
        return_value=dialog,
    )
    reconnect = mocker.patch.object(win, "_reconnect_after_confirmed_idle")

    assert win._confirm_idle_before_next_job(machine) is True
    response_handler = dialog.connect.call_args.args[1]
    if stale_driver:
        mocker.patch.object(machine.controller, "driver", MagicMock())
    else:
        other = Machine(machine.context)
        context = MagicMock()
        context.config.machine = other
        mocker.patch(
            "rayforge.ui_gtk.mainwindow.get_context", return_value=context
        )

    response_handler(dialog, "reconnect")

    reconnect.assert_not_called()


def test_manual_confirmation_stops_if_another_task_started(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    dialog = MagicMock()
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.Adw.MessageDialog",
        return_value=dialog,
    )
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=True
    )
    reconnect = mocker.patch.object(win, "_reconnect_after_confirmed_idle")

    assert win._confirm_idle_before_next_job(machine) is True
    response_handler = dialog.connect.call_args.args[1]
    response_handler(dialog, "reconnect")

    reconnect.assert_not_called()


def test_send_and_frame_stop_at_manual_confirmation(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    prompt = mocker.patch.object(
        win, "_confirm_idle_before_next_job", return_value=True
    )
    sanity_check = mocker.patch.object(win, "_run_sanity_check_and_proceed")
    send_job = mocker.patch.object(win.machine_cmd, "send_job")
    frame_job = mocker.patch.object(win.machine_cmd, "frame_job")

    win.on_send_clicked(None, None)
    win.on_frame_clicked(None, None)

    assert prompt.call_args_list == [
        mocker.call(machine),
        mocker.call(machine),
    ]
    sanity_check.assert_not_called()
    send_job.assert_not_called()
    frame_job.assert_not_called()


def test_bottom_panel_send_uses_guarded_window_action(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    prompt = mocker.patch.object(
        win, "_confirm_idle_before_next_job", return_value=True
    )
    direct_send = mocker.patch.object(win.machine_cmd, "run_send_job")
    win._update_actions_and_ui()

    assert (
        win.bottom_panel.jog_widget.send_btn.get_action_name()
        == "win.machine-send"
    )
    assert (
        win.bottom_panel.jog_widget.home_all_btn.get_action_name()
        == "win.machine-home"
    )
    win.bottom_panel.jog_widget.send_btn.emit("clicked")

    prompt.assert_called_once_with(machine)
    direct_send.assert_not_called()


def test_ruida_program_cancel_action_is_available(window_with_machine, mocker):
    win, machine = window_with_machine
    driver = MagicMock()
    driver.state.error = None
    driver.reports_device_status = False
    driver.supports_cancel = True
    driver.manual_execution_confirmation_required = True
    mocker.patch.object(machine.controller, "driver", driver)
    machine.set_connection_status(TransportStatus.CONNECTED)

    win._update_actions_and_ui()

    action = win.action_manager.get_action("machine-cancel")
    assert action.get_enabled()
    assert (
        win.bottom_panel.jog_widget.cancel_btn.get_action_name()
        == "win.machine-cancel"
    )


def test_status_driver_without_hold_does_not_enable_pause(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    driver = MagicMock()
    driver.state.error = None
    driver.reports_device_status = True
    driver.supports_hold = False
    driver.supports_cancel = True
    driver.manual_execution_confirmation_required = False
    mocker.patch.object(machine.controller, "driver", driver)
    machine.set_connection_status(TransportStatus.CONNECTED)
    machine.set_device_state(DeviceState(status=DeviceStatus.RUN))
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=False
    )

    win._update_actions_and_ui()

    assert not win.action_manager.get_action("machine-hold").get_enabled()
    assert win.action_manager.get_action("machine-cancel").get_enabled()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (DeviceStatus.IDLE, False),
        (DeviceStatus.UNKNOWN, False),
        (DeviceStatus.RUN, True),
        (DeviceStatus.HOLD, True),
        (DeviceStatus.CYCLE, True),
    ],
)
def test_status_driver_cancel_requires_active_program_state(
    window_with_machine, mocker, status, expected
):
    win, machine = window_with_machine
    driver = MagicMock()
    driver.state.error = None
    driver.reports_device_status = True
    driver.supports_hold = False
    driver.supports_cancel = True
    driver.manual_execution_confirmation_required = False
    mocker.patch.object(machine.controller, "driver", driver)
    machine.set_connection_status(TransportStatus.CONNECTED)
    machine.set_device_state(DeviceState(status=status))
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=False
    )

    win._update_actions_and_ui()

    assert (
        win.action_manager.get_action("machine-cancel").get_enabled()
        is expected
    )


def test_pending_cancel_disables_stop_action(window_with_machine, mocker):
    win, machine = window_with_machine
    driver = MagicMock()
    driver.state.error = None
    driver.reports_device_status = True
    driver.supports_hold = False
    driver.supports_cancel = True
    driver.manual_execution_confirmation_required = False
    mocker.patch.object(machine.controller, "driver", driver)
    machine.set_connection_status(TransportStatus.CONNECTED)
    machine.set_device_state(DeviceState(status=DeviceStatus.RUN))
    win.machine_cmd._cancel_pending_machine_ids.add(machine.id)
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=False
    )

    win._update_actions_and_ui()

    assert not win.action_manager.get_action("machine-cancel").get_enabled()


def test_transfer_driver_cancel_tracks_submission_or_confirmation(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    driver = MagicMock()
    driver.state.error = None
    driver.reports_device_status = False
    driver.supports_cancel = True
    driver.manual_execution_confirmation_required = False
    mocker.patch.object(machine.controller, "driver", driver)
    machine.set_connection_status(TransportStatus.CONNECTED)

    win._update_actions_and_ui()
    action = win.action_manager.get_action("machine-cancel")
    assert not action.get_enabled()

    win._machine_job_submission_pending = True
    win._machine_job_submission_machine_id = machine.id
    win._update_actions_and_ui()
    assert action.get_enabled()

    win._machine_job_submission_pending = False
    win._machine_job_submission_machine_id = None
    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    win._update_actions_and_ui()
    assert action.get_enabled()


def test_send_rechecks_confirmation_after_sanity_check(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    pending = {}
    mocker.patch.object(
        win,
        "_run_sanity_check_and_proceed",
        side_effect=lambda proceed: pending.setdefault("proceed", proceed),
    )
    prompt = mocker.patch.object(
        win, "_confirm_idle_before_next_job", return_value=False
    )
    run_job = mocker.patch.object(win, "_run_machine_job")

    win.on_send_clicked(None, None)
    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    prompt.return_value = True
    pending["proceed"]()

    assert prompt.call_args_list == [
        mocker.call(machine),
        mocker.call(machine),
    ]
    run_job.assert_not_called()


def test_pending_submission_disables_confirmation_actions(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    win._machine_job_submission_pending = True
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=False
    )

    win._update_actions_and_ui()

    assert not win.action_manager.get_action("machine-send").get_enabled()
    assert not win.action_manager.get_action("machine-frame").get_enabled()


def test_pending_submission_disables_motion_controls(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    driver = MagicMock()
    driver.state.error = None
    driver.reports_device_status = False
    driver.manual_execution_confirmation_required = False
    mocker.patch.object(machine.controller, "driver", driver)
    machine.set_connection_status(TransportStatus.CONNECTED)
    machine.single_axis_homing_enabled = True
    mocker.patch.object(machine, "can_jog", return_value=True)
    mocker.patch.object(machine, "can_home", return_value=True)
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.has_tasks", return_value=False
    )

    win._machine_job_submission_pending = False
    win._update_actions_and_ui()
    assert win.bottom_panel.jog_widget.east_btn.get_sensitive()
    assert win.bottom_panel.jog_widget.home_x_btn.get_sensitive()

    win._machine_job_submission_pending = True
    win._update_actions_and_ui()
    assert not win.bottom_panel.jog_widget.east_btn.get_sensitive()
    assert not win.bottom_panel.jog_widget.home_x_btn.get_sensitive()


def test_machine_job_submission_rejects_duplicate(window_with_machine, mocker):
    win, _machine = window_with_machine
    future = MagicMock()
    submit = mocker.patch(
        "rayforge.ui_gtk.mainwindow.asyncio.run_coroutine_threadsafe",
        return_value=future,
    )
    mocker.patch.object(win, "_update_actions_and_ui")

    async def _job():
        return None

    first = _job()
    second = _job()
    try:
        win._run_machine_job(_machine, first)
        win._run_machine_job(_machine, second)

        submit.assert_called_once()
        assert submit.call_args.args[0] is first
        future.add_done_callback.assert_called_once_with(
            win._on_job_future_done
        )
        assert second.cr_frame is None
    finally:
        first.close()


@pytest.mark.asyncio
async def test_confirmed_idle_task_only_reconnects(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    scheduled = {}

    def _capture(coroutine, **kwargs):
        scheduled["coroutine"] = coroutine
        scheduled.update(kwargs)

    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.add_coroutine",
        side_effect=_capture,
    )
    reconnect = mocker.patch.object(
        win.machine_cmd,
        "reconnect_after_confirmed_idle",
        new_callable=mocker.AsyncMock,
    )
    send_job = mocker.patch.object(win.machine_cmd, "send_job")
    frame_job = mocker.patch.object(win.machine_cmd, "frame_job")

    win._reconnect_after_confirmed_idle(machine)
    ctx = MagicMock()
    await scheduled["coroutine"](ctx)

    reconnect.assert_awaited_once_with(machine)
    send_job.assert_not_called()
    frame_job.assert_not_called()
    assert scheduled["key"] == (
        machine.id,
        "reconnect-after-confirmed-idle",
    )


@pytest.mark.asyncio
async def test_confirmed_idle_task_rechecks_active_machine(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    scheduled = {}

    def _capture(coroutine, **kwargs):
        scheduled["coroutine"] = coroutine

    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.add_coroutine",
        side_effect=_capture,
    )
    reconnect = mocker.patch.object(
        win.machine_cmd,
        "reconnect_after_confirmed_idle",
        new_callable=mocker.AsyncMock,
    )
    context = MagicMock()
    context.config.machine = Machine(machine.context)
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.get_context", return_value=context
    )

    win._reconnect_after_confirmed_idle(machine)
    with pytest.raises(
        RuntimeError, match="active machine changed before reconnecting"
    ):
        await scheduled["coroutine"](MagicMock())

    reconnect.assert_not_awaited()


@pytest.mark.parametrize("failed", [False, True])
def test_confirmed_idle_reconnect_notification(
    window_with_machine, mocker, failed
):
    win, machine = window_with_machine
    task = MagicMock()
    task.get_status.return_value = "failed" if failed else "completed"
    if failed:
        task.result.side_effect = RuntimeError("reopen failed")
        win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    notification = mocker.patch.object(win, "_on_editor_notification")
    update_actions = mocker.patch.object(win, "_update_actions_and_ui")
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.schedule_on_main_thread",
        side_effect=lambda callback: callback(),
    )

    win._on_confirmed_idle_reconnect_done(task)

    message = notification.call_args.kwargs["message"]
    if failed:
        assert message == "Reconnect failed: reopen failed"
        assert notification.call_args.kwargs["persistent"] is True
        assert win.machine_cmd.execution_completion_unknown is True
    else:
        assert message == (
            "Controller reconnected. Click Send or Frame again when ready."
        )
        assert "persistent" not in notification.call_args.kwargs
    update_actions.assert_called_once_with()


def test_confirmed_idle_reconnect_cancellation_retains_gate(
    window_with_machine, mocker
):
    win, machine = window_with_machine
    win.machine_cmd._execution_confirmation_machine_ids.add(machine.id)
    task = MagicMock()
    task.get_status.return_value = "canceled"
    notification = mocker.patch.object(win, "_on_editor_notification")
    update_actions = mocker.patch.object(win, "_update_actions_and_ui")
    mocker.patch(
        "rayforge.ui_gtk.mainwindow.task_mgr.schedule_on_main_thread",
        side_effect=lambda callback: callback(),
    )

    win._on_confirmed_idle_reconnect_done(task)

    task.result.assert_not_called()
    assert notification.call_args.kwargs == {
        "message": (
            "Reconnect canceled. Confirm the controller is idle before "
            "trying Send or Frame again."
        ),
        "persistent": True,
    }
    assert win.machine_cmd.execution_confirmation_required(machine) is True
    update_actions.assert_called_once_with()
