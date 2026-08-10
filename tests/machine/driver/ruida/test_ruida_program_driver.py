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
            records=[object()],
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
        self.open_error: Exception | None = None
        self.send_error: Exception | None = None
        self.sent_programs = []
        self.events = []
        self.send_started: threading.Event | None = None
        self.send_release: threading.Event | None = None
        self.receipt = SimpleNamespace(
            completed_packets=2,
            retries=0,
            packets=(b"one", b"two"),
        )

    def open(self, *, probe):
        self.open_probes.append(probe)
        if self.open_error is not None:
            raise self.open_error
        self.is_open = True
        self.is_ready = True

    def close(self):
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
    api = SimpleNamespace(
        ControllerClient=FakeControllerClient,
        RuidaCodec=lambda *, context: codec,
        SerialTransport=FakeSerialTransport,
        UdpTransport=FakeUdpTransport,
    )
    monkeypatch.setattr(program_driver, "_load_ruida_re", lambda: api)
    return api, codec


@pytest.fixture
def driver_objects():
    manager = SimpleNamespace(machines={})
    context = SimpleNamespace(machine_mgr=manager)
    machine = SimpleNamespace(id="test-machine", driver_args={}, heads=[])
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
    assert events == [
        "send-started",
        "send-complete",
        "callback-0",
        "callback-2",
    ]
    assert driver.confirms_execution_completion is False

    with pytest.raises(DeviceConnectionError, match="may still be executing"):
        await driver.run(output, MagicMock(), MagicMock())

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

    with pytest.raises(DeviceConnectionError, match="transfer failed"):
        await driver.run(
            make_output(),
            MagicMock(),
            MagicMock(),
            callback_calls.append,
        )

    assert callback_calls == []
    assert finished_calls == []
    assert driver._execution_unconfirmed is True
    client.send_error = None
    with pytest.raises(DeviceConnectionError, match="may still be executing"):
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

    with pytest.raises(DeviceConnectionError, match="invalid receipt"):
        await driver.run(make_output(), MagicMock(), MagicMock())

    assert client.sent_programs
    assert driver._execution_unconfirmed is True
    with pytest.raises(DeviceConnectionError, match="may still be executing"):
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
    with pytest.raises(DeviceConnectionError, match="may still be executing"):
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
