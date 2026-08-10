import inspect
from typing import Any, cast

from .driver import (
    DRIVER_MATURITY_LABELS,
    Driver,
    DriverMaturity,
    PWMParams,
)
from .dummy import NoDeviceDriver
from .grbl import (
    GrblNetworkDriver,
    GrblSerialDriver,
    GrblSerialSimpleDriver,
    GrblTelnetDriver,
)
from .marlin import MarlinSerialDriver
from .octoprint import OctoPrintDriver
from .ruida import RuidaDriver, RuidaSerialDriver, RuidaUdpProgramDriver
from .smoothie import SmoothieDriver


def isdriver(obj):
    return (
        inspect.isclass(obj) and issubclass(obj, Driver) and obj is not Driver
    )


drivers: list[type[Driver]] = []
for obj in list(locals().values()):
    if isdriver(obj) and obj not in drivers:
        drivers.append(cast(type[Driver], obj))

driver_by_classname = {o.__name__: o for o in drivers}
driver_by_classname["RuidaDriver"] = RuidaUdpProgramDriver


def get_driver_cls(classname: str, default=NoDeviceDriver):
    return driver_by_classname.get(classname, default)


def canonicalize_driver_config(
    classname: str | None,
    args: dict[str, Any] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Return the current driver name and migrated setup arguments."""
    driver_cls = driver_by_classname.get(classname) if classname else None
    canonical_name = driver_cls.__name__ if driver_cls else classname
    canonical_args = args.copy() if args is not None else None
    if driver_cls is RuidaUdpProgramDriver and canonical_args is not None:
        if "port" not in canonical_args and "main_port" in canonical_args:
            canonical_args["port"] = canonical_args["main_port"]
        if (
            "local_port" not in canonical_args
            and "response_port" in canonical_args
        ):
            canonical_args["local_port"] = canonical_args["response_port"]
        canonical_args.pop("main_port", None)
        canonical_args.pop("response_port", None)
        canonical_args.pop("jog_port", None)
    return canonical_name, canonical_args


def register_driver(driver: type[Driver]):
    driver_by_classname[driver.__name__] = driver
    drivers.append(driver)


__all__ = [
    "DRIVER_MATURITY_LABELS",
    "Driver",
    "DriverMaturity",
    "GrblNetworkDriver",
    "GrblSerialDriver",
    "GrblSerialSimpleDriver",
    "GrblTelnetDriver",
    "MarlinSerialDriver",
    "NoDeviceDriver",
    "OctoPrintDriver",
    "PWMParams",
    "RuidaDriver",
    "RuidaSerialDriver",
    "RuidaUdpProgramDriver",
    "SmoothieDriver",
    "canonicalize_driver_config",
]
