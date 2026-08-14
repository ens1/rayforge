import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rayforge.machine.driver import drivers, get_driver_cls
from rayforge.machine.driver.driver import (
    DeviceConnectionError,
    DeviceStatus,
    ExecutionCompletionUnknownError,
)
from rayforge.machine.driver.ruida import program_driver
from rayforge.machine.driver.ruida.program_driver import RuidaProgramDriver
from rayforge.machine.driver.ruida.ruida_serial_driver import (
    RuidaSerialDriver,
)
from rayforge.machine.driver.ruida.ruida_udp_program_driver import (
    RuidaUdpProgramDriver,
)
from rayforge.machine.models.laser import Laser
from rayforge.machine.models.machine import Machine
from rayforge.machine.transport import TransportStatus
from rayforge.pipeline.encoder.base import EncodedOutput, MachineCodeOpMap


class FakeSerialTransport:
    def __init__(self, device, *, baudrate):
        self.device = device
        self.baudrate = baudrate


class FakeUdpTransport:
    def __init__(self, host, *, controller_port, local_port):
        self.host = host
        self.controller_port = controller_port
        self.local_port = local_port


class MemorySerialTransport:
    kind = "serial"

    def __init__(self):
        self.is_open = False
        self.sent = []

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def send(self, data):
        self.sent.append(data)

    def receive(self, timeout):
        del timeout

    def drain(self, limit=256):
        del limit
        return ()


class FakeCodec:
    def __init__(self, payload=b"complete-rd"):
        self.payload = payload
        self.program = SimpleNamespace(
            records=[
                SimpleNamespace(
                    name="move_absolute",
                    values={"x_mm": 1.0, "y_mm": 1.0},
                ),
                SimpleNamespace(
                    name="cut_absolute",
                    values={"x_mm": 2.0, "y_mm": 2.0},
                ),
            ],
            issues=[],
            payload=payload,
        )
        self.decode_calls = []
        self.encode_calls = []

    def decode(self, payload, *, container):
        self.decode_calls.append((payload, container))
        return self.program

    def encode(self, program, *, container, checksum_policy):
        self.encode_calls.append((program, container, checksum_policy))
        return self.payload


class FakeControllerClient:
    def __init__(self, transport):
        self.transport = transport
        self.is_open = False
        self.is_ready = False
        self.open_probes = []
        self.close_calls = 0
        self.open_error: Exception | None = None
        self.close_error: Exception | None = None
        self.close_before_error = False
        self.send_error: Exception | None = None
        self.sent_programs = []
        self.events = []
        self.open_started: threading.Event | None = None
        self.open_release: threading.Event | None = None
        self.close_started: threading.Event | None = None
        self.close_release: threading.Event | None = None
        self.send_started: threading.Event | None = None
        self.send_release: threading.Event | None = None
        self.receipt = SimpleNamespace(
            completed_packets=2,
            retries=0,
            packets=(b"one", b"two"),
        )

    def open(self, *, probe):
        self.open_probes.append(probe)
        if self.open_started is not None:
            self.open_started.set()
        if self.open_release is not None:
            self.open_release.wait()
        if self.open_error is not None:
            raise self.open_error
        self.is_open = True
        self.is_ready = True

    def close(self):
        self.close_calls += 1
        if self.close_started is not None:
            self.close_started.set()
        if self.close_release is not None:
            self.close_release.wait()
        if self.close_error is not None:
            if self.close_before_error:
                self.is_open = False
                self.is_ready = False
            raise self.close_error
        self.is_open = False
        self.is_ready = False

    def send_job(self, program):
        self.events.append("send-started")
        if self.send_started is not None:
            self.send_started.set()
        if self.send_release is not None:
            self.send_release.wait()
        if self.send_error is not None:
            raise self.send_error
        self.sent_programs.append(program)
        self.events.append("send-complete")
        return self.receipt


@pytest.fixture
def fake_api(monkeypatch):
    codec = FakeCodec()
    clients = []
    api = SimpleNamespace(
        RuidaCodec=lambda *, context: codec,
        SerialTransport=FakeSerialTransport,
        UdpTransport=FakeUdpTransport,
        clients=clients,
        next_open_error=None,
    )

    def create_client(transport):
        client = FakeControllerClient(transport)
        client.open_error = api.next_open_error
        clients.append(client)
        return client

    api.ControllerClient = create_client
    monkeypatch.setattr(program_driver, "_load_ruida_re", lambda: api)
    return api, codec


@pytest.fixture
def driver_objects():
    manager = SimpleNamespace(machines={})
    context = SimpleNamespace(machine_mgr=manager)
    machine = SimpleNamespace(
        id="test-machine",
        driver_args={},
        heads=[],
        axis_extents=(200.0, 150.0),
    )
    return context, machine


def make_output(payload=b"complete-rd", warnings=()):
    return EncodedOutput(
        text="Ruida program",
        op_map=MachineCodeOpMap(
            op_to_machine_code={2: [1], 0: [0]},
            machine_code_to_op={0: 0, 1: 2},
        ),
        payload=payload,
        warnings=warnings,
    )


def client_for(driver: RuidaProgramDriver) -> FakeControllerClient:
    client = driver._client
    assert isinstance(client, FakeControllerClient)
    return client


async def wait_for_tasks(task_mgr):
    settled = await asyncio.to_thread(task_mgr.wait_until_settled, 2000)
    assert settled


def test_program_drivers_are_registered():
    assert get_driver_cls("RuidaSerialDriver") is RuidaSerialDriver
    assert get_driver_cls("RuidaUdpProgramDriver") is RuidaUdpProgramDriver
    assert get_driver_cls("RuidaDriver") is RuidaUdpProgramDriver
    assert (
        len([driver for driver in drivers if driver is RuidaUdpProgramDriver])
        == 1
    )


def test_program_setup_rejects_ambiguous_laser_tool_mapping(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    machine.heads = [Laser(), Laser()]
    driver = RuidaSerialDriver(context, machine)

    driver.setup(port="/dev/cu.ruida", baudrate=115200)

    assert driver.state.error is not None
    assert driver.state.error.title == (
        "Ruida laser head tool numbers must be unique: "
        "tool 0=Ruida channel 1 and tool 1=Ruida channel 2"
    )
    assert driver._transport is None


@pytest.mark.asyncio
async def test_accepts_complete_rd_from_real_compiler(
    monkeypatch, driver_objects
):
    ruida_re = pytest.importorskip("ruida_re")
    plan = ruida_re.JobPlan(
        layers=(
            ruida_re.LayerPlan(
                index=0,
                kind="vector",
                speed_mm_s=10.0,
                min_power_percent=10.0,
                max_power_percent=10.0,
                events=(
                    ruida_re.TravelTo(1.0, 1.0),
                    ruida_re.MarkTo(2.0, 2.0),
                ),
            ),
        )
    )
    payload = ruida_re.RuidaJobCompiler().compile(plan).encode_rd()
    monkeypatch.setattr(program_driver, "_load_ruida_re", lambda: ruida_re)
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    transport = MemorySerialTransport()
    client = ruida_re.ControllerClient(transport)
    client.open(probe=False)
    driver._transport = transport
    driver._client = client

    await driver.run(
        make_output(payload),
        MagicMock(),
        MagicMock(),
    )

    assert b"".join(transport.sent) == payload


@pytest.mark.asyncio
async def test_serial_connects_without_probe(fake_api, driver_objects):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    statuses = []
    states = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.state_changed.connect(
        lambda sender, state: states.append(state.status),
        weak=False,
    )

    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)

    transport = driver._transport
    assert isinstance(transport, FakeSerialTransport)
    assert transport.device == "/dev/cu.ruida"
    assert transport.baudrate == 115200
    assert client.open_probes == [False]
    assert driver.resource_uri == "serial:///dev/cu.ruida"
    assert statuses == [
        TransportStatus.CONNECTING,
        TransportStatus.CONNECTED,
    ]
    assert states == [DeviceStatus.UNKNOWN, DeviceStatus.UNKNOWN]

    await driver.cleanup()


@pytest.mark.asyncio
async def test_execution_latch_clears_only_after_successful_reopen(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    setup_args = {"port": "/dev/cu.ruida", "baudrate": 115200}
    driver.setup(**setup_args)
    await driver.connect()
    assert driver.reports_device_status is False
    assert driver.manual_execution_confirmation_required is False

    await driver.run(make_output(), MagicMock(), MagicMock())
    assert driver._execution_unconfirmed is True
    assert driver.manual_execution_confirmation_required is True

    await driver.cleanup()
    assert driver._execution_unconfirmed is True
    assert driver.manual_execution_confirmation_required is True

    driver.setup(**setup_args)
    client = client_for(driver)
    client.open_error = OSError("open failed")

    with pytest.raises(DeviceConnectionError, match="open failed"):
        await driver.connect()

    assert driver._execution_unconfirmed is True
    assert driver.manual_execution_confirmation_required is True
    assert client.sent_programs == []

    client.open_error = None
    await driver.connect()

    assert driver._execution_unconfirmed is False
    assert driver.manual_execution_confirmation_required is False
    assert client.sent_programs == []
    await driver.cleanup()


@pytest.mark.asyncio
async def test_cancelled_blocking_open_closes_after_worker_settles(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    driver._execution_unconfirmed = True
    client = client_for(driver)
    open_started = threading.Event()
    open_release = threading.Event()
    client.open_started = open_started
    client.open_release = open_release

    task = asyncio.create_task(driver.connect())
    assert await asyncio.to_thread(open_started.wait, 1.0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    open_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.close_calls == 1
    assert client.is_open is False
    assert client.is_ready is False
    assert client.sent_programs == []
    assert driver._client is client
    assert driver.manual_execution_confirmation_required is True
    assert driver.did_setup is True
    await driver.cleanup()


@pytest.mark.asyncio
async def test_cancelled_open_preserves_handle_when_close_fails(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    driver._execution_unconfirmed = True
    client = client_for(driver)
    open_started = threading.Event()
    open_release = threading.Event()
    client.open_started = open_started
    client.open_release = open_release
    client.close_error = RuntimeError("close failed")

    task = asyncio.create_task(driver.connect())
    assert await asyncio.to_thread(open_started.wait, 1.0)
    task.cancel()
    open_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.close_calls == 1
    assert client.is_open is True
    assert client.is_ready is True
    assert client.sent_programs == []
    assert driver._client is client
    assert driver.manual_execution_confirmation_required is True
    assert driver.did_setup is True

    client.close_error = None
    await driver.cleanup()
    assert client.close_calls == 2
    assert client.is_open is False
    assert driver._client is None


@pytest.mark.asyncio
async def test_cancelled_blocking_close_finalizes_after_worker_settles(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    driver._execution_unconfirmed = True
    client = client_for(driver)
    close_started = threading.Event()
    close_release = threading.Event()
    client.close_started = close_started
    client.close_release = close_release

    task = asyncio.create_task(driver.cleanup())
    assert await asyncio.to_thread(close_started.wait, 1.0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.close_calls == 1
    assert client.is_open is False
    assert client.is_ready is False
    assert client.sent_programs == []
    assert driver._client is None
    assert driver._transport is None
    assert driver.resource_uri is None
    assert driver.manual_execution_confirmation_required is True
    assert driver.did_setup is False


@pytest.mark.asyncio
async def test_cancelled_close_preserves_cancellation_after_close_error(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    driver._execution_unconfirmed = True
    client = client_for(driver)
    close_started = threading.Event()
    close_release = threading.Event()
    client.close_started = close_started
    client.close_release = close_release
    client.close_before_error = True
    client.close_error = RuntimeError("closed with error")

    task = asyncio.create_task(driver.cleanup())
    assert await asyncio.to_thread(close_started.wait, 1.0)
    task.cancel()
    close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.close_calls == 1
    assert client.is_open is False
    assert client.is_ready is False
    assert client.sent_programs == []
    assert driver._client is None
    assert driver._transport is None
    assert driver.resource_uri is None
    assert driver.manual_execution_confirmation_required is True
    assert driver.did_setup is False


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_connect", (False, True))
async def test_machine_reconnect_opens_fresh_session_without_resending(
    fake_api,
    lite_context,
    task_mgr,
    auto_connect,
):
    api, codec = fake_api
    machine = Machine(lite_context)
    lite_context.machine_mgr.add_machine(machine)
    controller = machine.controller
    machine.driver_name = "RuidaSerialDriver"
    machine.driver_args = {
        "port": "/dev/cu.ruida",
        "baudrate": 115200,
    }
    machine.auto_connect = auto_connect

    try:
        await machine.reconnect()
        await wait_for_tasks(task_mgr)
        old_driver = machine.driver
        assert isinstance(old_driver, RuidaSerialDriver)
        old_client = client_for(old_driver)

        await old_driver.run(make_output(), MagicMock(), MagicMock())
        with pytest.raises(ExecutionCompletionUnknownError):
            await old_driver.run(make_output(), MagicMock(), MagicMock())

        await machine.reconnect()
        await wait_for_tasks(task_mgr)
        new_driver = machine.driver
        assert isinstance(new_driver, RuidaSerialDriver)
        assert new_driver is not old_driver
        new_client = client_for(new_driver)

        assert len(api.clients) == 2
        assert old_client.close_calls == 1
        assert new_client.open_probes == [False]
        assert old_client.sent_programs == [codec.program]
        assert new_client.sent_programs == []
        assert new_driver.manual_execution_confirmation_required is False

        await new_driver.run(make_output(), MagicMock(), MagicMock())
        assert new_client.sent_programs == [codec.program]
    finally:
        await controller.shutdown()


@pytest.mark.asyncio
async def test_failed_machine_reconnect_does_not_send_or_replay_job(
    fake_api,
    lite_context,
    task_mgr,
):
    api, codec = fake_api
    machine = Machine(lite_context)
    lite_context.machine_mgr.add_machine(machine)
    controller = machine.controller
    machine.driver_name = "RuidaSerialDriver"
    machine.driver_args = {
        "port": "/dev/cu.ruida",
        "baudrate": 115200,
    }
    machine.auto_connect = False

    try:
        await machine.reconnect()
        await wait_for_tasks(task_mgr)
        old_driver = machine.driver
        assert isinstance(old_driver, RuidaSerialDriver)
        old_client = client_for(old_driver)
        await old_driver.run(make_output(), MagicMock(), MagicMock())

        api.next_open_error = OSError("reopen failed")
        with pytest.raises(DeviceConnectionError, match="reopen failed"):
            await machine.reconnect()
        await wait_for_tasks(task_mgr)

        failed_driver = machine.driver
        assert isinstance(failed_driver, RuidaSerialDriver)
        failed_client = client_for(failed_driver)
        assert old_client.close_calls == 1
        assert old_client.sent_programs == [codec.program]
        assert failed_client.open_probes == [False]
        assert failed_client.sent_programs == []

        with pytest.raises(DeviceConnectionError, match="not connected"):
            await failed_driver.run(make_output(), MagicMock(), MagicMock())
        assert failed_client.sent_programs == []
    finally:
        await controller.shutdown()


@pytest.mark.asyncio
async def test_machine_reconnect_preserves_old_driver_when_close_fails(
    fake_api,
    lite_context,
    task_mgr,
):
    api, codec = fake_api
    machine = Machine(lite_context)
    lite_context.machine_mgr.add_machine(machine)
    controller = machine.controller
    machine.driver_name = "RuidaSerialDriver"
    machine.driver_args = {
        "port": "/dev/cu.ruida",
        "baudrate": 115200,
    }
    machine.auto_connect = False

    try:
        await machine.reconnect()
        await wait_for_tasks(task_mgr)
        old_driver = machine.driver
        assert isinstance(old_driver, RuidaSerialDriver)
        old_client = client_for(old_driver)
        await old_driver.run(make_output(), MagicMock(), MagicMock())
        old_client.close_error = OSError("port close failed")

        with pytest.raises(DeviceConnectionError, match="port close failed"):
            await machine.reconnect()

        candidate_client = api.clients[1]
        assert machine.driver is old_driver
        assert old_driver._client is old_client
        assert old_driver.resource_uri == "serial:///dev/cu.ruida"
        assert old_driver.manual_execution_confirmation_required is True
        assert old_client.is_open is True
        assert old_client.is_ready is True
        assert old_client.close_calls == 1
        assert old_client.sent_programs == [codec.program]
        assert candidate_client.open_probes == []
        assert candidate_client.sent_programs == []

        old_client.close_error = None
        await machine.reconnect()
        await wait_for_tasks(task_mgr)

        new_driver = machine.driver
        assert isinstance(new_driver, RuidaSerialDriver)
        assert new_driver is not old_driver
        new_client = client_for(new_driver)
        assert old_client.close_calls == 2
        assert new_client.open_probes == [False]
        assert new_client.sent_programs == []
    finally:
        await controller.shutdown()


@pytest.mark.asyncio
async def test_overlapping_rebuild_skips_stale_driver_open(
    fake_api,
    lite_context,
    task_mgr,
):
    api, _ = fake_api
    machine = Machine(lite_context)
    lite_context.machine_mgr.add_machine(machine)
    controller = machine.controller
    machine.driver_name = "RuidaSerialDriver"
    machine.driver_args = {
        "port": "/dev/cu.ruida-a",
        "baudrate": 115200,
    }
    machine.auto_connect = False

    try:
        await machine.reconnect()
        await wait_for_tasks(task_mgr)
        old_driver = machine.driver
        assert isinstance(old_driver, RuidaSerialDriver)
        old_client = client_for(old_driver)
        close_started = threading.Event()
        close_release = threading.Event()
        old_client.close_started = close_started
        old_client.close_release = close_release

        first = asyncio.create_task(machine.reconnect())
        assert await asyncio.to_thread(close_started.wait, 1.0)
        machine.driver_args = {
            "port": "/dev/cu.ruida-b",
            "baudrate": 115200,
        }
        second = asyncio.create_task(machine.reconnect())
        await asyncio.sleep(0)
        assert not second.done()

        close_release.set()
        with pytest.raises(
            DeviceConnectionError,
            match="configuration changed",
        ):
            await first
        await second
        await wait_for_tasks(task_mgr)

        stale_client = api.clients[1]
        current_driver = machine.driver
        assert isinstance(current_driver, RuidaSerialDriver)
        current_client = client_for(current_driver)
        assert old_client.close_calls == 1
        assert stale_client.open_probes == []
        assert stale_client.close_calls == 0
        assert current_client.open_probes == [False]
        assert current_client.close_calls == 0
        assert all(client.sent_programs == [] for client in api.clients)
        transport = current_driver._transport
        assert isinstance(transport, FakeSerialTransport)
        assert transport.device == "/dev/cu.ruida-b"
        assert machine.driver_args["port"] == "/dev/cu.ruida-b"
    finally:
        await controller.shutdown()


@pytest.mark.asyncio
async def test_config_change_rebuilds_after_stale_cleanup(
    fake_api,
    lite_context,
    mocker,
    task_mgr,
):
    api, _ = fake_api
    machine = Machine(lite_context)
    lite_context.machine_mgr.add_machine(machine)
    controller = machine.controller
    machine.driver_name = "RuidaSerialDriver"
    machine.driver_args = {
        "port": "/dev/cu.ruida",
        "baudrate": 115200,
    }
    machine.driver_config = {"firmware_version": "a"}
    machine.auto_connect = False
    close_release = threading.Event()
    lifecycle_tasks: list[asyncio.Task] = []

    try:
        await machine.reconnect()
        await wait_for_tasks(task_mgr)
        old_driver = machine.driver
        assert isinstance(old_driver, RuidaSerialDriver)
        old_client = client_for(old_driver)
        close_started = threading.Event()
        old_client.close_started = close_started
        old_client.close_release = close_release
        add_coroutine = mocker.patch.object(task_mgr, "add_coroutine")

        first = asyncio.create_task(controller.rebuild_driver(connect=False))
        lifecycle_tasks.append(first)
        assert await asyncio.to_thread(close_started.wait, 1.0)
        latest_config = {"firmware_version": "b"}
        machine.driver_config = latest_config
        machine.changed.send(machine)
        rebuild_key = (machine.id, "rebuild-driver-on-change")
        rebuild_calls = [
            call
            for call in add_coroutine.call_args_list
            if call.kwargs.get("key") == rebuild_key
        ]
        assert len(rebuild_calls) == 1
        scheduled_rebuild = rebuild_calls[0].args[0]
        second = asyncio.create_task(scheduled_rebuild())
        lifecycle_tasks.append(second)
        close_release.set()

        await asyncio.gather(first, second)

        stale_client = api.clients[1]
        current_driver = machine.driver
        assert isinstance(current_driver, RuidaSerialDriver)
        current_client = client_for(current_driver)
        assert old_client.close_calls == 1
        assert stale_client.open_probes == []
        assert stale_client.close_calls == 0
        assert current_client.open_probes == []
        assert current_driver.config == latest_config
        assert controller._active_driver_config == latest_config
        assert all(client.sent_programs == [] for client in api.clients)
    finally:
        close_release.set()
        for task in lifecycle_tasks:
            if not task.done():
                task.cancel()
        if lifecycle_tasks:
            await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
        await controller.shutdown()


@pytest.mark.asyncio
async def test_overlapping_same_config_reconnects_coalesce(
    fake_api,
    lite_context,
    task_mgr,
):
    api, _ = fake_api
    machine = Machine(lite_context)
    lite_context.machine_mgr.add_machine(machine)
    controller = machine.controller
    machine.driver_name = "RuidaSerialDriver"
    machine.driver_args = {
        "port": "/dev/cu.ruida",
        "baudrate": 115200,
    }
    machine.auto_connect = False

    try:
        await machine.reconnect()
        await wait_for_tasks(task_mgr)
        old_driver = machine.driver
        assert isinstance(old_driver, RuidaSerialDriver)
        old_client = client_for(old_driver)
        close_started = threading.Event()
        close_release = threading.Event()
        old_client.close_started = close_started
        old_client.close_release = close_release

        first = asyncio.create_task(machine.reconnect())
        assert await asyncio.to_thread(close_started.wait, 1.0)
        second = asyncio.create_task(machine.reconnect())
        await asyncio.sleep(0)
        assert not second.done()

        close_release.set()
        await asyncio.gather(first, second)
        await wait_for_tasks(task_mgr)

        assert len(api.clients) == 2
        assert old_client.close_calls == 1
        new_driver = machine.driver
        assert isinstance(new_driver, RuidaSerialDriver)
        new_client = client_for(new_driver)
        assert new_client.open_probes == [False]
        assert new_client.sent_programs == []
    finally:
        await controller.shutdown()


@pytest.mark.asyncio
async def test_disconnect_and_reconnect_share_lifecycle_lock(
    fake_api,
    lite_context,
    task_mgr,
):
    api, _ = fake_api
    machine = Machine(lite_context)
    lite_context.machine_mgr.add_machine(machine)
    controller = machine.controller
    machine.driver_name = "RuidaSerialDriver"
    machine.driver_args = {
        "port": "/dev/cu.ruida",
        "baudrate": 115200,
    }
    machine.auto_connect = False

    try:
        await machine.reconnect()
        await wait_for_tasks(task_mgr)
        old_driver = machine.driver
        assert isinstance(old_driver, RuidaSerialDriver)
        old_client = client_for(old_driver)
        close_started = threading.Event()
        close_release = threading.Event()
        old_client.close_started = close_started
        old_client.close_release = close_release

        disconnect = asyncio.create_task(machine.disconnect())
        assert await asyncio.to_thread(close_started.wait, 1.0)
        reconnect = asyncio.create_task(machine.reconnect())
        await asyncio.sleep(0)
        assert not reconnect.done()

        close_release.set()
        await asyncio.gather(disconnect, reconnect)
        await wait_for_tasks(task_mgr)

        assert len(api.clients) == 3
        assert old_client.close_calls == 1
        disconnected_client = api.clients[1]
        current_driver = machine.driver
        assert isinstance(current_driver, RuidaSerialDriver)
        current_client = client_for(current_driver)
        assert disconnected_client.open_probes == []
        assert disconnected_client.close_calls == 0
        assert current_client.open_probes == [False]
        assert current_client.close_calls == 0
        assert all(client.sent_programs == [] for client in api.clients)
    finally:
        await controller.shutdown()


@pytest.mark.asyncio
async def test_udp_connects_with_probe(fake_api, driver_objects):
    context, machine = driver_objects
    driver = RuidaUdpProgramDriver(context, machine)

    driver.setup(host="192.0.2.10", port=50200, local_port=40200)
    await driver.connect()
    client = client_for(driver)

    transport = driver._transport
    assert isinstance(transport, FakeUdpTransport)
    assert transport.host == "192.0.2.10"
    assert transport.controller_port == 50200
    assert transport.local_port == 40200
    assert client.open_probes == [True]
    assert driver.resource_uri == ("udp://192.0.2.10:50200?local_port=40200")

    await driver.cleanup()


@pytest.mark.asyncio
async def test_run_validates_and_transfers_before_callbacks(
    fake_api, driver_objects
):
    _, codec = fake_api
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    events = client.events

    async def callback(op_index):
        events.append(f"callback-{op_index}")
        await asyncio.sleep(0)

    def on_finished(sender):
        events.append("job-finished")

    driver.job_finished.connect(on_finished, weak=False)
    output = make_output()

    await driver.run(output, MagicMock(), MagicMock(), callback)

    assert codec.decode_calls == [(b"complete-rd", "rd")]
    assert codec.encode_calls == [(codec.program, "rd", "recompute")]
    assert client.sent_programs == [codec.program]
    assert driver.manual_execution_confirmation_required is True
    assert events == [
        "send-started",
        "send-complete",
        "callback-0",
        "callback-2",
    ]
    assert driver.confirms_execution_completion is False

    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="may still be executing",
    ):
        await driver.run(output, MagicMock(), MagicMock())

    await driver.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("driver_cls", "setup_args"),
    (
        (RuidaSerialDriver, {"port": "/dev/cu.ruida", "baudrate": 115200}),
        (
            RuidaUdpProgramDriver,
            {"host": "192.0.2.10", "port": 50200, "local_port": 40200},
        ),
    ),
)
async def test_out_of_bounds_payload_fails_before_transport_open_or_write(
    fake_api,
    driver_objects,
    driver_cls,
    setup_args,
):
    _, codec = fake_api
    codec.program.records = [
        SimpleNamespace(
            name="move_absolute",
            values={"x_mm": 199.0, "y_mm": 149.0},
        ),
        SimpleNamespace(
            name="cut_relative",
            values={"dx_mm": 1.000001, "dy_mm": 1.0},
        ),
    ]
    context, machine = driver_objects
    driver = driver_cls(context, machine)
    driver.setup(**setup_args)
    client = client_for(driver)

    with pytest.raises(
        DeviceConnectionError,
        match=r"X coordinate 200\.000001 mm.*bounds 0\.\.200 mm",
    ):
        await driver.run(make_output(), MagicMock(), MagicMock())

    assert client.open_probes == []
    assert client.sent_programs == []
    await driver.cleanup()


@pytest.mark.asyncio
async def test_out_of_bounds_payload_does_not_write_connected_transport(
    fake_api,
    driver_objects,
):
    _, codec = fake_api
    codec.program.records = [
        SimpleNamespace(
            name="move_absolute",
            values={"x_mm": 0.0, "y_mm": 0.0},
        ),
        SimpleNamespace(
            name="cut_absolute",
            values={"x_mm": 200.0, "y_mm": 150.001},
        ),
    ]
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)

    with pytest.raises(DeviceConnectionError, match="Y coordinate 150.001"):
        await driver.run(make_output(), MagicMock(), MagicMock())

    assert client.sent_programs == []
    assert driver._execution_unconfirmed is False
    await driver.cleanup()


@pytest.mark.asyncio
async def test_pretransfer_bounds_allow_exact_machine_edges(
    fake_api,
    driver_objects,
):
    _, codec = fake_api
    codec.program.records = [
        SimpleNamespace(
            name="move_absolute",
            values={"x_mm": 0.0, "y_mm": 0.0},
        ),
        SimpleNamespace(
            name="cut_absolute",
            values={"x_mm": 200.0, "y_mm": 150.0},
        ),
    ]
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)

    await driver.run(make_output(), MagicMock(), MagicMock())

    assert client.sent_programs == [codec.program]
    await driver.cleanup()


@pytest.mark.asyncio
async def test_run_logs_encoder_warnings_before_transfer(
    fake_api, driver_objects, caplog
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    output = make_output(
        warnings=("controller owns rapid speed",),
    )

    with caplog.at_level(logging.WARNING, logger=program_driver.__name__):
        await driver.run(output, MagicMock(), MagicMock())

    assert "Ruida encoder warning: controller owns rapid speed" in (
        caplog.text
    )
    await driver.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, b""])
async def test_run_rejects_missing_payload(fake_api, driver_objects, payload):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    output = make_output(payload)

    with pytest.raises(DeviceConnectionError, match="no Ruida .rd"):
        await driver.run(output, MagicMock(), MagicMock())

    assert client.sent_programs == []
    await driver.cleanup()


@pytest.mark.asyncio
async def test_run_rejects_decoder_issues(fake_api, driver_objects):
    _, codec = fake_api
    codec.program.issues = ["unknown command"]
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)

    with pytest.raises(DeviceConnectionError, match="decoder reported"):
        await driver.run(make_output(), MagicMock(), MagicMock())

    assert client.sent_programs == []
    codec.program.issues = []
    await driver.run(make_output(), MagicMock(), MagicMock())
    assert client.sent_programs == [codec.program]
    await driver.cleanup()


@pytest.mark.asyncio
async def test_run_rejects_noncanonical_payload(fake_api, driver_objects):
    _, codec = fake_api
    codec.payload = b"different-rd"
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)

    with pytest.raises(DeviceConnectionError, match="not an exact"):
        await driver.run(make_output(), MagicMock(), MagicMock())

    assert client.sent_programs == []
    await driver.cleanup()


@pytest.mark.asyncio
async def test_transfer_failure_has_no_success_signals(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaUdpProgramDriver(context, machine)
    driver.setup(host="192.0.2.10", port=50200, local_port=40200)
    await driver.connect()
    client = client_for(driver)
    client.send_error = RuntimeError("controller rejected packet")
    callback_calls = []
    finished_calls = []
    driver.job_finished.connect(
        lambda sender: finished_calls.append(sender),
        weak=False,
    )

    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="transfer failed",
    ):
        await driver.run(
            make_output(),
            MagicMock(),
            MagicMock(),
            callback_calls.append,
        )

    assert callback_calls == []
    assert finished_calls == []
    assert driver._execution_unconfirmed is True
    assert driver.manual_execution_confirmation_required is True
    client.send_error = None
    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="may still be executing",
    ):
        await driver.run(make_output(), MagicMock(), MagicMock())
    assert client.sent_programs == []
    await driver.cleanup()


@pytest.mark.asyncio
async def test_invalid_receipt_latches_successful_transfer(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    client.receipt = SimpleNamespace(retries=0)

    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="invalid receipt",
    ):
        await driver.run(make_output(), MagicMock(), MagicMock())

    assert client.sent_programs
    assert driver._execution_unconfirmed is True
    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="may still be executing",
    ):
        await driver.run(make_output(), MagicMock(), MagicMock())
    await driver.cleanup()


@pytest.mark.asyncio
async def test_cancellation_waits_for_blocking_transfer(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    send_started = threading.Event()
    send_release = threading.Event()
    client.send_started = send_started
    client.send_release = send_release
    callback_calls = []
    finished_calls = []
    driver.job_finished.connect(
        lambda sender: finished_calls.append(sender),
        weak=False,
    )

    task = asyncio.create_task(
        driver.run(
            make_output(),
            MagicMock(),
            MagicMock(),
            callback_calls.append,
        )
    )
    assert await asyncio.to_thread(send_started.wait, 1.0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    send_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.sent_programs
    assert callback_calls == []
    assert finished_calls == []
    assert driver._execution_unconfirmed is True
    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="may still be executing",
    ):
        await driver.run(make_output(), MagicMock(), MagicMock())
    await driver.cleanup()


@pytest.mark.asyncio
async def test_concurrent_programs_send_only_the_first(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    send_started = threading.Event()
    send_release = threading.Event()
    client.send_started = send_started
    client.send_release = send_release

    first = asyncio.create_task(
        driver.run(make_output(), MagicMock(), MagicMock())
    )
    assert await asyncio.to_thread(send_started.wait, 1.0)

    with pytest.raises(DeviceConnectionError, match="already in progress"):
        await driver.run(make_output(), MagicMock(), MagicMock())

    send_release.set()
    await first
    assert len(client.sent_programs) == 1
    await driver.cleanup()


@pytest.mark.asyncio
async def test_worker_failure_wins_over_pending_cancellation(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    send_started = threading.Event()
    send_release = threading.Event()
    client.send_started = send_started
    client.send_release = send_release
    client.send_error = RuntimeError("write failed")

    task = asyncio.create_task(
        driver.run(make_output(), MagicMock(), MagicMock())
    )
    assert await asyncio.to_thread(send_started.wait, 1.0)
    task.cancel()
    await asyncio.sleep(0)
    send_release.set()

    with pytest.raises(DeviceConnectionError, match="write failed"):
        await task

    await driver.cleanup()


@pytest.mark.asyncio
async def test_program_drivers_expose_no_guessed_controls(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)

    assert driver.native_overscan is False
    assert driver.can_home() is False
    assert driver.can_jog() is False
    assert driver.supported_wcs == ["MACHINE"]
    with pytest.raises(DeviceConnectionError, match="hold and resume"):
        await driver.set_hold()
    with pytest.raises(DeviceConnectionError, match="homing"):
        await driver.home()
    with pytest.raises(DeviceConnectionError, match="controller settings"):
        await driver.write_setting("key", 1)

    await driver.cleanup()


@pytest.mark.asyncio
async def test_connect_failure_is_device_connection_error(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaUdpProgramDriver(context, machine)
    driver.setup(host="192.0.2.10", port=50200, local_port=40200)
    client = client_for(driver)
    client.open_error = OSError("network unreachable")
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )

    with pytest.raises(DeviceConnectionError, match="network unreachable"):
        await driver.connect()

    assert statuses == [TransportStatus.CONNECTING, TransportStatus.ERROR]
    await driver.cleanup()
