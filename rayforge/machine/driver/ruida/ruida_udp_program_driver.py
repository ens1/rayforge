"""Ruida UDP program transfer driver."""

from __future__ import annotations

from gettext import gettext as _
from typing import Any

from ....core.varset import HostnameVar, PortVar, VarSet
from ....core.varset.hostnamevar import is_valid_hostname_or_ip
from ..driver import DriverPrecheckError, DriverSetupError
from .program_driver import RuidaProgramDriver
from .ruida_encoder import ruida_job_profile_vars


class RuidaUdpProgramDriver(RuidaProgramDriver):
    """Transfer complete Ruida programs over the controller UDP link."""

    label = _("Ruida (UDP Program)")
    subtitle = _("Send complete Ruida programs over UDP")
    _probe_on_open = True

    @classmethod
    def precheck(cls, **kwargs: Any) -> None:
        host = kwargs.get("host", "")
        if host and not is_valid_hostname_or_ip(host):
            message = _("Invalid hostname or IP address: '{host}'")
            raise DriverPrecheckError(message.format(host=host))

    @classmethod
    def get_setup_vars(cls) -> VarSet:
        return VarSet(
            vars=[
                HostnameVar(
                    key="host",
                    label=_("Hostname"),
                    description=_(
                        "The IP address or hostname of the Ruida controller"
                    ),
                ),
                PortVar(
                    key="port",
                    label=_("Controller Port"),
                    description=_("Ruida controller UDP port"),
                    default=50200,
                ),
                PortVar(
                    key="local_port",
                    label=_("Local Port"),
                    description=_("Local UDP response port"),
                    default=40200,
                ),
                *ruida_job_profile_vars(),
            ]
        )

    def _create_transport(self, module: Any, **kwargs: Any) -> tuple[Any, str]:
        host = kwargs.get("host", "")
        port = kwargs.get("port", 50200)
        local_port = kwargs.get("local_port", 40200)
        if not isinstance(host, str) or not host:
            raise DriverSetupError(_("Hostname must be configured."))
        if not is_valid_hostname_or_ip(host):
            message = _("Invalid hostname or IP address: '{host}'")
            raise DriverSetupError(message.format(host=host))
        if isinstance(port, bool) or not isinstance(port, int):
            raise DriverSetupError(_("Controller port must be an integer."))
        if isinstance(local_port, bool) or not isinstance(local_port, int):
            raise DriverSetupError(_("Local port must be an integer."))
        transport = module.UdpTransport(
            host,
            controller_port=port,
            local_port=local_port,
        )
        resource = f"udp://{host}:{port}?local_port={local_port}"
        return transport, resource
