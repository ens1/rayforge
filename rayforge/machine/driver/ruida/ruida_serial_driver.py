"""Ruida USB serial program transfer driver."""

from __future__ import annotations

from gettext import gettext as _
from typing import Any

from ....core.varset import BaudrateVar, SerialPortVar, VarSet
from ..driver import DriverSetupError
from .program_driver import RuidaProgramDriver


class RuidaSerialDriver(RuidaProgramDriver):
    """Transfer complete Ruida programs over USB serial."""

    label = _("Ruida (USB Serial)")
    subtitle = _("Send complete Ruida programs over USB serial")
    _probe_on_open = False

    @classmethod
    def precheck(cls, **kwargs: Any) -> None:
        del kwargs

    @classmethod
    def get_setup_vars(cls) -> VarSet:
        return VarSet(
            vars=[
                SerialPortVar(
                    key="port",
                    label=_("Port"),
                    description=_("USB serial port for the controller"),
                ),
                BaudrateVar(key="baudrate", default=115200),
            ]
        )

    def _create_transport(self, module: Any, **kwargs: Any) -> tuple[Any, str]:
        port = kwargs.get("port", "")
        baudrate = kwargs.get("baudrate", 115200)
        if not isinstance(port, str) or not port:
            raise DriverSetupError(_("Serial port must be configured."))
        if isinstance(baudrate, bool) or not isinstance(baudrate, int):
            raise DriverSetupError(_("Baud rate must be an integer."))
        if baudrate <= 0:
            raise DriverSetupError(_("Baud rate must be positive."))
        transport = module.SerialTransport(port, baudrate=baudrate)
        return transport, f"serial://{port}"
