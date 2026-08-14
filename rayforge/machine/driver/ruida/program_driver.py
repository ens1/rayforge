"""Evidence-backed Ruida program transfer driver foundation."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import sys
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from gettext import gettext as _
from typing import TYPE_CHECKING, Any, NoReturn

from raygeo.ops.axis import Axis

from ....context import RayforgeContext
from ....core.varset import VarSet
from ....pipeline.encoder.base import EncodedOutput, OpsEncoder
from ...transport import TransportStatus
from ..driver import (
    DeviceConnectionError,
    DeviceStatus,
    Driver,
    DriverMaturity,
    DriverSetupError,
    ExecutionCompletionUnknownError,
    Pos,
    PWMParams,
)
from .ruida_encoder import (
    RuidaEncoder,
    RuidaEncodingError,
    ruida_pwm_params,
    validate_ruida_program_bounds,
)

if TYPE_CHECKING:
    from raygeo.ops import Ops

    from ....core.doc import Doc
    from ...models.head import Head
    from ...models.laser import Laser
    from ...models.machine import Machine


logger = logging.getLogger(__name__)


def _load_ruida_re() -> Any:
    try:
        module = importlib.import_module("ruida_re")
    except ImportError as error:
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise DriverSetupError(
            "Ruida program transfer requires the optional ruida-re "
            f"package and Python 3.11 or newer; running Python {version}"
        ) from error

    required = (
        "ControllerClient",
        "RuidaCodec",
        "SerialTransport",
        "UdpTransport",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        names = ", ".join(missing)
        raise DriverSetupError(
            f"Installed ruida-re lacks required API: {names}"
        )
    return module


class RuidaProgramDriver(Driver):
    """Transfer complete ``.rd`` programs without controller management."""

    supports_settings = False
    reports_granular_progress = False
    reports_device_status = False
    confirms_execution_completion = False
    supports_cancel = True
    uses_gcode = False
    accepts_arc_ops = False
    accepts_curve_ops = False
    maturity = DriverMaturity.EXPERIMENTAL
    native_overscan = False
    _probe_on_open = True

    def __init__(self, context: RayforgeContext, machine: Machine):
        super().__init__(context, machine)
        self._transport: Any | None = None
        self._client: Any | None = None
        self._resource: str | None = None
        self._io_lock = asyncio.Lock()
        self._program_active = False
        self._execution_unconfirmed = False

    def supports_pwm(self, head: Head) -> bool:
        return self.get_pwm_params(head) is not None

    def get_pwm_params(self, head: Head) -> PWMParams | None:
        return ruida_pwm_params(self._machine, head)

    @classmethod
    def create_encoder_context(
        cls,
        machine: Machine,
    ) -> tuple[OpsEncoder, Any]:
        return RuidaEncoder.context_from_machine(machine)

    @property
    def machine_space_wcs(self) -> str:
        return "MACHINE"

    @property
    def machine_space_wcs_display_name(self) -> str:
        return _("Machine Coordinates")

    @property
    def supported_wcs(self) -> list[str]:
        return [self.machine_space_wcs]

    @property
    def resource_uri(self) -> str | None:
        return self._resource

    @property
    def manual_execution_confirmation_required(self) -> bool:
        return self._execution_unconfirmed

    @classmethod
    def create_encoder(cls, machine: Machine) -> OpsEncoder:
        return RuidaEncoder.from_machine(machine)

    @classmethod
    def encoder_token_payload(cls, machine: Machine) -> Any:
        return RuidaEncoder.token_payload(machine)

    @abstractmethod
    def _create_transport(self, module: Any, **kwargs: Any) -> tuple[Any, str]:
        """Construct one ruida-re transport and its resource URI."""

    def _setup_implementation(self, **kwargs: Any) -> None:
        try:
            RuidaEncoder.token_payload(self._machine)
        except RuidaEncodingError as error:
            raise DriverSetupError(str(error)) from error
        module = _load_ruida_re()
        try:
            transport, resource = self._create_transport(module, **kwargs)
            client = module.ControllerClient(transport)
            if not callable(getattr(client, "stop_process", None)):
                raise DriverSetupError(
                    "Installed ruida-re lacks required API: "
                    "ControllerClient.stop_process"
                )
        except DriverSetupError:
            raise
        except Exception as error:
            raise DriverSetupError(str(error)) from error
        self._transport = transport
        self._client = client
        self._resource = resource

    async def _connect_implementation(self) -> None:
        client = self._client
        if client is None:
            error = DeviceConnectionError(_("Ruida driver is not configured."))
            self._update_connection_status(TransportStatus.ERROR, str(error))
            raise error

        if getattr(client, "is_ready", False):
            self._update_connection_status(TransportStatus.CONNECTED)
            return

        self._update_connection_status(TransportStatus.CONNECTING)
        try:
            await self._call_blocking(
                client.open,
                probe=self._probe_on_open,
            )
        except asyncio.CancelledError:
            await self._close_after_cancelled_open(client)
            if getattr(client, "is_open", False) or getattr(
                client, "is_ready", False
            ):
                self._update_connection_status(
                    TransportStatus.ERROR,
                    _("Could not close the cancelled Ruida connection."),
                )
            else:
                self._update_connection_status(TransportStatus.DISCONNECTED)
            raise
        except Exception as error:
            message = _("Could not connect to the Ruida controller: {error}")
            wrapped = DeviceConnectionError(message.format(error=error))
            self._update_connection_status(TransportStatus.ERROR, str(wrapped))
            raise wrapped from error

        self._execution_unconfirmed = False
        self._update_connection_status(TransportStatus.CONNECTED)

    async def _close_after_cancelled_open(self, client: Any) -> None:
        if not (
            getattr(client, "is_open", False)
            or getattr(client, "is_ready", False)
        ):
            return
        try:
            await self._call_blocking(client.close)
        except asyncio.CancelledError:
            pass
        except Exception as error:  # noqa: BLE001 - cancellation cleanup
            logger.warning(
                "Could not close cancelled Ruida program transport: %s",
                error,
                extra=self._log_extra("MACHINE_EVENT"),
            )

    async def cleanup(self) -> None:
        client = self._client
        if client is not None and (
            getattr(client, "is_open", False)
            or getattr(client, "is_ready", False)
        ):
            self._update_connection_status(TransportStatus.CLOSING)
            try:
                await self._call_blocking(
                    client.close,
                    _preserve_cancellation_on_error=True,
                )
            except asyncio.CancelledError:
                if getattr(client, "is_open", False) or getattr(
                    client, "is_ready", False
                ):
                    self._update_connection_status(
                        TransportStatus.ERROR,
                        _("The Ruida controller connection is still open."),
                    )
                else:
                    await self._finalize_cleanup()
                raise
            except Exception as error:
                if getattr(client, "is_open", False) or getattr(
                    client, "is_ready", False
                ):
                    message = _(
                        "Could not close the Ruida controller connection: "
                        "{error}"
                    ).format(error=error)
                    self._update_connection_status(
                        TransportStatus.ERROR, message
                    )
                    raise DeviceConnectionError(message) from error
                logger.warning(
                    "Ruida program transport reported an error after closing: "
                    "%s",
                    error,
                    extra=self._log_extra("MACHINE_EVENT"),
                )

            if getattr(client, "is_open", False) or getattr(
                client, "is_ready", False
            ):
                message = _("The Ruida controller connection did not close.")
                self._update_connection_status(TransportStatus.ERROR, message)
                raise DeviceConnectionError(message)

        await self._finalize_cleanup()

    async def _finalize_cleanup(self) -> None:
        self._client = None
        self._transport = None
        self._resource = None
        self._update_connection_status(TransportStatus.DISCONNECTED)
        await super().cleanup()

    async def _call_blocking(
        self,
        operation: Callable[..., Any],
        *args: Any,
        _preserve_cancellation_on_error: bool = False,
        **kwargs: Any,
    ) -> Any:
        async with self._io_lock:
            worker = asyncio.create_task(
                asyncio.to_thread(operation, *args, **kwargs)
            )
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancelled = True
                    continue
                except BaseException as error:
                    if cancelled and _preserve_cancellation_on_error:
                        raise asyncio.CancelledError from error
                    raise
                break
            if cancelled:
                raise asyncio.CancelledError
            return result

    def _decode_payload(self, payload: bytes | None) -> Any:
        if not isinstance(payload, bytes) or not payload:
            raise DeviceConnectionError(
                _("The encoded job has no Ruida .rd program payload.")
            )

        try:
            module = _load_ruida_re()
            codec = module.RuidaCodec(context="job")
            program = codec.decode(payload, container="rd")
            if not program.records:
                raise ValueError("the program contains no commands")
            if program.issues:
                issues = "; ".join(program.issues[:3])
                raise ValueError(f"the decoder reported: {issues}")
            encoded = codec.encode(
                program,
                container="rd",
                checksum_policy="recompute",
            )
            if encoded != payload:
                raise ValueError(
                    "the program is not an exact canonical .rd file"
                )
        except DeviceConnectionError:
            raise
        except Exception as error:
            message = _("Invalid Ruida .rd program payload: {error}")
            raise DeviceConnectionError(message.format(error=error)) from error
        return program

    async def run(
        self,
        encoded: EncodedOutput,
        doc: Doc,
        ops: Ops,
        on_command_done: Callable[[int], None | Awaitable[None]] | None = None,
    ) -> None:
        del doc, ops
        program = self._decode_payload(encoded.payload)
        try:
            validate_ruida_program_bounds(program, self._machine)
        except RuidaEncodingError as error:
            message = _("Unsafe Ruida job rejected: {error}")
            raise DeviceConnectionError(message.format(error=error)) from error
        if self._program_active:
            raise DeviceConnectionError(
                _("A Ruida program transfer is already in progress.")
            )
        self._program_active = True
        try:
            await self._transfer_program(program, encoded, on_command_done)
        finally:
            self._program_active = False

    async def _transfer_program(
        self,
        program: Any,
        encoded: EncodedOutput,
        on_command_done: Callable[[int], None | Awaitable[None]] | None,
    ) -> None:
        if self._execution_unconfirmed:
            raise ExecutionCompletionUnknownError(
                _(
                    "The previous Ruida program may still be executing. "
                    "Reconnect only after the controller is visibly idle."
                )
            )
        client = self._client
        if client is None or not getattr(client, "is_ready", False):
            raise DeviceConnectionError(
                _("The Ruida controller is not connected.")
            )

        for warning in encoded.warnings:
            logger.warning(
                "Ruida encoder warning: %s",
                warning,
                extra=self._log_extra("USER_COMMAND"),
            )

        transfer_started = False

        def send_job() -> Any:
            nonlocal transfer_started
            transfer_started = True
            return client.send_job(program)

        try:
            receipt = await self._call_blocking(send_job)
        except asyncio.CancelledError:
            if transfer_started:
                self._execution_unconfirmed = True
            raise
        except Exception as error:
            if transfer_started:
                self._execution_unconfirmed = True
            if not getattr(client, "is_ready", False):
                self._update_connection_status(
                    TransportStatus.ERROR, str(error)
                )
            message = _("Ruida program transfer failed: {error}")
            error_cls = (
                ExecutionCompletionUnknownError
                if transfer_started
                else DeviceConnectionError
            )
            raise error_cls(message.format(error=error)) from error

        self._execution_unconfirmed = True
        try:
            completed_packets = receipt.completed_packets
            retries = receipt.retries
        except Exception as error:
            message = _(
                "Ruida program transfer returned an invalid receipt: {error}"
            )
            raise ExecutionCompletionUnknownError(
                message.format(error=error)
            ) from error

        logger.info(
            "Ruida program transfer completed: %d packet(s), %d retry(s). "
            "Controller execution is not monitored.",
            completed_packets,
            retries,
            extra=self._log_extra("USER_COMMAND"),
        )
        await self._report_transferred_ops(encoded, on_command_done)

    async def _report_transferred_ops(
        self,
        encoded: EncodedOutput,
        callback: Callable[[int], None | Awaitable[None]] | None,
    ) -> None:
        if callback is None or encoded.op_map is None:
            return
        for op_index in sorted(encoded.op_map.op_to_machine_code):
            try:
                result = callback(op_index)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug(
                    "Ruida transfer callback raised for op %d",
                    op_index,
                    exc_info=True,
                )

    async def run_raw(self, machine_code: str) -> None:
        del machine_code
        self._raise_unsupported(_("raw commands"))

    async def set_hold(self, hold: bool = True) -> None:
        del hold
        self._raise_unsupported(_("hold and resume"))

    async def cancel(self) -> None:
        client = self._client
        if client is None or not getattr(client, "is_ready", False):
            raise DeviceConnectionError(
                _("The Ruida controller is not connected.")
            )

        self._execution_unconfirmed = True
        try:
            receipt = await self._call_blocking(client.stop_process)
            completed_packets = receipt.completed_packets
            retries = receipt.retries
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not getattr(client, "is_ready", False):
                self._update_connection_status(
                    TransportStatus.ERROR, str(error)
                )
            message = _("Ruida stop command failed: {error}")
            raise DeviceConnectionError(message.format(error=error)) from error

        logger.info(
            "Ruida stop command transferred: %d packet(s), %d retry(s). "
            "Controller execution is not monitored.",
            completed_packets,
            retries,
            extra=self._log_extra("USER_COMMAND"),
        )

    def can_home(self, axis: Axis | None = None) -> bool:
        del axis
        return False

    async def home(self, axes: Axis | None = None) -> None:
        del axes
        self._raise_unsupported(_("homing"))

    async def move_to(self, pos_x: float, pos_y: float) -> None:
        del pos_x, pos_y
        self._raise_unsupported(_("direct movement"))

    async def select_tool(self, tool_number: int) -> None:
        del tool_number
        self._raise_unsupported(_("runtime tool selection"))

    async def read_settings(self) -> None:
        self.settings_read.send(self, settings=[])

    def get_setting_vars(self) -> list[VarSet]:
        return []

    async def write_setting(self, key: str, value: Any) -> None:
        del key, value
        self._raise_unsupported(_("controller settings"))

    async def clear_alarm(self) -> None:
        self._raise_unsupported(_("alarm control"))

    async def set_power(self, head: Laser, percent: float) -> None:
        del head, percent
        self._raise_unsupported(_("immediate power control"))

    async def set_focus_power(self, head: Laser, percent: float) -> None:
        del head, percent
        self._raise_unsupported(_("focus power control"))

    def can_jog(self, axis: Axis | None = None) -> bool:
        del axis
        return False

    async def jog(self, speed: int, **deltas: float) -> None:
        del speed, deltas
        self._raise_unsupported(_("jogging"))

    async def set_wcs_offset(
        self, wcs_slot: str, x: float, y: float, z: float
    ) -> None:
        del wcs_slot, x, y, z
        self._raise_unsupported(_("work coordinate management"))

    async def read_wcs_offsets(self) -> dict[str, Pos]:
        offsets: dict[str, Pos] = {self.machine_space_wcs: (0.0, 0.0, 0.0)}
        self.wcs_updated.send(self, offsets=offsets)
        return offsets

    async def read_parser_state(self) -> str | None:
        return self.machine_space_wcs

    async def select_wcs(self, wcs: str) -> None:
        if wcs != self.machine_space_wcs:
            self._raise_unsupported(_("work coordinate management"))

    async def run_probe_cycle(
        self, axis: Axis, max_travel: float, feed_rate: int
    ) -> Pos | None:
        del axis, max_travel, feed_rate
        self._raise_unsupported(_("probing"))

    def _raise_unsupported(self, feature: str) -> NoReturn:
        message = _(
            "Ruida program transfer does not support {feature}."
        ).format(feature=feature)
        raise DeviceConnectionError(message)

    def _update_connection_status(
        self, status: TransportStatus, message: str = ""
    ) -> None:
        self.state.status = DeviceStatus.UNKNOWN
        self.state_changed.send(self, state=self.state)
        self.connection_status_changed.send(
            self, status=status, message=message
        )
