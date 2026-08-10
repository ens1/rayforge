"""Ruida program generation and transfer drivers."""

from .ruida_serial_driver import RuidaSerialDriver
from .ruida_udp_program_driver import RuidaUdpProgramDriver

RuidaDriver = RuidaUdpProgramDriver

__all__ = [
    "RuidaDriver",
    "RuidaSerialDriver",
    "RuidaUdpProgramDriver",
]
