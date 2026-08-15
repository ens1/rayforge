"""Evidence-backed Ruida program transfer driver foundation."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import sys
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import replace
from gettext import gettext as _
from time import monotonic
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
    JobCancelledError,
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


RUIDA_MACHINE_STATUS_PROFILE_KEY = "machine_status_profile"
BOSS_LS2040_DA000400_STATUS_PROFILE = "boss-ls2040-da000400-v1"
_BOSS_IDLE_STATUS_WORDS = frozenset((0, 0x10600))
_BOSS_ACTIVE_STATUS_WORDS = frozenset(
    (
        0x10401,
        0x10403,
        0x10405,
        0x410403,
        0x830401,
        0x510600,
    )
)
_BOSS_POST_EXECUTION_IDLE_WORD = 0x10600
_BOSS_PAUSED_STATUS_WORD = 0x10403
_BOSS_STOP_TRANSITION_WORD = 0x510600


class _StatusReconnectAbortedError(DeviceConnectionError):
    pass


class _StatusReconnectCloseError(DeviceConnectionError):
    pass


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
    """Transfer complete ``.rd`` programs and monitor controller status."""

    supports_settings = False
    reports_granular_progress = False
    supports_hold = False
    supports_cancel = True
    uses_gcode = False
    accepts_arc_ops = False
    accepts_curve_ops = False
    maturity = DriverMaturity.EXPERIMENTAL
    native_overscan = False
    _probe_on_open = True
    _supports_boss_da000400_status_profile = False
    STATUS_POLL_INTERVAL: float = 0.2
    STATUS_RECONNECT_INITIAL_DELAY: float = 1.0
    STATUS_RECONNECT_MAX_DELAY: float = 5.0
    COMPLETION_IDLE_SAMPLES: int = 3
    ACTIVE_OBSERVATION_IDLE_SAMPLES: int = 25
    STOP_IDLE_MIN_DURATION: float = 0.6
    _execution_completion_monitoring_enabled: bool = False
    _status_semantics_validated: bool = False

    def __init__(self, context: RayforgeContext, machine: Machine):
        super().__init__(context, machine)
        self._completion_capability_override: bool | None = None
        self._transport: Any | None = None
        self._client: Any | None = None
        self._resource: str | None = None
        self._io_lock = asyncio.Lock()
        self._program_active = False
        self._execution_unconfirmed = False
        self._status_task: asyncio.Task | None = None
        self._status_reconnect_task: asyncio.Task | None = None
        self._status_reconnect_generation: int | None = None
        self._session_generation = 0
        self._job_generation = 0
        self._active_job_generation: int | None = None
        self._completion_future: asyncio.Future[None] | None = None
        self._completion_kind: str | None = None
        self._observed_active = False
        self._idle_samples = 0
        self._preactive_idle_samples = 0
        self._cancel_requested_generation: int | None = None
        self._stop_attempted_generation: int | None = None
        self._stop_delivered_generation: int | None = None
        self._stop_idle_started_at: float | None = None
        self._last_raw_status_word: int | None = None

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

    @property
    def reports_device_status(self) -> bool:
        return bool(
            self._reports_device_status
            and self._has_validated_status_semantics
        )

    @reports_device_status.setter
    def reports_device_status(self, value: bool) -> None:
        self._reports_device_status = value

    @property
    def confirms_execution_completion(self) -> bool:
        return self._can_confirm_execution

    @confirms_execution_completion.setter
    def confirms_execution_completion(self, value: bool) -> None:
        self._completion_capability_override = value

    @property
    def _can_confirm_execution(self) -> bool:
        supported = bool(
            self._has_validated_status_semantics
            and (
                self._execution_completion_monitoring_enabled
                or self._configured_status_profile
                == BOSS_LS2040_DA000400_STATUS_PROFILE
            )
        )
        return supported and self._completion_capability_override is not False

    @property
    def _configured_status_profile(self) -> str | None:
        profile = self.config.get(RUIDA_MACHINE_STATUS_PROFILE_KEY)
        return profile if isinstance(profile, str) else None

    @property
    def _has_validated_status_semantics(self) -> bool:
        return bool(
            self._status_semantics_validated
            or (
                self._supports_boss_da000400_status_profile
                and self._configured_status_profile
                == BOSS_LS2040_DA000400_STATUS_PROFILE
            )
        )

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
            required_methods = ["stop_process"]
            if self._has_validated_status_semantics:
                required_methods.append("read_machine_status")
            missing = [
                name
                for name in required_methods
                if not callable(getattr(client, name, None))
            ]
            if missing:
                raise DriverSetupError(
                    "Installed ruida-re lacks required API: "
                    + ", ".join(f"ControllerClient.{name}" for name in missing)
                )
        except DriverSetupError:
            raise
        except Exception as error:
            raise DriverSetupError(str(error)) from error
        self._transport = transport
        self._client = client
        self._resource = resource

    async def _connect_implementation(self) -> None:
        reconnect_task = self._status_reconnect_task
        automatic_reconnect = asyncio.current_task() is reconnect_task
        reconnect_generation = (
            self._status_reconnect_generation if automatic_reconnect else None
        )
        if not automatic_reconnect:
            await self._stop_status_reconnect()

        client = self._client
        if client is None:
            error = DeviceConnectionError(_("Ruida driver is not configured."))
            self._update_connection_status(TransportStatus.ERROR, str(error))
            raise error

        if automatic_reconnect and (
            reconnect_generation is None
            or not self._idle_status_recovery_allowed(
                client,
                reconnect_generation,
            )
        ):
            error = _StatusReconnectAbortedError(
                _("Ruida status reconnect was no longer safe.")
            )
            self._update_connection_status(TransportStatus.ERROR, str(error))
            raise error

        if getattr(client, "is_ready", False):
            if automatic_reconnect:
                closed = await self._close_after_cancelled_open(client)
                error = _StatusReconnectAbortedError(
                    _(
                        "Ruida status reconnect found an unexpected open "
                        "session."
                    )
                )
                if not closed:
                    error = _StatusReconnectCloseError(
                        _("Could not close the unexpected Ruida session.")
                    )
                self._update_connection_status(
                    TransportStatus.ERROR,
                    str(error),
                )
                raise error
            if self._has_validated_status_semantics:
                await self._refresh_status(client)
            self._update_connection_status(TransportStatus.CONNECTED)
            self._start_status_polling(client)
            return

        self._update_connection_status(TransportStatus.CONNECTING)
        try:
            async with self._io_lock:
                if automatic_reconnect and not (
                    self._idle_status_recovery_allowed(
                        client,
                        reconnect_generation,
                    )
                ):
                    raise _StatusReconnectAbortedError(
                        _("Ruida status reconnect was no longer safe.")
                    )
                await self._call_blocking_unlocked(
                    client.open,
                    probe=self._probe_on_open,
                    _preserve_cancellation_on_error=automatic_reconnect,
                )
                if automatic_reconnect and not (
                    self._idle_status_recovery_allowed(
                        client,
                        reconnect_generation,
                    )
                ):
                    raise _StatusReconnectAbortedError(
                        _(
                            "Ruida status reconnect became unsafe after "
                            "opening."
                        )
                    )
                device_status = DeviceStatus.UNKNOWN
                if self._has_validated_status_semantics:
                    status = await self._call_blocking_unlocked(
                        client.read_machine_status,
                        _preserve_cancellation_on_error=(automatic_reconnect),
                    )
                    if automatic_reconnect and not (
                        self._idle_status_recovery_allowed(
                            client,
                            reconnect_generation,
                        )
                    ):
                        raise _StatusReconnectAbortedError(
                            _(
                                "Ruida status reconnect became unsafe while "
                                "validating status."
                            )
                        )
                    device_status = self._publish_machine_status(status)
                    if automatic_reconnect and not (
                        self._idle_status_recovery_allowed(
                            client,
                            reconnect_generation,
                        )
                    ):
                        raise _StatusReconnectAbortedError(
                            _(
                                "Ruida status reconnect became unsafe while "
                                "publishing status."
                            )
                        )
        except asyncio.CancelledError:
            closed = await self._close_after_cancelled_open(client)
            if not closed:
                self._update_connection_status(
                    TransportStatus.ERROR,
                    _("Could not close the cancelled Ruida connection."),
                )
            else:
                self._update_connection_status(TransportStatus.DISCONNECTED)
            raise
        except Exception as error:
            closed = await self._close_after_cancelled_open(client)
            if automatic_reconnect and not closed:
                close_error = _StatusReconnectCloseError(
                    _("Could not close the failed Ruida status reconnect.")
                )
                self._update_connection_status(
                    TransportStatus.ERROR,
                    str(close_error),
                )
                raise close_error from error
            if isinstance(error, _StatusReconnectAbortedError):
                self._update_connection_status(
                    TransportStatus.ERROR,
                    str(error),
                )
                raise
            message = _("Could not connect to the Ruida controller: {error}")
            wrapped = DeviceConnectionError(message.format(error=error))
            reconnect_status = (
                TransportStatus.SLEEPING
                if automatic_reconnect
                else TransportStatus.ERROR
            )
            self._update_connection_status(reconnect_status, str(wrapped))
            raise wrapped from error

        self._session_generation += 1
        if not automatic_reconnect and (
            not self._has_validated_status_semantics
            or device_status == DeviceStatus.IDLE
        ):
            self._execution_unconfirmed = False
        self._update_connection_status(TransportStatus.CONNECTED)
        self._start_status_polling(client)

    async def _close_after_cancelled_open(self, client: Any) -> bool:
        if not (
            getattr(client, "is_open", False)
            or getattr(client, "is_ready", False)
        ):
            return True
        try:
            await self._call_blocking(client.close)
        except Exception as error:  # noqa: BLE001 - cancellation cleanup
            logger.warning(
                "Could not close cancelled Ruida program transport: %s",
                error,
                extra=self._log_extra("MACHINE_EVENT"),
            )
            return False
        return not (
            getattr(client, "is_open", False)
            or getattr(client, "is_ready", False)
        )

    async def cleanup(self) -> None:
        client = self._client
        await self._stop_status_polling()
        await self._stop_status_reconnect()
        self._session_generation += 1
        if self._active_job_generation is not None:
            self._execution_unconfirmed = True
            self._fail_completion(
                self._active_job_generation,
                ExecutionCompletionUnknownError(
                    _("Ruida status monitoring stopped before completion.")
                ),
            )
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
        await self._stop_status_polling()
        await self._stop_status_reconnect()
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
            return await self._call_blocking_unlocked(
                operation,
                *args,
                _preserve_cancellation_on_error=(
                    _preserve_cancellation_on_error
                ),
                **kwargs,
            )

    async def _call_blocking_unlocked(
        self,
        operation: Callable[..., Any],
        *args: Any,
        _preserve_cancellation_on_error: bool = False,
        **kwargs: Any,
    ) -> Any:
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

    async def _refresh_status(self, client: Any) -> DeviceStatus:
        if not self._has_validated_status_semantics:
            self._set_device_status(DeviceStatus.UNKNOWN)
            return DeviceStatus.UNKNOWN
        status = await self._call_blocking(client.read_machine_status)
        return self._publish_machine_status(status)

    def _start_status_polling(self, client: Any) -> None:
        if not self._has_validated_status_semantics:
            return
        task = self._status_task
        if task is not None and not task.done():
            return
        session_generation = self._session_generation
        self._status_task = asyncio.create_task(
            self._status_poll_loop(client, session_generation),
            name="ruida-status-poll",
        )

    async def _stop_status_polling(self) -> None:
        task = self._status_task
        self._status_task = None
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug(
                "Ruida status task failed during cleanup", exc_info=True
            )

    async def _stop_status_reconnect(self) -> None:
        task = self._status_reconnect_task
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug(
                "Ruida status reconnect task failed during cleanup",
                exc_info=True,
            )
        finally:
            if self._status_reconnect_task is task:
                self._status_reconnect_task = None
                self._status_reconnect_generation = None

    async def _status_poll_loop(
        self,
        client: Any,
        session_generation: int,
    ) -> None:
        try:
            while self._status_session_is_current(
                client,
                session_generation,
            ) and getattr(client, "is_ready", False):
                await asyncio.sleep(self.STATUS_POLL_INTERVAL)
                observed_generation = self._active_job_generation
                try:
                    status = await self._call_blocking(
                        client.read_machine_status
                    )
                    if (
                        client is not self._client
                        or session_generation != self._session_generation
                    ):
                        return
                    self._publish_machine_status(status)
                    self._process_completion_status(
                        status,
                        observed_generation,
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    if not isinstance(error, Exception):
                        raise
                    await self._handle_status_failure(
                        client,
                        session_generation,
                        error,
                    )
                    return
        finally:
            if self._status_task is asyncio.current_task():
                self._status_task = None

    async def _handle_status_failure(
        self,
        client: Any,
        session_generation: int,
        error: BaseException,
    ) -> None:
        if not self._status_session_is_current(client, session_generation):
            return
        logger.warning(
            "Ruida status poll failed: %s",
            error,
            extra=self._log_extra("MACHINE_EVENT"),
        )
        self._set_device_status(DeviceStatus.UNKNOWN)
        active_generation = self._active_job_generation
        if active_generation is not None:
            self._execution_unconfirmed = True
            message = _(
                "Ruida status became unavailable before execution "
                "completion was confirmed: {error}"
            )
            self._fail_completion(
                active_generation,
                ExecutionCompletionUnknownError(message.format(error=error)),
            )
        if (
            active_generation is not None
            or self._program_active
            or self._execution_unconfirmed
        ):
            self._update_connection_status(TransportStatus.ERROR, str(error))
            await self._close_failed_status_client(client)
            return

        closed = await self._close_failed_status_client(client)
        if not closed:
            message = _(
                "Could not fully close the Ruida connection after a status "
                "failure."
            )
            self._update_connection_status(TransportStatus.ERROR, message)
            return
        if not self._idle_status_recovery_allowed(
            client,
            session_generation,
        ):
            self._update_connection_status(TransportStatus.ERROR, str(error))
            return
        self._update_connection_status(
            TransportStatus.SLEEPING,
            str(error),
        )
        self._schedule_status_reconnect(
            client,
            session_generation,
            asyncio.current_task(),
        )

    def _status_session_is_current(
        self,
        client: Any,
        session_generation: int,
    ) -> bool:
        return bool(
            client is self._client
            and session_generation == self._session_generation
            and self._has_validated_status_semantics
        )

    def _idle_status_recovery_allowed(
        self,
        client: Any,
        session_generation: int | None,
    ) -> bool:
        return bool(
            session_generation is not None
            and self._status_session_is_current(client, session_generation)
            and self._active_job_generation is None
            and not self._program_active
            and not self._execution_unconfirmed
        )

    async def _close_failed_status_client(self, client: Any) -> bool:
        try:
            await self._call_blocking(client.close)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Could not close Ruida connection after status failure",
                extra=self._log_extra("MACHINE_EVENT"),
                exc_info=error,
            )
            return False
        if getattr(client, "is_open", False) or getattr(
            client,
            "is_ready",
            False,
        ):
            logger.warning(
                "Ruida connection remained open after status failure",
                extra=self._log_extra("MACHINE_EVENT"),
            )
            return False
        return True

    def _schedule_status_reconnect(
        self,
        client: Any,
        session_generation: int,
        failed_poll_task: asyncio.Task | None,
    ) -> None:
        task = self._status_reconnect_task
        if task is not None and not task.done():
            return
        self._status_reconnect_generation = session_generation
        self._status_reconnect_task = asyncio.create_task(
            self._recover_idle_status_session(
                client,
                session_generation,
                failed_poll_task,
            ),
            name="ruida-status-reconnect",
        )

    async def _recover_idle_status_session(
        self,
        client: Any,
        session_generation: int,
        failed_poll_task: asyncio.Task | None,
    ) -> None:
        delay = self.STATUS_RECONNECT_INITIAL_DELAY
        current_task = asyncio.current_task()
        try:
            if (
                failed_poll_task is not None
                and failed_poll_task is not current_task
            ):
                await asyncio.shield(failed_poll_task)
            while self._idle_status_recovery_allowed(
                client,
                session_generation,
            ):
                await asyncio.sleep(delay)
                if not self._idle_status_recovery_allowed(
                    client,
                    session_generation,
                ):
                    return
                if getattr(client, "is_open", False) or getattr(
                    client,
                    "is_ready",
                    False,
                ):
                    message = _(
                        "Ruida status reconnect stopped because the failed "
                        "session remained open."
                    )
                    self._update_connection_status(
                        TransportStatus.ERROR,
                        message,
                    )
                    return
                try:
                    await self.connect()
                except asyncio.CancelledError:
                    raise
                except (
                    _StatusReconnectAbortedError,
                    _StatusReconnectCloseError,
                ) as error:
                    logger.warning(
                        "Ruida status reconnect stopped: %s",
                        error,
                        extra=self._log_extra("MACHINE_EVENT"),
                    )
                    return
                except BaseException as error:
                    if not isinstance(error, Exception):
                        raise
                    logger.warning(
                        "Ruida status reconnect failed: %s",
                        error,
                        extra=self._log_extra("MACHINE_EVENT"),
                    )
                    if getattr(client, "is_open", False) or getattr(
                        client,
                        "is_ready",
                        False,
                    ):
                        self._update_connection_status(
                            TransportStatus.ERROR,
                            _(
                                "Ruida status reconnect left the failed "
                                "session open."
                            ),
                        )
                        return
                    if not self._idle_status_recovery_allowed(
                        client,
                        session_generation,
                    ):
                        return
                    self._update_connection_status(
                        TransportStatus.SLEEPING,
                        str(error),
                    )
                    delay = min(
                        delay * 2,
                        self.STATUS_RECONNECT_MAX_DELAY,
                    )
                    continue
                return
        finally:
            if self._status_reconnect_task is current_task:
                self._status_reconnect_task = None
                self._status_reconnect_generation = None

    def _status_fields(
        self,
        status: Any,
    ) -> tuple[int, bool, bool, bool, int]:
        try:
            raw_word = status.raw_word
            moving = status.moving
            job_running = status.job_running
            part_end = status.part_end
            unknown_bits = status.unknown_bits
        except AttributeError as error:
            raise DeviceConnectionError(
                _("The Ruida controller returned malformed machine status.")
            ) from error
        if (
            isinstance(raw_word, bool)
            or not isinstance(raw_word, int)
            or not isinstance(moving, bool)
            or not isinstance(job_running, bool)
            or not isinstance(part_end, bool)
            or isinstance(unknown_bits, bool)
            or not isinstance(unknown_bits, int)
            or raw_word < 0
            or unknown_bits < 0
        ):
            raise DeviceConnectionError(
                _("The Ruida controller returned malformed machine status.")
            )
        return raw_word, moving, job_running, part_end, unknown_bits

    def _publish_machine_status(self, status: Any) -> DeviceStatus:
        (
            raw_word,
            moving,
            job_running,
            part_end,
            unknown_bits,
        ) = self._status_fields(status)
        device_status = self._device_status_for_word(raw_word)
        if raw_word != self._last_raw_status_word:
            logger.info(
                "Ruida machine status: raw=0x%09x moving=%s "
                "job_running=%s part_end=%s unknown=0x%09x",
                raw_word,
                moving,
                job_running,
                part_end,
                unknown_bits,
                extra=self._log_extra("MACHINE_EVENT"),
            )
            self._last_raw_status_word = raw_word
        self._set_device_status(device_status)
        return device_status

    def _device_status_for_word(self, raw_word: int) -> DeviceStatus:
        if not self._has_validated_status_semantics:
            return DeviceStatus.UNKNOWN
        if raw_word in _BOSS_IDLE_STATUS_WORDS:
            return DeviceStatus.IDLE
        if raw_word == _BOSS_PAUSED_STATUS_WORD:
            return DeviceStatus.HOLD
        if raw_word in _BOSS_ACTIVE_STATUS_WORDS:
            return DeviceStatus.RUN
        return DeviceStatus.UNKNOWN

    def _set_device_status(self, status: DeviceStatus) -> None:
        state = replace(self.state, status=status)
        if state == self.state:
            return
        self.state = state
        self.state_changed.send(self, state=state)

    def _begin_completion(self, kind: str) -> tuple[int, asyncio.Future[None]]:
        if self._active_job_generation is not None:
            raise DeviceConnectionError(
                _("A Ruida execution monitor is already active.")
            )
        self._job_generation += 1
        generation = self._job_generation
        future = asyncio.get_running_loop().create_future()
        self._active_job_generation = generation
        self._completion_future = future
        self._completion_kind = kind
        self._observed_active = False
        self._idle_samples = 0
        self._preactive_idle_samples = 0
        self._cancel_requested_generation = None
        self._stop_delivered_generation = None
        self._stop_idle_started_at = None
        return generation, future

    def _process_completion_status(
        self,
        status: Any,
        observed_generation: int | None,
    ) -> None:
        if (
            observed_generation is None
            or observed_generation != self._active_job_generation
        ):
            return
        try:
            raw_word, moving, job_running, part_end, unknown_bits = (
                self._status_fields(status)
            )
            del moving, job_running, part_end, unknown_bits
        except DeviceConnectionError as error:
            self._execution_unconfirmed = True
            self._fail_completion(observed_generation, error)
            return
        if self._device_status_for_word(raw_word) == DeviceStatus.UNKNOWN:
            self._fail_unknown_completion_word(
                observed_generation,
                raw_word,
            )
            return

        if self._cancel_requested_generation == observed_generation:
            if self._stop_delivered_generation == observed_generation:
                self._process_stop_status(observed_generation, raw_word)
                return
            if (
                raw_word in _BOSS_ACTIVE_STATUS_WORDS
                and raw_word != _BOSS_STOP_TRANSITION_WORD
            ):
                self._observed_active = True
                self._preactive_idle_samples = 0
            self._reset_completion_idle_observation()
            return
        if raw_word == _BOSS_STOP_TRANSITION_WORD:
            self._execution_unconfirmed = True
            message = _(
                "Ruida reported a stop transition outside a confirmed "
                "Rayforge Stop sequence. Confirm that the controller is "
                "idle before reconnecting."
            )
            self._fail_completion(
                observed_generation,
                ExecutionCompletionUnknownError(message),
            )
            return
        if raw_word in _BOSS_ACTIVE_STATUS_WORDS:
            self._observed_active = True
            self._preactive_idle_samples = 0
            self._reset_completion_idle_observation()
            return
        if not self._observed_active:
            self._preactive_idle_samples += 1
            if (
                self._preactive_idle_samples
                >= self.ACTIVE_OBSERVATION_IDLE_SAMPLES
            ):
                self._execution_unconfirmed = True
                message = _(
                    "Ruida never reported this program as active. It may "
                    "have completed between status polls or may not have "
                    "started. Confirm that the controller is idle before "
                    "reconnecting."
                )
                self._fail_completion(
                    observed_generation,
                    ExecutionCompletionUnknownError(message),
                )
            return
        if raw_word != _BOSS_POST_EXECUTION_IDLE_WORD:
            self._execution_unconfirmed = True
            message = _(
                "Ruida did not report the validated post-execution idle "
                "word. Confirm that the controller is idle before "
                "reconnecting."
            )
            self._fail_completion(
                observed_generation,
                ExecutionCompletionUnknownError(message),
            )
            return
        self._idle_samples += 1
        if self._idle_samples >= self.COMPLETION_IDLE_SAMPLES:
            self._complete_generation(observed_generation)

    def _process_stop_status(
        self,
        generation: int,
        raw_word: int,
    ) -> None:
        if self._stop_delivered_generation != generation:
            self._reset_completion_idle_observation()
            return
        if raw_word in _BOSS_ACTIVE_STATUS_WORDS:
            self._reset_completion_idle_observation()
            return
        if raw_word != _BOSS_POST_EXECUTION_IDLE_WORD:
            self._execution_unconfirmed = True
            message = _(
                "Ruida did not report the validated post-Stop idle word. "
                "Confirm that the controller is idle before reconnecting."
            )
            self._fail_completion(
                generation,
                ExecutionCompletionUnknownError(message),
            )
            return
        now = monotonic()
        if self._stop_idle_started_at is None:
            self._stop_idle_started_at = now
        self._idle_samples += 1
        stable_duration = now - self._stop_idle_started_at
        if (
            self._idle_samples >= self.COMPLETION_IDLE_SAMPLES
            and stable_duration >= self.STOP_IDLE_MIN_DURATION
        ):
            self._complete_generation(generation)

    def _fail_unknown_completion_word(
        self,
        generation: int,
        raw_word: int,
    ) -> None:
        self._execution_unconfirmed = True
        message = _(
            "Ruida reported unvalidated machine status 0x{raw_word:x} "
            "while execution was being monitored. Confirm that the "
            "controller is idle before reconnecting."
        )
        self._fail_completion(
            generation,
            ExecutionCompletionUnknownError(message.format(raw_word=raw_word)),
        )

    def _reset_completion_idle_observation(self) -> None:
        self._idle_samples = 0
        self._stop_idle_started_at = None

    def _complete_generation(self, generation: int) -> None:
        if generation != self._active_job_generation:
            return
        future = self._completion_future
        kind = self._completion_kind
        was_cancelled = self._cancel_requested_generation == generation
        self._clear_completion(generation)
        self._execution_unconfirmed = False
        if kind == "job" and not was_cancelled:
            self.job_finished.send(self)
        if future is None or future.done():
            return
        if was_cancelled:
            future.set_exception(
                JobCancelledError(_("The Ruida program was cancelled."))
            )
        else:
            future.set_result(None)

    def _fail_completion(
        self,
        generation: int,
        error: BaseException,
    ) -> None:
        if generation != self._active_job_generation:
            return
        future = self._completion_future
        self._clear_completion(generation)
        if future is not None and not future.done():
            future.set_exception(error)

    def _abandon_completion(self, generation: int) -> None:
        if generation != self._active_job_generation:
            return
        future = self._completion_future
        self._clear_completion(generation)
        if future is not None and not future.done():
            future.cancel()

    def _clear_completion(self, generation: int) -> None:
        if generation != self._active_job_generation:
            return
        self._active_job_generation = None
        self._completion_future = None
        self._completion_kind = None
        self._observed_active = False
        self._idle_samples = 0
        self._preactive_idle_samples = 0
        self._cancel_requested_generation = None
        self._stop_delivered_generation = None
        self._stop_idle_started_at = None

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
        completion_generation: int | None = None
        completion_future: asyncio.Future[None] | None = None

        def send_job() -> Any:
            nonlocal transfer_started
            transfer_started = True
            return client.send_job(program)

        try:
            async with self._io_lock:
                if self._has_validated_status_semantics:
                    status = await self._call_blocking_unlocked(
                        client.read_machine_status
                    )
                    device_status = self._publish_machine_status(status)
                    if device_status == DeviceStatus.UNKNOWN:
                        raise DeviceConnectionError(
                            _(
                                "Ruida machine status is ambiguous. No "
                                "program was transferred."
                            )
                        )
                    if device_status != DeviceStatus.IDLE:
                        raise DeviceConnectionError(
                            _(
                                "The Ruida controller program is active. "
                                "Wait for it to become idle before sending "
                                "another program."
                            )
                        )
                if self.confirms_execution_completion:
                    completion_generation, completion_future = (
                        self._begin_completion("job")
                    )
                else:
                    self._job_generation += 1
                self._execution_unconfirmed = True
                receipt = await self._call_blocking_unlocked(send_job)
        except asyncio.CancelledError:
            if transfer_started:
                self._execution_unconfirmed = True
            if completion_generation is not None:
                self._abandon_completion(completion_generation)
            raise
        except Exception as error:
            if transfer_started:
                self._execution_unconfirmed = True
            elif completion_generation is not None:
                self._execution_unconfirmed = False
            if completion_generation is not None:
                self._abandon_completion(completion_generation)
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
            if completion_generation is not None:
                self._abandon_completion(completion_generation)
            message = _(
                "Ruida program transfer returned an invalid receipt: {error}"
            )
            raise ExecutionCompletionUnknownError(
                message.format(error=error)
            ) from error

        if not self.confirms_execution_completion:
            logger.info(
                "Ruida program transfer completed: %d packet(s), %d "
                "retry(s). Execution completion remains "
                "operator-confirmed.",
                completed_packets,
                retries,
                extra=self._log_extra("USER_COMMAND"),
            )
            await self._report_transferred_ops(encoded, on_command_done)
            return

        if completion_generation is None or completion_future is None:
            raise RuntimeError("Ruida completion monitor was not initialized")
        self._start_status_polling(client)
        logger.info(
            "Ruida program transfer completed: %d packet(s), %d retry(s). "
            "Waiting for a confirmed active-to-idle transition.",
            completed_packets,
            retries,
            extra=self._log_extra("USER_COMMAND"),
        )
        try:
            await completion_future
        except asyncio.CancelledError:
            self._execution_unconfirmed = True
            self._abandon_completion(completion_generation)
            raise
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

    def notify_cancel_requested(self) -> None:
        self._execution_unconfirmed = True
        generation = self._active_job_generation
        if (
            generation is not None
            and self._cancel_requested_generation != generation
        ):
            self._cancel_requested_generation = generation
            self._reset_completion_idle_observation()

    async def cancel(self) -> None:
        client = self._client
        if client is None or not getattr(client, "is_ready", False):
            raise DeviceConnectionError(
                _("The Ruida controller is not connected.")
            )

        self.notify_cancel_requested()
        completion_generation: int | None = None
        completion_future: asyncio.Future[None] | None = None
        can_confirm_stop = False
        owns_stop_attempt = False
        duplicate_stop_attempt = False
        completed_packets = 0
        retries = 0
        try:
            async with self._io_lock:
                completion_generation = self._active_job_generation
                completion_future = self._completion_future
                stop_generation = (
                    completion_generation
                    if completion_generation is not None
                    else self._job_generation
                )
                duplicate_stop_attempt = (
                    self._stop_attempted_generation == stop_generation
                )
                if not duplicate_stop_attempt:
                    self._stop_attempted_generation = stop_generation
                    owns_stop_attempt = True
                can_confirm_stop = bool(
                    self.confirms_execution_completion
                    and completion_generation is not None
                    and completion_future is not None
                    and self._completion_kind == "job"
                    and self._observed_active
                    and self._cancel_requested_generation
                    == completion_generation
                )
                if owns_stop_attempt:
                    receipt = await self._call_blocking_unlocked(
                        self._stop_process_once,
                        client,
                    )
                    completed_packets, retries = self._validate_stop_receipt(
                        receipt
                    )
                    if can_confirm_stop:
                        self._stop_delivered_generation = completion_generation
                        self._reset_completion_idle_observation()
        except asyncio.CancelledError:
            self._execution_unconfirmed = True
            if (
                owns_stop_attempt
                and completion_generation is not None
                and self._stop_delivered_generation != completion_generation
            ):
                self._abandon_completion(completion_generation)
            raise
        except Exception as error:
            if owns_stop_attempt and completion_generation is not None:
                self._execution_unconfirmed = True
                self._abandon_completion(completion_generation)
            if not getattr(client, "is_ready", False):
                self._update_connection_status(
                    TransportStatus.ERROR, str(error)
                )
            message = _("Ruida stop command failed: {error}")
            raise DeviceConnectionError(message.format(error=error)) from error

        if duplicate_stop_attempt:
            if completion_future is not None:
                await self._await_stop_completion(completion_future)
                return
            if not self._execution_unconfirmed:
                return
            raise ExecutionCompletionUnknownError(
                _(
                    "Ruida Stop delivery was already attempted for this "
                    "program. Confirm that the controller is idle before "
                    "reconnecting."
                )
            )

        if not can_confirm_stop:
            self._execution_unconfirmed = True
            if completion_generation is not None:
                message = _(
                    "Ruida Stop was sent before this program had a "
                    "same-generation active status observation. Confirm "
                    "that the controller is idle before reconnecting."
                )
                self._fail_completion(
                    completion_generation,
                    ExecutionCompletionUnknownError(message),
                )
            logger.info(
                "Ruida stop command transferred: %d packet(s), %d "
                "retry(s). Stopped execution remains "
                "operator-confirmed.",
                completed_packets,
                retries,
                extra=self._log_extra("USER_COMMAND"),
            )
            return

        if completion_generation is None or completion_future is None:
            raise RuntimeError("Ruida stop monitor was not initialized")
        self._start_status_polling(client)
        logger.info(
            "Ruida stop command transferred: %d packet(s), %d retry(s). "
            "Waiting for validated stable post-Stop idle status.",
            completed_packets,
            retries,
            extra=self._log_extra("USER_COMMAND"),
        )
        await self._await_stop_completion(completion_future)

    async def _await_stop_completion(
        self,
        completion_future: asyncio.Future[None],
    ) -> None:
        try:
            await asyncio.shield(completion_future)
        except JobCancelledError:
            return
        except asyncio.CancelledError:
            self._execution_unconfirmed = True
            task = asyncio.current_task()
            if completion_future.cancelled() and not (
                task is not None and task.cancelling()
            ):
                raise ExecutionCompletionUnknownError(
                    _(
                        "Ruida Stop completion became ambiguous. Confirm "
                        "that the controller is idle before reconnecting."
                    )
                ) from None
            raise

    @staticmethod
    def _stop_process_once(client: Any) -> Any:
        profile = getattr(client, "handshake_profile", None)
        if profile is None or not hasattr(profile, "max_retries"):
            raise DeviceConnectionError(
                _("The Ruida client cannot guarantee a no-retry Stop send.")
            )
        try:
            no_retry_profile = replace(profile, max_retries=0)
        except TypeError as error:
            raise DeviceConnectionError(
                _("The Ruida client cannot guarantee a no-retry Stop send.")
            ) from error
        client.handshake_profile = no_retry_profile
        try:
            return client.stop_process()
        finally:
            client.handshake_profile = profile

    def _validate_stop_receipt(self, receipt: Any) -> tuple[int, int]:
        try:
            completed_packets = receipt.completed_packets
            retries = receipt.retries
            transmissions = receipt.transmissions
            packets = receipt.packets
        except AttributeError as error:
            raise DeviceConnectionError(
                _("Ruida Stop returned an invalid transfer receipt.")
            ) from error
        if (
            type(completed_packets) is not int
            or type(retries) is not int
            or type(transmissions) is not int
            or not isinstance(packets, tuple)
            or len(packets) != 1
            or completed_packets != 1
            or retries != 0
            or transmissions != 1
        ):
            raise DeviceConnectionError(
                _("Ruida Stop was not delivered as exactly one attempt.")
            )
        is_scoped_serial = bool(
            self._supports_boss_da000400_status_profile
            and self._configured_status_profile
            == BOSS_LS2040_DA000400_STATUS_PROFILE
        )
        if is_scoped_serial and packets != (bytes.fromhex("d209"),):
            raise DeviceConnectionError(
                _("Ruida Stop returned unexpected serial wire bytes.")
            )
        return completed_packets, retries

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
        if status != TransportStatus.CONNECTED:
            self._set_device_status(DeviceStatus.UNKNOWN)
        self.connection_status_changed.send(
            self, status=status, message=message
        )
