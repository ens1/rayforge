import asyncio
import logging
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rayforge.machine.driver import drivers, get_driver_cls
from rayforge.machine.driver.driver import (
    DeviceConnectionError,
    DeviceStatus,
    ExecutionCompletionUnknownError,
    JobCancelledError,
)
from rayforge.machine.driver.ruida import program_driver
from rayforge.machine.driver.ruida.program_driver import (
    BOSS_LS2040_DA000400_STATUS_PROFILE,
    RUIDA_MACHINE_STATUS_PROFILE_KEY,
    RuidaProgramDriver,
)
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


class UnexpectedStatusFailure(Exception):
    pass


@dataclass(frozen=True)
class FakeHandshakeProfile:
    max_retries: int = 3


class MemorySerialTransport:
    kind = "serial"

    def __init__(self):
        self.is_open = False
        self.sent = []
        self.responses = []

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def send(self, data):
        self.sent.append(data)
        if data == bytes.fromhex("d4898d89"):
            self.responses.append(bytes.fromhex("d4098d898989898989"))

    def receive(self, timeout):
        del timeout
        if self.responses:
            return self.responses.pop(0)
        return None

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


def machine_status(
    raw_word=0,
    *,
    moving=False,
    job_running=False,
    part_end=False,
    unknown_bits=0,
):
    return SimpleNamespace(
        raw_word=raw_word,
        moving=moving,
        job_running=job_running,
        part_end=part_end,
        unknown_bits=unknown_bits,
    )


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
        self.status_error: Exception | None = None
        self.status_errors: list[Exception] = []
        self.status_responses = []
        self.status_reads = 0
        self.default_status_response = machine_status()
        self.auto_status_after_send = True
        self.auto_status_after_stop = True
        self.sent_programs = []
        self.stop_calls = 0
        self.stop_retry_limits = []
        self.events = []
        self.handshake_profile = FakeHandshakeProfile()
        self.open_started: threading.Event | None = None
        self.open_release: threading.Event | None = None
        self.close_started: threading.Event | None = None
        self.close_release: threading.Event | None = None
        self.send_started: threading.Event | None = None
        self.send_release: threading.Event | None = None
        self.status_started: threading.Event | None = None
        self.status_release: threading.Event | None = None
        self.receipt = SimpleNamespace(
            completed_packets=2,
            transmissions=2,
            retries=0,
            packets=(b"one", b"two"),
        )
        self.stop_receipt = SimpleNamespace(
            completed_packets=1,
            transmissions=1,
            retries=0,
            packets=(bytes.fromhex("d209"),),
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
        if self.auto_status_after_send:
            self.status_responses.extend(
                [
                    machine_status(
                        0x10401,
                        moving=True,
                        job_running=True,
                    ),
                    machine_status(0x10600),
                    machine_status(0x10600),
                    machine_status(0x10600),
                ]
            )
        self.events.append("send-complete")
        return self.receipt

    def stop_process(self):
        self.stop_calls += 1
        self.stop_retry_limits.append(self.handshake_profile.max_retries)
        if self.send_error is not None:
            raise self.send_error
        if self.auto_status_after_stop:
            self.status_responses.extend(
                [
                    machine_status(0x510600),
                    machine_status(0x10600),
                    machine_status(0x10600),
                    machine_status(0x10600),
                    machine_status(0x10600),
                    machine_status(0x10600),
                ]
            )
        return self.stop_receipt

    def read_machine_status(self):
        self.status_reads += 1
        if self.status_started is not None:
            self.status_started.set()
        if self.status_release is not None:
            self.status_release.wait()
        if self.status_errors:
            raise self.status_errors.pop(0)
        if self.status_error is not None:
            raise self.status_error
        if self.status_responses:
            return self.status_responses.pop(0)
        return self.default_status_response


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setattr(
        RuidaProgramDriver,
        "_status_semantics_validated",
        True,
    )
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


def compile_real_program(ruida_re):
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
    return ruida_re.RuidaJobCompiler().compile(plan).encode_rd()


def client_for(driver: RuidaProgramDriver) -> FakeControllerClient:
    client = driver._client
    assert isinstance(client, FakeControllerClient)
    return client


async def wait_for_tasks(task_mgr):
    settled = await asyncio.to_thread(task_mgr.wait_until_settled, 2000)
    assert settled


async def wait_until(predicate, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


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


def test_program_setup_requires_stop_process_api(fake_api, driver_objects):
    api, _codec = fake_api
    create_client = api.ControllerClient

    def create_client_without_stop(transport):
        client = create_client(transport)
        client.stop_process = None
        return client

    api.ControllerClient = create_client_without_stop
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)

    driver.setup(port="/dev/cu.ruida", baudrate=115200)

    assert driver.state.error is not None
    assert driver.state.error.title == (
        "Installed ruida-re lacks required API: ControllerClient.stop_process"
    )
    assert driver._transport is None


def test_program_setup_requires_machine_status_api(fake_api, driver_objects):
    api, _codec = fake_api
    create_client = api.ControllerClient

    def create_client_without_status(transport):
        client = create_client(transport)
        client.read_machine_status = None
        return client

    api.ControllerClient = create_client_without_status
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)

    driver.setup(port="/dev/cu.ruida", baudrate=115200)

    assert driver.state.error is not None
    assert driver.state.error.title == (
        "Installed ruida-re lacks required API: "
        "ControllerClient.read_machine_status"
    )
    assert driver._transport is None


def test_unscoped_program_setup_does_not_require_machine_status_api(
    fake_api, driver_objects
):
    api, _codec = fake_api
    create_client = api.ControllerClient

    def create_client_without_status(transport):
        client = create_client(transport)
        client.read_machine_status = None
        return client

    api.ControllerClient = create_client_without_status
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._status_semantics_validated = False

    driver.setup(port="/dev/cu.ruida", baudrate=115200)

    assert driver.state.error is None
    assert driver._transport is not None


@pytest.mark.asyncio
async def test_accepts_complete_rd_from_real_compiler(
    monkeypatch, driver_objects
):
    ruida_re = pytest.importorskip("ruida_re")
    if not hasattr(ruida_re.ControllerClient, "read_machine_status"):
        pytest.skip("installed ruida-re predates machine status support")
    payload = compile_real_program(ruida_re)
    monkeypatch.setattr(program_driver, "_load_ruida_re", lambda: ruida_re)
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._status_semantics_validated = True
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

    program_writes = [
        data for data in transport.sent if data != bytes.fromhex("d4898d89")
    ]
    assert b"".join(program_writes) == payload


@pytest.mark.asyncio
async def test_unscoped_transfer_writes_no_machine_status_request(
    monkeypatch, driver_objects
):
    ruida_re = pytest.importorskip("ruida_re")
    payload = compile_real_program(ruida_re)
    monkeypatch.setattr(program_driver, "_load_ruida_re", lambda: ruida_re)
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._status_semantics_validated = False
    transport = MemorySerialTransport()
    client = ruida_re.ControllerClient(transport)
    client.open(probe=False)
    driver._transport = transport
    driver._client = client

    await driver.run(make_output(payload), MagicMock(), MagicMock())

    assert bytes.fromhex("d4898d89") not in transport.sent
    assert b"".join(transport.sent) == payload
    assert driver.manual_execution_confirmation_required is True


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
    assert states == [DeviceStatus.IDLE]

    await driver.cleanup()


@pytest.mark.asyncio
async def test_connect_publishes_fresh_immutable_active_state(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    initial_state = driver.state
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    client = client_for(driver)
    client.status_responses.append(
        machine_status(
            0x10401,
            moving=True,
            job_running=True,
        )
    )

    await driver.connect()

    assert initial_state.status == DeviceStatus.UNKNOWN
    assert driver.state is not initial_state
    assert driver.state.status == DeviceStatus.RUN
    assert driver.manual_execution_confirmation_required is False
    await driver.cleanup()


@pytest.mark.parametrize(
    ("raw_word", "expected"),
    (
        (0, DeviceStatus.IDLE),
        (0x10600, DeviceStatus.IDLE),
        (0x10401, DeviceStatus.RUN),
        (0x10403, DeviceStatus.HOLD),
        (0x10405, DeviceStatus.RUN),
        (0x410403, DeviceStatus.RUN),
        (0x830401, DeviceStatus.RUN),
        (0x510600, DeviceStatus.RUN),
        (0x10400, DeviceStatus.UNKNOWN),
        (0x10601, DeviceStatus.UNKNOWN),
    ),
)
def test_boss_profile_maps_only_validated_exact_status_words(
    fake_api,
    driver_objects,
    raw_word,
    expected,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._status_semantics_validated = False
    driver.config = {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (BOSS_LS2040_DA000400_STATUS_PROFILE)
    }

    result = driver._publish_machine_status(
        machine_status(raw_word, unknown_bits=raw_word)
    )

    assert result == expected
    assert driver.state.status == expected
    assert driver.reports_device_status is True
    assert driver.confirms_execution_completion is True


def test_generic_ruida_does_not_interpret_validated_boss_word(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._status_semantics_validated = False
    driver.config = {}

    result = driver._publish_machine_status(machine_status(0x10401))

    assert result == DeviceStatus.UNKNOWN
    assert driver.reports_device_status is False
    assert driver.confirms_execution_completion is False


def test_completion_capability_override_cannot_bypass_profile_scope(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)

    driver.confirms_execution_completion = True

    assert driver.confirms_execution_completion is False

    driver.config = {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (BOSS_LS2040_DA000400_STATUS_PROFILE)
    }
    assert driver.confirms_execution_completion is True

    driver.confirms_execution_completion = False

    assert driver.confirms_execution_completion is False


@pytest.mark.asyncio
async def test_boss_status_marker_does_not_enable_udp(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaUdpProgramDriver(context, machine)
    driver._status_semantics_validated = False
    driver.config = {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (BOSS_LS2040_DA000400_STATUS_PROFILE)
    }
    driver.setup(host="192.0.2.10", port=50200, local_port=40200)

    await driver.connect()
    client = client_for(driver)
    await driver.run(make_output(), MagicMock(), MagicMock())

    assert driver.state.status == DeviceStatus.UNKNOWN
    assert driver.reports_device_status is False
    assert driver.confirms_execution_completion is False
    assert client.status_reads == 0
    assert client.sent_programs
    assert driver.manual_execution_confirmation_required is True
    await driver.cleanup()


@pytest.mark.asyncio
async def test_unvalidated_status_words_remain_transfer_only_and_unknown(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._status_semantics_validated = False
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.setup(port="/dev/cu.ruida", baudrate=115200)

    await driver.connect()
    client = client_for(driver)
    assert driver.state.status == DeviceStatus.UNKNOWN
    await asyncio.sleep(0.02)

    assert driver.state.status == DeviceStatus.UNKNOWN
    assert client.status_reads == 0
    assert driver._last_raw_status_word is None
    assert driver.manual_execution_confirmation_required is False
    driver._execution_completion_monitoring_enabled = True
    assert driver._can_confirm_execution is False
    assert driver.confirms_execution_completion is False
    assert driver.reports_device_status is False

    await driver.run(make_output(), MagicMock(), MagicMock())

    assert client.sent_programs
    assert client.status_reads == 0
    assert driver.manual_execution_confirmation_required is True
    await driver.cleanup()


@pytest.mark.asyncio
async def test_status_polling_publishes_run_then_idle_as_new_states(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    published = []
    driver.state_changed.connect(
        lambda sender, state: published.append(state),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    connected_state = driver.state
    client = client_for(driver)
    client.status_responses.extend(
        [
            machine_status(0x10401, job_running=True),
            machine_status(),
        ]
    )

    await wait_until(
        lambda: (
            len(published) >= 3 and published[-1].status == DeviceStatus.IDLE
        )
    )

    assert connected_state.status == DeviceStatus.IDLE
    assert [state.status for state in published[-2:]] == [
        DeviceStatus.RUN,
        DeviceStatus.IDLE,
    ]
    assert published[-2] is not published[-1]
    assert driver.state is published[-1]
    await driver.cleanup()


@pytest.mark.asyncio
async def test_polling_never_clears_operator_confirmation_latch(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    observed_statuses = []
    driver.state_changed.connect(
        lambda sender, state: observed_statuses.append(state.status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)

    await driver.run(make_output(), MagicMock(), MagicMock())
    await wait_until(
        lambda: (
            DeviceStatus.RUN in observed_statuses
            and driver.state.status == DeviceStatus.IDLE
            and not client.status_responses
        )
    )

    assert driver.confirms_execution_completion is False
    assert driver._execution_completion_monitoring_enabled is False
    assert driver._status_semantics_validated is True
    assert driver.manual_execution_confirmation_required is True
    assert driver._active_job_generation is None
    await driver.cleanup()


@pytest.mark.asyncio
async def test_boss_profile_run_waits_for_validated_natural_completion(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._status_semantics_validated = False
    driver.config = {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (BOSS_LS2040_DA000400_STATUS_PROFILE)
    }
    driver.STATUS_POLL_INTERVAL = 0.005
    finished = []
    driver.job_finished.connect(
        lambda sender: finished.append(sender),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)

    await driver.run(make_output(), MagicMock(), MagicMock())

    assert client.sent_programs
    assert driver.manual_execution_confirmation_required is False
    assert driver._active_job_generation is None
    assert finished == [driver]
    await driver.cleanup()


@pytest.mark.asyncio
async def test_boss_profile_completes_two_jobs_on_one_connection(
    fake_api, driver_objects
):
    api, _codec = fake_api
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.config = {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (BOSS_LS2040_DA000400_STATUS_PROFILE)
    }
    driver.STATUS_POLL_INTERVAL = 0.005
    finished = []
    driver.job_finished.connect(
        lambda sender: finished.append(sender),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)

    await driver.run(make_output(), MagicMock(), MagicMock())

    assert driver._active_job_generation is None
    assert driver.manual_execution_confirmation_required is False
    assert driver.state.status == DeviceStatus.IDLE

    await driver.run(make_output(), MagicMock(), MagicMock())

    assert driver._client is client
    assert api.clients == [client]
    assert client.open_probes == [False]
    assert len(client.sent_programs) == 2
    assert driver._job_generation == 2
    assert driver._active_job_generation is None
    assert driver.manual_execution_confirmation_required is False
    assert driver.state.status == DeviceStatus.IDLE
    assert finished == [driver, driver]
    await driver.cleanup()


@pytest.mark.asyncio
async def test_internal_completion_requires_active_then_stable_idle(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    finished = []
    driver.job_finished.connect(
        lambda sender: finished.append(sender),
        weak=False,
    )
    driver._execution_completion_monitoring_enabled = True
    driver._execution_unconfirmed = True
    assert driver.confirms_execution_completion is True
    generation, future = driver._begin_completion("job")
    preactive_idle = machine_status()
    completed_idle = machine_status(0x10600)
    active = machine_status(0x10401, job_running=True)

    driver._process_completion_status(preactive_idle, generation)
    driver._process_completion_status(preactive_idle, generation)
    assert future.done() is False
    driver._process_completion_status(active, generation)
    driver._process_completion_status(completed_idle, generation)
    driver._process_completion_status(completed_idle, generation)
    assert future.done() is False
    driver._process_completion_status(completed_idle, generation)
    await future

    assert driver.manual_execution_confirmation_required is False
    assert driver._active_job_generation is None
    assert finished == [driver]


@pytest.mark.asyncio
async def test_internal_completion_rejects_unobserved_short_job(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.ACTIVE_OBSERVATION_IDLE_SAMPLES = 2
    driver._execution_completion_monitoring_enabled = True
    driver._execution_unconfirmed = True
    generation, future = driver._begin_completion("job")

    driver._process_completion_status(machine_status(), generation)
    driver._process_completion_status(machine_status(0x10600), generation)

    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="never reported this program as active",
    ):
        await future
    assert driver.manual_execution_confirmation_required is True
    assert driver._active_job_generation is None


@pytest.mark.asyncio
async def test_natural_completion_rejects_fresh_idle_after_active(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._execution_completion_monitoring_enabled = True
    driver._execution_unconfirmed = True
    generation, future = driver._begin_completion("job")
    driver._process_completion_status(machine_status(0x10401), generation)

    driver._process_completion_status(machine_status(0), generation)

    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="post-execution idle",
    ):
        await future
    assert driver.manual_execution_confirmation_required is True


@pytest.mark.asyncio
async def test_unknown_exact_word_fails_active_completion_closed(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._execution_completion_monitoring_enabled = True
    driver._execution_unconfirmed = True
    generation, future = driver._begin_completion("job")
    driver._process_completion_status(machine_status(0x10401), generation)

    driver._process_completion_status(machine_status(0x10402), generation)

    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="unvalidated machine status 0x10402",
    ):
        await future
    assert driver.manual_execution_confirmation_required is True


@pytest.mark.asyncio
async def test_stale_status_generation_cannot_complete_current_job(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._execution_completion_monitoring_enabled = True
    old_generation, old_future = driver._begin_completion("job")
    driver._abandon_completion(old_generation)
    assert old_future.cancelled()
    generation, future = driver._begin_completion("job")

    driver._process_completion_status(
        machine_status(0x10401, job_running=True),
        old_generation,
    )
    for _ in range(driver.COMPLETION_IDLE_SAMPLES):
        driver._process_completion_status(
            machine_status(0x10600),
            old_generation,
        )

    assert generation != old_generation
    assert future.done() is False
    assert driver._active_job_generation == generation
    driver._abandon_completion(generation)


@pytest.mark.asyncio
async def test_internal_cancel_requires_post_stop_stable_idle(
    fake_api, driver_objects, monkeypatch
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._execution_completion_monitoring_enabled = True
    driver._execution_unconfirmed = True
    generation, future = driver._begin_completion("job")
    finished = []
    driver.job_finished.connect(
        lambda sender: finished.append(sender),
        weak=False,
    )
    driver._process_completion_status(
        machine_status(0x10401),
        generation,
    )
    driver.notify_cancel_requested()

    for _ in range(driver.COMPLETION_IDLE_SAMPLES):
        driver._process_completion_status(
            machine_status(0x10600),
            generation,
        )
    assert future.done() is False
    driver._stop_delivered_generation = generation
    clock = [10.0]
    monkeypatch.setattr(program_driver, "monotonic", lambda: clock[0])
    driver._process_completion_status(
        machine_status(0x10600),
        generation,
    )
    clock[0] = 10.2
    driver._process_completion_status(
        machine_status(0x10600),
        generation,
    )
    clock[0] = 10.7
    driver._process_completion_status(
        machine_status(0x10600),
        generation,
    )

    assert future.done() is True

    with pytest.raises(JobCancelledError):
        await future
    assert driver.manual_execution_confirmation_required is False
    assert finished == []


@pytest.mark.asyncio
async def test_stop_transition_resets_stable_idle_window(
    fake_api, driver_objects, monkeypatch
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._execution_completion_monitoring_enabled = True
    driver._execution_unconfirmed = True
    generation, future = driver._begin_completion("job")
    driver._process_completion_status(machine_status(0x10401), generation)
    driver.notify_cancel_requested()
    driver._stop_delivered_generation = generation
    clock = [20.0]
    monkeypatch.setattr(program_driver, "monotonic", lambda: clock[0])
    driver._process_completion_status(machine_status(0x10600), generation)
    clock[0] = 20.4
    driver._process_completion_status(machine_status(0x10600), generation)
    driver._process_completion_status(machine_status(0x510600), generation)
    clock[0] = 21.0
    driver._process_completion_status(machine_status(0x10600), generation)
    clock[0] = 21.3
    driver._process_completion_status(machine_status(0x10600), generation)
    clock[0] = 21.5
    driver._process_completion_status(machine_status(0x10600), generation)
    assert future.done() is False
    clock[0] = 21.7
    driver._process_completion_status(machine_status(0x10600), generation)

    with pytest.raises(JobCancelledError):
        await future
    assert driver.manual_execution_confirmation_required is False


@pytest.mark.asyncio
async def test_boss_stop_confirms_only_observed_active_generation(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._status_semantics_validated = False
    driver.config = {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (BOSS_LS2040_DA000400_STATUS_PROFILE)
    }
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STOP_IDLE_MIN_DURATION = 0
    driver.ACTIVE_OBSERVATION_IDLE_SAMPLES = 1000
    finished = []
    driver.job_finished.connect(
        lambda sender: finished.append(sender),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    client.auto_status_after_send = False
    run_task = asyncio.create_task(
        driver.run(make_output(), MagicMock(), MagicMock())
    )
    await wait_until(lambda: bool(client.sent_programs))
    client.status_responses.append(machine_status(0x10401))
    await wait_until(lambda: driver._observed_active)

    await driver.cancel()

    with pytest.raises(JobCancelledError):
        await run_task
    assert client.stop_calls == 1
    assert client.stop_retry_limits == [0]
    assert client.handshake_profile.max_retries == 3
    assert driver.manual_execution_confirmation_required is False
    assert finished == []
    await driver.cleanup()


@pytest.mark.asyncio
async def test_concurrent_boss_stop_is_one_shot_and_new_generation_resets(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.config = {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (BOSS_LS2040_DA000400_STATUS_PROFILE)
    }
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STOP_IDLE_MIN_DURATION = 0
    driver.ACTIVE_OBSERVATION_IDLE_SAMPLES = 1000
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    client.auto_status_after_send = False
    stop_started = threading.Event()
    stop_release = threading.Event()
    stop_attempts = []
    original_stop = client.stop_process

    def blocking_stop():
        stop_attempts.append(None)
        stop_started.set()
        stop_release.wait()
        return original_stop()

    client.stop_process = blocking_stop
    run_task = asyncio.create_task(
        driver.run(make_output(), MagicMock(), MagicMock())
    )
    await wait_until(lambda: bool(client.sent_programs))
    client.status_responses.append(machine_status(0x10401))
    await wait_until(lambda: driver._observed_active)

    first_stop = asyncio.create_task(driver.cancel())
    assert await asyncio.to_thread(stop_started.wait, 1)
    second_stop = asyncio.create_task(driver.cancel())
    await asyncio.sleep(0.02)
    assert len(stop_attempts) == 1
    stop_release.set()
    await asyncio.gather(first_stop, second_stop)

    with pytest.raises(JobCancelledError):
        await run_task
    assert client.stop_calls == 1
    assert driver.manual_execution_confirmation_required is False

    run_task = asyncio.create_task(
        driver.run(make_output(), MagicMock(), MagicMock())
    )
    await wait_until(lambda: len(client.sent_programs) == 2)
    client.status_responses.append(machine_status(0x10401))
    await wait_until(lambda: driver._observed_active)
    await driver.cancel()

    with pytest.raises(JobCancelledError):
        await run_task
    assert len(stop_attempts) == 2
    assert client.stop_calls == 2
    await driver.cleanup()


@pytest.mark.asyncio
async def test_cancelled_boss_stop_attempt_is_not_retransmitted(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.config = {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (BOSS_LS2040_DA000400_STATUS_PROFILE)
    }
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.ACTIVE_OBSERVATION_IDLE_SAMPLES = 1000
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    client.auto_status_after_send = False
    stop_started = threading.Event()
    stop_release = threading.Event()
    original_stop = client.stop_process

    def blocking_stop():
        stop_started.set()
        stop_release.wait()
        return original_stop()

    client.stop_process = blocking_stop
    run_task = asyncio.create_task(
        driver.run(make_output(), MagicMock(), MagicMock())
    )
    await wait_until(lambda: bool(client.sent_programs))
    client.status_responses.append(machine_status(0x10401))
    await wait_until(lambda: driver._observed_active)

    stop_task = asyncio.create_task(driver.cancel())
    assert await asyncio.to_thread(stop_started.wait, 1)
    stop_task.cancel()
    stop_release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop_task
    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="already attempted",
    ):
        await driver.cancel()

    assert client.stop_calls == 1
    with pytest.raises(asyncio.CancelledError):
        await run_task
    await driver.cleanup()


@pytest.mark.asyncio
async def test_scoped_boss_stop_rejects_unexpected_serial_packet(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.config = {
        RUIDA_MACHINE_STATUS_PROFILE_KEY: (BOSS_LS2040_DA000400_STATUS_PROFILE)
    }
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    client.stop_receipt = SimpleNamespace(
        completed_packets=1,
        transmissions=1,
        retries=0,
        packets=(bytes.fromhex("00dbd209"),),
    )

    with pytest.raises(DeviceConnectionError, match="unexpected serial"):
        await driver.cancel()
    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="already attempted",
    ):
        await driver.cancel()

    assert client.stop_calls == 1
    await driver.cleanup()


@pytest.mark.asyncio
async def test_unknown_status_flags_fail_closed_without_clearing_latch(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    driver._execution_unconfirmed = True
    client.status_responses.append(machine_status(0x40, unknown_bits=0x40))

    await wait_until(lambda: driver.state.status == DeviceStatus.UNKNOWN)

    assert driver.manual_execution_confirmation_required is True
    assert client.is_ready is True
    await driver.cleanup()


@pytest.mark.asyncio
async def test_idle_status_failure_reopens_session_and_resumes_polling(
    fake_api,
    driver_objects,
    caplog,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.005
    driver.STATUS_RECONNECT_MAX_DELAY = 0.005
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    initial_generation = driver._session_generation
    initial_status_task = driver._status_task

    try:
        with caplog.at_level(logging.WARNING, logger=program_driver.__name__):
            client.status_errors.append(
                UnexpectedStatusFailure("transient status timeout")
            )
            await wait_until(
                lambda: (
                    client.open_probes == [False, False]
                    and statuses[-1] == TransportStatus.CONNECTED
                )
            )

        assert TransportStatus.SLEEPING in statuses
        assert TransportStatus.ERROR not in statuses
        assert client.close_calls == 1
        assert client.is_ready is True
        assert driver.state.status == DeviceStatus.IDLE
        assert driver._session_generation == initial_generation + 1
        assert initial_status_task is not None
        assert initial_status_task.done()
        assert driver._status_task is not None
        assert driver._status_task is not initial_status_task
        assert not driver._status_task.done()
        assert "transient status timeout" in caplog.text
    finally:
        await driver.cleanup()


@pytest.mark.asyncio
async def test_idle_status_recovery_retries_failed_reopen_status(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.005
    driver.STATUS_RECONNECT_MAX_DELAY = 0.005
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    try:
        client.status_errors.extend(
            [
                UnexpectedStatusFailure("poll timeout"),
                UnexpectedStatusFailure("reopen status timeout"),
            ]
        )

        await wait_until(
            lambda: (
                client.open_probes == [False, False, False]
                and statuses[-1] == TransportStatus.CONNECTED
            )
        )

        assert statuses.count(TransportStatus.SLEEPING) >= 2
        assert TransportStatus.ERROR not in statuses
        assert client.close_calls == 2
        assert client.is_ready is True
        assert driver.state.status == DeviceStatus.IDLE
    finally:
        await driver.cleanup()


@pytest.mark.asyncio
async def test_cleanup_cancels_idle_status_reconnect_backoff(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 60.0
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    try:
        client.status_errors.append(UnexpectedStatusFailure("status timeout"))
        await wait_until(
            lambda: (
                client.close_calls == 1
                and statuses[-1] == TransportStatus.SLEEPING
            )
        )

        await asyncio.wait_for(driver.cleanup(), timeout=0.2)

        assert client.open_probes == [False]
        assert client.is_ready is False
        assert driver._status_task is None
        assert driver._status_reconnect_task is None
        assert driver._client is None
        assert statuses[-1] == TransportStatus.DISCONNECTED
    finally:
        if driver._client is not None:
            await driver.cleanup()


@pytest.mark.asyncio
async def test_udp_status_recovery_uses_configured_probe(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaUdpProgramDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.005
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(host="192.0.2.10", port=50200, local_port=40200)
    await driver.connect()
    client = client_for(driver)

    try:
        client.status_errors.append(UnexpectedStatusFailure("status timeout"))
        await wait_until(
            lambda: (
                client.open_probes == [True, True]
                and statuses[-1] == TransportStatus.CONNECTED
            )
        )

        assert client.close_calls == 1
        assert client.is_ready is True
    finally:
        await driver.cleanup()


@pytest.mark.asyncio
async def test_idle_status_close_failure_never_reopens(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.005
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    client.close_error = RuntimeError("close failed")

    try:
        client.status_errors.append(UnexpectedStatusFailure("status timeout"))
        await wait_until(lambda: statuses[-1] == TransportStatus.ERROR)
        await asyncio.sleep(0.02)

        assert client.open_probes == [False]
        assert client.close_calls == 1
        assert client.is_ready is True
        assert driver._status_reconnect_task is None
    finally:
        client.close_error = None
        await driver.cleanup()


@pytest.mark.asyncio
async def test_status_failure_during_program_transfer_never_reopens(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.005
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    driver._program_active = True

    try:
        client.status_errors.append(UnexpectedStatusFailure("status timeout"))
        await wait_until(
            lambda: (
                statuses[-1] == TransportStatus.ERROR
                and client.close_calls == 1
            )
        )
        await asyncio.sleep(0.02)

        assert client.open_probes == [False]
        assert client.is_ready is False
        assert driver._status_reconnect_task is None
    finally:
        driver._program_active = False
        await driver.cleanup()


@pytest.mark.asyncio
async def test_status_recovery_does_not_clear_latch_set_during_open(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.005
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    open_started = threading.Event()
    open_release = threading.Event()
    client.open_started = open_started
    client.open_release = open_release

    try:
        client.status_errors.append(UnexpectedStatusFailure("status timeout"))
        assert await asyncio.to_thread(open_started.wait, 1.0)
        driver._execution_unconfirmed = True
        open_release.set()
        await wait_until(
            lambda: (
                statuses[-1] == TransportStatus.ERROR
                and client.close_calls == 2
            )
        )
        await asyncio.sleep(0.02)

        assert driver.manual_execution_confirmation_required is True
        assert client.open_probes == [False, False]
        assert client.is_ready is False
        assert driver._status_reconnect_task is None
    finally:
        open_release.set()
        await driver.cleanup()


@pytest.mark.asyncio
async def test_cleanup_cancels_blocked_status_reconnect_read(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.05
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    status_started = threading.Event()
    status_release = threading.Event()

    try:
        client.status_errors.append(UnexpectedStatusFailure("status timeout"))
        await wait_until(
            lambda: (
                client.close_calls == 1
                and statuses[-1] == TransportStatus.SLEEPING
            )
        )
        client.status_started = status_started
        client.status_release = status_release
        assert await asyncio.to_thread(status_started.wait, 1.0)

        cleanup_task = asyncio.create_task(driver.cleanup())
        await asyncio.sleep(0)
        assert not cleanup_task.done()
        status_release.set()
        await asyncio.wait_for(cleanup_task, timeout=0.2)

        assert client.open_probes == [False, False]
        assert client.close_calls == 2
        assert client.is_ready is False
        assert driver._status_task is None
        assert driver._status_reconnect_task is None
        assert driver._client is None
        assert statuses[-1] == TransportStatus.DISCONNECTED
    finally:
        status_release.set()
        if driver._client is not None:
            await driver.cleanup()


@pytest.mark.asyncio
async def test_cleanup_cancellation_wins_when_reconnect_open_raises(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.05
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    open_started = threading.Event()
    open_release = threading.Event()

    try:
        client.status_errors.append(UnexpectedStatusFailure("status timeout"))
        await wait_until(
            lambda: (
                client.close_calls == 1
                and statuses[-1] == TransportStatus.SLEEPING
            )
        )
        client.open_started = open_started
        client.open_release = open_release
        client.open_error = RuntimeError("open failed")
        assert await asyncio.to_thread(open_started.wait, 1.0)
        reconnect_task = driver._status_reconnect_task
        assert reconnect_task is not None

        cleanup_task = asyncio.create_task(driver.cleanup())
        await wait_until(lambda: reconnect_task.cancelling() > 0)
        open_release.set()
        await asyncio.wait_for(cleanup_task, timeout=0.2)

        assert client.open_probes == [False, False]
        assert client.close_calls == 1
        assert client.is_ready is False
        assert driver._status_task is None
        assert driver._status_reconnect_task is None
        assert driver._client is None
        assert statuses[-1] == TransportStatus.DISCONNECTED
    finally:
        open_release.set()
        client.open_error = None
        if driver._client is not None:
            await driver.cleanup()


@pytest.mark.asyncio
async def test_cleanup_cancellation_wins_when_reconnect_status_raises(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.05
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    status_started = threading.Event()
    status_release = threading.Event()

    try:
        client.status_errors.append(UnexpectedStatusFailure("status timeout"))
        await wait_until(
            lambda: (
                client.close_calls == 1
                and statuses[-1] == TransportStatus.SLEEPING
            )
        )
        client.status_started = status_started
        client.status_release = status_release
        client.status_errors.append(RuntimeError("status read failed"))
        assert await asyncio.to_thread(status_started.wait, 1.0)
        reconnect_task = driver._status_reconnect_task
        assert reconnect_task is not None

        cleanup_task = asyncio.create_task(driver.cleanup())
        await wait_until(lambda: reconnect_task.cancelling() > 0)
        status_release.set()
        await asyncio.wait_for(cleanup_task, timeout=0.2)

        assert client.open_probes == [False, False]
        assert client.close_calls == 2
        assert client.is_ready is False
        assert driver._status_task is None
        assert driver._status_reconnect_task is None
        assert driver._client is None
        assert statuses[-1] == TransportStatus.DISCONNECTED
    finally:
        status_release.set()
        if driver._client is not None:
            await driver.cleanup()


@pytest.mark.asyncio
async def test_cleanup_cancellation_wins_during_failed_reconnect_close(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.05
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    close_started = threading.Event()
    close_release = threading.Event()

    try:
        client.status_errors.append(UnexpectedStatusFailure("status timeout"))
        await wait_until(
            lambda: (
                client.close_calls == 1
                and statuses[-1] == TransportStatus.SLEEPING
            )
        )
        client.status_errors.append(RuntimeError("reconnect status failed"))
        client.close_started = close_started
        client.close_release = close_release
        assert await asyncio.to_thread(close_started.wait, 1.0)
        reconnect_task = driver._status_reconnect_task
        assert reconnect_task is not None

        cleanup_task = asyncio.create_task(driver.cleanup())
        await wait_until(lambda: reconnect_task.cancelling() > 0)
        close_release.set()
        await asyncio.wait_for(cleanup_task, timeout=0.2)

        assert client.open_probes == [False, False]
        assert client.close_calls == 2
        assert client.is_ready is False
        assert driver._status_task is None
        assert driver._status_reconnect_task is None
        assert driver._client is None
        assert statuses[-1] == TransportStatus.DISCONNECTED
    finally:
        close_release.set()
        if driver._client is not None:
            await driver.cleanup()


@pytest.mark.asyncio
async def test_status_recovery_rechecks_resource_conflicts(
    fake_api,
    driver_objects,
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.STATUS_RECONNECT_INITIAL_DELAY = 0.02
    driver.STATUS_RECONNECT_MAX_DELAY = 0.02
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    busy = {"connected": True}

    try:
        client.status_errors.append(UnexpectedStatusFailure("status timeout"))
        await wait_until(
            lambda: (
                client.close_calls == 1
                and statuses[-1] == TransportStatus.SLEEPING
            )
        )
        other_driver = SimpleNamespace(resource_uri=driver.resource_uri)
        other_machine = SimpleNamespace(
            name="Other laser",
            driver=other_driver,
            is_connected=lambda: busy["connected"],
        )
        context.machine_mgr.machines["other"] = other_machine

        await asyncio.sleep(0.06)
        assert client.open_probes == [False]
        assert statuses[-1] == TransportStatus.SLEEPING
        assert driver._status_reconnect_task is not None

        busy["connected"] = False
        await wait_until(
            lambda: (
                client.open_probes == [False, False]
                and statuses[-1] == TransportStatus.CONNECTED
            )
        )
        assert client.is_ready is True
    finally:
        busy["connected"] = False
        await driver.cleanup()


@pytest.mark.asyncio
async def test_status_failure_invalidates_session_and_retains_latch(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    driver._execution_unconfirmed = True
    client.status_error = UnexpectedStatusFailure("status timeout")

    await wait_until(lambda: TransportStatus.ERROR in statuses)

    assert driver.state.status == DeviceStatus.UNKNOWN
    assert driver.manual_execution_confirmation_required is True
    assert client.is_ready is False
    assert client.open_probes == [False]
    await driver.cleanup()


@pytest.mark.asyncio
async def test_status_failure_fails_generation_started_after_poll_snapshot(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver._execution_completion_monitoring_enabled = True
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    await driver._stop_status_polling()
    client = client_for(driver)
    generation, future = driver._begin_completion("job")
    driver._execution_unconfirmed = True

    await driver._handle_status_failure(
        client,
        driver._session_generation,
        UnexpectedStatusFailure("late poll failed"),
    )

    with pytest.raises(
        ExecutionCompletionUnknownError,
        match="status became unavailable",
    ):
        await future
    assert driver._active_job_generation is None
    assert driver.manual_execution_confirmation_required is True
    assert client.is_ready is False
    assert generation == 1
    await driver.cleanup()


@pytest.mark.asyncio
async def test_malformed_polled_status_invalidates_session(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    statuses = []
    driver.connection_status_changed.connect(
        lambda sender, status, message: statuses.append(status),
        weak=False,
    )
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    driver._execution_unconfirmed = True
    client.status_responses.append(SimpleNamespace(raw_word=0))

    await wait_until(lambda: TransportStatus.ERROR in statuses)

    assert driver.state.status == DeviceStatus.UNKNOWN
    assert driver.manual_execution_confirmation_required is True
    assert client.is_ready is False
    assert driver._status_task is None
    await driver.cleanup()


@pytest.mark.asyncio
async def test_active_preflight_rejects_program_without_transfer(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    client.status_responses.append(machine_status(0x10401, job_running=True))

    with pytest.raises(
        DeviceConnectionError,
        match="controller program is active",
    ):
        await driver.run(make_output(), MagicMock(), MagicMock())

    assert client.sent_programs == []
    assert driver.state.status == DeviceStatus.RUN
    assert driver.manual_execution_confirmation_required is False
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
    assert driver.reports_device_status is True
    assert driver.supports_cancel is True
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
async def test_cancel_sends_process_stop_and_retains_execution_latch(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.STATUS_POLL_INTERVAL = 0.005
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    driver._execution_unconfirmed = True

    await driver.cancel()

    assert client.stop_calls == 1
    assert client.stop_retry_limits == [0]
    assert client.handshake_profile.max_retries == 3
    assert client.sent_programs == []
    await wait_until(lambda: not client.status_responses)
    assert driver.state.status == DeviceStatus.IDLE
    assert driver.manual_execution_confirmation_required is True
    await driver.cleanup()


@pytest.mark.asyncio
async def test_cancel_serializes_exact_process_stop_wire_bytes(
    monkeypatch, driver_objects
):
    ruida_re = pytest.importorskip("ruida_re")
    monkeypatch.setattr(program_driver, "_load_ruida_re", lambda: ruida_re)
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    transport = MemorySerialTransport()
    client = ruida_re.ControllerClient(transport)
    client.open(probe=False)
    driver._transport = transport
    driver._client = client

    await driver.cancel()

    assert transport.sent == [bytes.fromhex("d209")]
    assert driver.manual_execution_confirmation_required is True
    await driver.cleanup()


@pytest.mark.asyncio
async def test_cancel_failure_retains_execution_latch(
    fake_api, driver_objects
):
    context, machine = driver_objects
    driver = RuidaSerialDriver(context, machine)
    driver.setup(port="/dev/cu.ruida", baudrate=115200)
    await driver.connect()
    client = client_for(driver)
    client.send_error = OSError("stop write failed")

    with pytest.raises(DeviceConnectionError, match="stop write failed"):
        await driver.cancel()

    assert driver.manual_execution_confirmation_required is True
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
    assert driver.supports_hold is False
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
