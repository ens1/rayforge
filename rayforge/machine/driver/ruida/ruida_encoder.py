"""Compile Rayforge operations into complete Ruida programs."""

from __future__ import annotations

import importlib
import json
import math
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from gettext import gettext as _
from typing import TYPE_CHECKING, Any, Literal

from raygeo.ops import Ops
from raygeo.ops.state import AirAssistMode
from raygeo.ops.types import CommandType

from ....pipeline.encoder.base import (
    EncodedOutput,
    MachineCodeOpMap,
    OpsEncoder,
)

if TYPE_CHECKING:
    from ....core.doc import Doc
    from ....machine.models.machine import Machine


_POSITION_TOLERANCE = 1e-9
_WIRE_COORDINATE_SCALE = 1000
_U8_POWER_TOLERANCE = 0.5 / 255 + 1e-12
_PROCESS_SCHEMA = "rayforge.process"
_PROCESS_VERSION = 1
_UNSET_INACTIVE_POWER = -1.0
_HEAD_MAPPING_ERROR = (
    "Ruida laser head tool numbers must be unique: tool 0=Ruida channel 1 "
    "and tool 1=Ruida channel 2"
)
RUIDA_JOB_PROFILE_KEY = "job_profile"
DEFAULT_RUIDA_JOB_PROFILE = "proven"
RUIDA_JOB_PROFILES = (
    DEFAULT_RUIDA_JOB_PROFILE,
    "planned-path-research",
    "dual-laser-research",
    "stationary-research",
    "rf-research",
    "fiber-research",
    "z-research",
    "dynamic-power-research",
)
RUIDA_INACTIVE_POWER_KEYS = {
    1: (
        "laser_1_inactive_min_power_percent",
        "laser_1_inactive_max_power_percent",
    ),
    2: (
        "laser_2_inactive_min_power_percent",
        "laser_2_inactive_max_power_percent",
    ),
}
RUIDA_INACTIVE_POWER_CONFIRMED_KEYS = {
    1: "laser_1_inactive_powers_confirmed",
    2: "laser_2_inactive_powers_confirmed",
}

_PROFILE_EXPORTS = {
    "proven": "LIGHTBURN_2103_644XS",
    "planned-path-research": ("LIGHTBURN_2103_644XS_PLANNED_PATH_RESEARCH"),
    "dual-laser-research": ("LIGHTBURN_2103_644XS_DUAL_LASER_RESEARCH"),
    "stationary-research": ("LIGHTBURN_2103_644XS_STATIONARY_RESEARCH"),
    "rf-research": "LIGHTBURN_2103_644XS_RF_RESEARCH",
    "fiber-research": "LIGHTBURN_2103_644XS_FIBER_RESEARCH",
    "z-research": "LIGHTBURN_2103_644XS_Z_RESEARCH",
    "dynamic-power-research": ("LIGHTBURN_2103_644XS_DYNAMIC_POWER_RESEARCH"),
}


@dataclass(frozen=True)
class _RuidaEncoderConfig:
    profile_name: str
    dynamic_power_restore_contract: int | None
    inactive_channel_powers: tuple[
        tuple[float | None, float | None],
        tuple[float | None, float | None],
    ]
    inactive_channel_powers_confirmed: tuple[bool, bool]
    head_mappings: tuple[tuple[str, int, str | None], ...]

    def token_payload(self) -> dict[str, Any]:
        payload = {
            RUIDA_JOB_PROFILE_KEY: self.profile_name,
            "inactive_channel_powers_confirmed": {
                str(index): value
                for index, value in enumerate(
                    self.inactive_channel_powers_confirmed,
                    start=1,
                )
            },
            "inactive_channel_powers": {
                str(index): list(values)
                for index, values in enumerate(
                    self.inactive_channel_powers,
                    start=1,
                )
            },
            "head_mappings": [
                {
                    "uid": uid,
                    "tool_number": tool_number,
                    "laser_type": laser_type,
                }
                for uid, tool_number, laser_type in self.head_mappings
            ],
        }
        if self.profile_name == "dynamic-power-research":
            payload["dynamic_power_restore_contract"] = (
                self.dynamic_power_restore_contract
            )
        return payload

    def inactive_power(
        self,
        index: int,
    ) -> tuple[float | None, float | None]:
        return self.inactive_channel_powers[index - 1]

    def inactive_power_confirmed(self, index: int) -> bool:
        return self.inactive_channel_powers_confirmed[index - 1]


class RuidaEncodingError(ValueError):
    """Raised when Ops cannot be represented by the proven Ruida API."""


@dataclass(frozen=True)
class _RuidaApi:
    dynamic_power_restore_contract: int | None
    Dwell: Any
    JobPlan: Any
    LaserChannelPlan: Any
    LayerPlan: Any
    MarkTo: Any
    MarkWithPower: Any
    RasterSection: Any
    RuidaJobCompiler: Any
    SetModulation: Any
    TravelTo: Any
    profiles: dict[str, Any]


@lru_cache(maxsize=1)
def _load_ruida_api() -> _RuidaApi:
    try:
        module = importlib.import_module("ruida_re")
    except ImportError as error:
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise RuntimeError(
            "Ruida encoding requires the optional ruida-re package "
            f"and Python 3.11 or newer; running Python {version}"
        ) from error

    core_names = (
        "Dwell",
        "JobPlan",
        "LaserChannelPlan",
        "LayerPlan",
        "MarkTo",
        "MarkWithPower",
        "RasterSection",
        "RuidaJobCompiler",
        "SetModulation",
        "TravelTo",
    )
    required = (*core_names, *_PROFILE_EXPORTS.values())
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Installed ruida-re lacks required API: {names}")
    profiles = {
        name: getattr(module, export)
        for name, export in _PROFILE_EXPORTS.items()
    }
    restore_contract = getattr(
        module,
        "DYNAMIC_POWER_RESTORE_CONTRACT",
        None,
    )
    if isinstance(restore_contract, bool) or not isinstance(
        restore_contract,
        int,
    ):
        restore_contract = None
    return _RuidaApi(
        dynamic_power_restore_contract=restore_contract,
        **{name: getattr(module, name) for name in core_names},
        profiles=profiles,
    )


def normalize_ruida_job_profile(value: object) -> str:
    """Validate and normalize one persisted Ruida job profile name."""
    if value is None:
        return DEFAULT_RUIDA_JOB_PROFILE
    if not isinstance(value, str) or value not in RUIDA_JOB_PROFILES:
        raise RuidaEncodingError(f"Unsupported Ruida job profile {value!r}")
    return value


def ruida_job_profile_from_machine(machine: Machine) -> str:
    """Return the explicitly configured job profile for a machine."""
    return normalize_ruida_job_profile(
        machine.driver_args.get(
            RUIDA_JOB_PROFILE_KEY,
            DEFAULT_RUIDA_JOB_PROFILE,
        )
    )


def ruida_pwm_params(machine: Machine, head: Any) -> Any | None:
    """Return profile-scoped PWM fields for one Ruida laser head."""
    from ...models.laser import LaserHead, LaserType
    from ..driver import PWMParams

    if not isinstance(head, LaserHead) or head.laser_type == LaserType.DIODE:
        return None
    profile = ruida_job_profile_from_machine(machine)
    if profile == "rf-research":
        frequency = min(max(head.pwm_frequency, 10_000), 20_000)
        return PWMParams(
            frequency=frequency,
            min_frequency=10_000,
            max_frequency=20_000,
            frequency_zero_disables=True,
            pulse_width=None,
            min_pulse_width=None,
            max_pulse_width=None,
        )
    if profile == "fiber-research":
        if head.laser_type != LaserType.FIBER:
            return None
        pulse_width = min(max(float(head.pulse_width), 0.0), 0.2)
        return PWMParams(
            frequency=None,
            min_frequency=None,
            max_frequency=None,
            pulse_width=pulse_width,
            min_pulse_width=0.0,
            max_pulse_width=0.2,
        )
    return None


def ruida_encoder_token_payload(machine: Machine) -> dict[str, Any]:
    """Return all machine fields that affect Ruida plan adaptation."""
    return _snapshot_ruida_encoder_config(machine).token_payload()


def _snapshot_ruida_encoder_config(
    machine: Machine,
    profile: str | None = None,
) -> _RuidaEncoderConfig:
    from ...models.laser import LaserHead

    profile_name = (
        ruida_job_profile_from_machine(machine)
        if profile is None
        else normalize_ruida_job_profile(profile)
    )
    channel_config = _channel_config_payload(machine.driver_args)
    laser_heads = [
        head for head in machine.heads if isinstance(head, LaserHead)
    ]
    tool_numbers = [head.tool_number for head in laser_heads]
    if any(
        isinstance(tool_number, bool)
        or not isinstance(tool_number, int)
        or tool_number not in (0, 1)
        for tool_number in tool_numbers
    ):
        raise RuidaEncodingError(_HEAD_MAPPING_ERROR)
    if len(set(tool_numbers)) != len(tool_numbers):
        raise RuidaEncodingError(_HEAD_MAPPING_ERROR)
    heads = tuple(
        sorted(
            (
                (
                    head.uid,
                    head.tool_number,
                    getattr(
                        getattr(head, "laser_type", None),
                        "value",
                        None,
                    ),
                )
                for head in laser_heads
            ),
            key=lambda item: (item[0], item[1]),
        )
    )
    return _RuidaEncoderConfig(
        profile_name=profile_name,
        dynamic_power_restore_contract=(
            _load_ruida_api().dynamic_power_restore_contract
            if profile_name == "dynamic-power-research"
            else None
        ),
        inactive_channel_powers=(
            (channel_config["1"][0], channel_config["1"][1]),
            (channel_config["2"][0], channel_config["2"][1]),
        ),
        inactive_channel_powers_confirmed=(
            _confirmed_channel_config(machine.driver_args, 1),
            _confirmed_channel_config(machine.driver_args, 2),
        ),
        head_mappings=heads,
    )


def ruida_job_profile_vars() -> list[Any]:
    """Return persisted setup fields for the Ruida compiler profile."""
    from ....core.varset import BoolVar, FloatVar, LabeledChoiceVar

    offline_research = _("Offline research; not hardware-validated")
    limited_research = _("Research; limited hardware evidence")
    choices = [
        (_("Proven LightBurn 2.1.03 / Ruida 644XS"), "proven"),
        (
            f"{limited_research}: " + _("planned-path raster"),
            "planned-path-research",
        ),
        (
            f"{offline_research}: "
            + _("one selected channel; no simultaneous dual-head output"),
            "dual-laser-research",
        ),
        (
            f"{offline_research}: "
            + _("stationary dwell (manual Ops/frame only)"),
            "stationary-research",
        ),
        (
            f"{offline_research}: "
            + _("RF frequency (profile selection confirms RF hardware)"),
            "rf-research",
        ),
        (
            f"{offline_research}: " + _("fiber pulse width"),
            "fiber-research",
        ),
        (
            f"{offline_research}: " + _("logical raster layer Z offset"),
            "z-research",
        ),
        (
            f"{limited_research}: "
            + _("dynamic vector power; corrected restoration offline-only"),
            "dynamic-power-research",
        ),
    ]
    fields: list[Any] = [
        LabeledChoiceVar(
            key=RUIDA_JOB_PROFILE_KEY,
            label=_("Ruida Job Profile"),
            choices=choices,
            description=_(
                "Advanced profiles are evidence-limited; planned-path has "
                "narrow positive hardware observations, dynamic power has "
                "hardware observations that exposed missing restoration, "
                "and the corrected sequence plus the remaining profiles are "
                "offline-only"
            ),
            default=DEFAULT_RUIDA_JOB_PROFILE,
            allow_none=False,
        )
    ]
    for index, keys in RUIDA_INACTIVE_POWER_KEYS.items():
        for bound, key in zip((_("minimum"), _("maximum")), keys):
            fields.append(
                FloatVar(
                    key=key,
                    label=_(
                        "Laser {index} inactive stored {bound} power (%)"
                    ).format(index=index, bound=bound),
                    description=_(
                        "Required for an explicit two-channel research "
                        "profile; -1 means unset, otherwise enter the "
                        "controller's known stored value"
                    ),
                    default=_UNSET_INACTIVE_POWER,
                    min_val=_UNSET_INACTIVE_POWER,
                    max_val=100,
                )
            )
        fields.append(
            BoolVar(
                key=RUIDA_INACTIVE_POWER_CONFIRMED_KEYS[index],
                label=_("Confirm laser {index} inactive powers").format(
                    index=index
                ),
                description=_(
                    "Confirms that this channel's inactive minimum and "
                    "maximum power values were intentionally entered for "
                    "the selected research profile"
                ),
                default=False,
            )
        )
    return fields


def _confirmed_channel_config(
    driver_args: dict[str, Any],
    index: int,
) -> bool:
    value = driver_args.get(
        RUIDA_INACTIVE_POWER_CONFIRMED_KEYS[index],
        False,
    )
    if not isinstance(value, bool):
        raise RuidaEncodingError(
            f"Inactive laser {index} power confirmation must be boolean"
        )
    return value


def _channel_config_payload(
    driver_args: dict[str, Any],
) -> dict[str, list[float | None]]:
    result: dict[str, list[float | None]] = {}
    for index, (minimum_key, maximum_key) in RUIDA_INACTIVE_POWER_KEYS.items():
        minimum = _optional_power_percent(
            driver_args.get(minimum_key),
            f"inactive laser {index} minimum power",
        )
        maximum = _optional_power_percent(
            driver_args.get(maximum_key),
            f"inactive laser {index} maximum power",
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise RuidaEncodingError(
                f"Inactive laser {index} minimum power cannot exceed maximum"
            )
        result[str(index)] = [minimum, maximum]
    return result


def _optional_power_percent(value: object, label: str) -> float | None:
    if value is None:
        return None
    result = _finite_number(value, label)
    if result == _UNSET_INACTIVE_POWER:
        return None
    if not 0 <= result <= 100:
        raise RuidaEncodingError(f"{label} must be between 0 and 100")
    return result


@dataclass(frozen=True)
class _ProcessMetadata:
    uid: str
    kind: Literal["vector", "raster", "mixed", "generic"]
    cut_speed_mm_min: float
    travel_speed_mm_min: float
    head_uid: str | None
    head_tool_number: int | None
    power_mode: str | None
    power: float | None
    min_power: float | None
    max_power: float | None
    air_assist: bool | None
    color_rgb: int
    frequency_hz: int | None
    pulse_width_us: float | None
    z_offset_mm: float | None
    raster_depth_mode: str | None
    raster_mode: str | None
    raster_min_power: float | None
    raster_max_power: float | None
    sample_power_encoding: str | None
    raster_scan_axis: str | None
    raster_strategy: str | None
    raster_scan_angle: float | None
    raster_scan_mode: str | None
    raster_cross_hatch: bool | None
    z_motion: bool
    rotary: bool
    rotary_mode: str | None
    rotary_axis: str | None

    @classmethod
    def from_json(cls, uid: str, raw: str) -> _ProcessMetadata:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuidaEncodingError(
                f"Process {uid!r} has invalid JSON metadata"
            ) from error
        if not isinstance(data, dict):
            raise RuidaEncodingError(
                f"Process {uid!r} metadata must be an object"
            )
        version = data.get("version")
        if (
            data.get("schema") != _PROCESS_SCHEMA
            or isinstance(version, bool)
            or version != _PROCESS_VERSION
        ):
            raise RuidaEncodingError(
                f"Process {uid!r} requires {_PROCESS_SCHEMA} version "
                f"{_PROCESS_VERSION}"
            )

        identity = _mapping(data.get("identity"), "process identity")
        identity_uid = _required_string(identity.get("uid"), "process UID")
        if identity_uid != uid:
            raise RuidaEncodingError(
                "Process marker UID disagrees with its identity"
            )
        motion = _mapping(data.get("motion"), "process motion")
        axes = _mapping(data.get("axes"), "process axes")
        raster = _optional_mapping(data.get("raster"), "raster metadata")
        power = _optional_mapping(data.get("power"), "power metadata")

        kind = data.get("kind")
        if kind not in ("vector", "raster", "mixed", "generic"):
            raise RuidaEncodingError(
                f"Process {uid!r} has unsupported kind {kind!r}"
            )

        return cls(
            uid=uid,
            kind=kind,
            cut_speed_mm_min=_finite_number(
                motion.get("cut_speed_mm_min"),
                "process cut speed",
            ),
            travel_speed_mm_min=_finite_number(
                motion.get("rapid_speed_mm_min"),
                "process travel speed",
            ),
            head_uid=_optional_string(data.get("head_uid"), "head UID"),
            head_tool_number=_optional_integer(
                data.get("head_tool_number"),
                "head tool number",
            ),
            power_mode=(
                _required_string(power.get("mode"), "power mode")
                if power is not None
                else None
            ),
            power=(
                _fraction(power.get("value"), "process power")
                if power is not None
                else None
            ),
            min_power=(
                _fraction(power.get("min"), "minimum process power")
                if power is not None
                else None
            ),
            max_power=(
                _fraction(power.get("max"), "maximum process power")
                if power is not None
                else None
            ),
            air_assist=_optional_boolean(
                data.get("air_assist"),
                "air assist",
            ),
            color_rgb=_rgb(identity.get("color_rgb")),
            frequency_hz=_optional_integer(
                data.get("frequency_hz"),
                "frequency",
            ),
            pulse_width_us=_optional_number(
                data.get("pulse_width_us"),
                "pulse width",
            ),
            z_offset_mm=_optional_number(
                data.get("z_offset_mm"),
                "logical layer Z offset",
            ),
            raster_depth_mode=_optional_string(
                raster.get("depth_mode") if raster is not None else None,
                "raster depth mode",
            ),
            raster_mode=_optional_string(
                raster.get("raster_mode") if raster is not None else None,
                "raygeo raster mode",
            ),
            raster_min_power=_optional_fraction(
                raster.get("min_output_power") if raster is not None else None,
                "minimum raster output power",
            ),
            raster_max_power=_optional_fraction(
                raster.get("max_output_power") if raster is not None else None,
                "maximum raster output power",
            ),
            sample_power_encoding=_optional_string(
                raster.get("sample_power_encoding")
                if raster is not None
                else None,
                "sample power encoding",
            ),
            raster_scan_axis=_optional_string(
                raster.get("scan_axis") if raster is not None else None,
                "raster scan axis",
            ),
            raster_strategy=_optional_string(
                raster.get("scan_strategy") if raster is not None else None,
                "raster scan strategy",
            ),
            raster_scan_angle=_optional_number(
                raster.get("scan_angle_degrees")
                if raster is not None
                else None,
                "raster scan angle",
            ),
            raster_scan_mode=_optional_string(
                raster.get("scan_mode") if raster is not None else None,
                "raster scan mode",
            ),
            raster_cross_hatch=_optional_boolean(
                raster.get("cross_hatch") if raster is not None else None,
                "raster cross-hatch state",
            ),
            z_motion=_boolean(axes.get("z_motion"), "Z motion intent"),
            rotary=_boolean(axes.get("rotary"), "rotary intent"),
            rotary_mode=_optional_string(
                axes.get("rotary_mode"),
                "rotary mode",
            ),
            rotary_axis=_optional_string(
                axes.get("rotary_axis"),
                "rotary axis",
            ),
        )


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuidaEncodingError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise RuidaEncodingError(f"{label} must be a finite number")
    return result


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, label)


def _pulse_width_ns(value: object, label: str) -> int:
    width_us = _finite_number(value, label)
    if width_us < 0:
        raise RuidaEncodingError(f"{label} cannot be negative")
    width_ns = Decimal(str(width_us)) * Decimal(1000)
    integral = width_ns.to_integral_value()
    if width_ns != integral:
        raise RuidaEncodingError(
            f"{label} must convert exactly to an integer number of nanoseconds"
        )
    return int(integral)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuidaEncodingError(f"{label} must be an integer")
    return value


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label)


def _fraction(value: object, label: str) -> float:
    result = _finite_number(value, label)
    if not 0 <= result <= 1:
        raise RuidaEncodingError(f"{label} must be between 0 and 1")
    return result


def _optional_fraction(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _fraction(value, label)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuidaEncodingError(f"{label} must be boolean")
    return value


def _optional_boolean(value: object, label: str) -> bool | None:
    if value is None:
        return None
    return _boolean(value, label)


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RuidaEncodingError(f"{label} must be a nonempty string")
    return value


def _required_string(value: object, label: str) -> str:
    result = _optional_string(value, label)
    if result is None:
        raise RuidaEncodingError(f"{label} is required")
    return result


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuidaEncodingError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise RuidaEncodingError(f"{label} keys must be strings")
    return value


def _optional_mapping(
    value: object,
    label: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    return _mapping(value, label)


def _rgb(value: object) -> int:
    if value is None:
        return 0
    if not isinstance(value, list) or len(value) != 3:
        raise RuidaEncodingError(
            "process color must be an RGB triplet or null"
        )
    channels = [_integer(channel, "RGB channel") for channel in value]
    if any(not 0 <= channel <= 255 for channel in channels):
        raise RuidaEncodingError("RGB channels must be between 0 and 255")
    return (channels[0] << 16) | (channels[1] << 8) | channels[2]


def _raster_axis(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
) -> Literal["horizontal", "vertical"]:
    start_x, start_y = _wire_xy(start)
    end_x, end_y = _wire_xy(end)
    dx = end_x - start_x
    dy = end_y - start_y
    if dy == 0 and dx != 0:
        return "horizontal"
    if dx == 0 and dy != 0:
        return "vertical"
    raise RuidaEncodingError(
        "Ruida raster marks must be horizontal or vertical"
    )


def _wire_xy(
    position: tuple[float, float, float],
) -> tuple[int, int]:
    return (
        round(position[0] * _WIRE_COORDINATE_SCALE),
        round(position[1] * _WIRE_COORDINATE_SCALE),
    )


def _same_wire_position(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
) -> bool:
    return _wire_xy(start) == _wire_xy(end)


def _source_raster_axis(
    angle: float,
) -> Literal["horizontal", "vertical", "arbitrary"]:
    normalized = angle % 180
    if math.isclose(
        normalized,
        0,
        rel_tol=0,
        abs_tol=_POSITION_TOLERANCE,
    ) or math.isclose(
        normalized,
        180,
        rel_tol=0,
        abs_tol=_POSITION_TOLERANCE,
    ):
        return "horizontal"
    if math.isclose(
        normalized,
        90,
        rel_tol=0,
        abs_tol=_POSITION_TOLERANCE,
    ):
        return "vertical"
    return "arbitrary"


def _sample_runs(samples: bytes) -> Iterator[tuple[int, int]]:
    run_value = samples[0]
    for index, sample in enumerate(samples[1:], start=1):
        if sample == run_value:
            continue
        yield index, run_value
        run_value = sample
    yield len(samples), run_value


def _process_uid(ops: Ops, index: int) -> str:
    accessor = getattr(ops, "process_uid", None)
    if not callable(accessor):
        raise TypeError(
            "Ruida encoding requires Raygeo process marker bindings"
        )
    return _required_string(accessor(index), "process marker UID")


def _process_params(ops: Ops, index: int) -> str:
    accessor = getattr(ops, "process_params", None)
    if not callable(accessor):
        raise TypeError(
            "Ruida encoding requires Raygeo process marker bindings"
        )
    return _required_string(accessor(index), "process marker metadata")


@dataclass
class _MachineState:
    power: float = 0.0
    feed_rate_mm_min: float | None = None
    rapid_rate_mm_min: float | None = None
    air_assist: bool = False
    head_uid: str | None = None
    frequency_hz: int | None = None
    pulse_width_ns: int | None = None


@dataclass(frozen=True)
class _LayerKey:
    kind: Literal["vector", "raster"]
    speed_mm_s: float
    power_percent: float | None
    air_assist: bool
    laser_index: int
    color_rgb: int
    process_uid: str
    layer_uid: str | None
    raster_axis: Literal["horizontal", "vertical"] | None
    raster_processing: Literal["native", "planned-path"] | None
    explicit_channels: bool


@dataclass
class _LayerBuilder:
    key: _LayerKey
    events: list[Any] = field(default_factory=list)
    raster_sections: list[tuple[int, list[Any]]] = field(default_factory=list)
    raster_axes: set[str] = field(default_factory=set)
    raster_directions: list[int] = field(default_factory=list)
    raster_powers: list[float] = field(default_factory=list)
    process: _ProcessMetadata | None = None
    inactive_power: tuple[float, float] | None = None
    frequency_hz: int | None = None
    pulse_width_ns: int | None = None
    z_offset_mm: float | None = None
    uses_dynamic_power: bool = False

    def can_merge(self, other: _LayerBuilder) -> bool:
        if self.key != other.key or self.process != other.process:
            return False
        if (
            self.inactive_power != other.inactive_power
            or self.frequency_hz != other.frequency_hz
            or self.pulse_width_ns != other.pulse_width_ns
            or self.z_offset_mm != other.z_offset_mm
        ):
            return False
        if self.key.kind != "raster":
            return True
        if self.key.raster_processing == "planned-path":
            return True
        strategy = self.process.raster_strategy if self.process else None
        directions = {*self.raster_directions, *other.raster_directions}
        return strategy != "unidirectional" or len(directions) <= 1

    def merge(self, other: _LayerBuilder) -> None:
        self.events.extend(other.events)
        for section_id, events in other.raster_sections:
            if (
                self.raster_sections
                and self.raster_sections[-1][0] == section_id
            ):
                self.raster_sections[-1][1].extend(events)
            else:
                self.raster_sections.append((section_id, list(events)))
        self.raster_axes.update(other.raster_axes)
        self.raster_directions.extend(other.raster_directions)
        self.raster_powers.extend(other.raster_powers)
        self.uses_dynamic_power = (
            self.uses_dynamic_power or other.uses_dynamic_power
        )

    def append_raster_direction(
        self,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
    ) -> None:
        start_x, start_y = _wire_xy(start)
        end_x, end_y = _wire_xy(end)
        axis = _raster_axis(start, end)
        self.raster_axes.add(axis)
        delta = end_x - start_x if axis == "horizontal" else end_y - start_y
        self.raster_directions.append(1 if delta > 0 else -1)

    def extend_events(
        self,
        events: Iterator[Any] | list[Any] | tuple[Any, ...],
        section_id: int | None,
    ) -> None:
        values = list(events)
        if not values:
            return
        if self.key.raster_processing != "planned-path":
            self.events.extend(values)
            return
        if section_id is None:
            raise RuidaEncodingError(
                "Planned-path raster events require an Ops section"
            )
        if self.raster_sections and self.raster_sections[-1][0] == section_id:
            self.raster_sections[-1][1].extend(values)
        else:
            self.raster_sections.append((section_id, values))

    def append_event(self, event: Any, section_id: int | None) -> None:
        self.extend_events((event,), section_id)

    def to_layer_plan(self, api: _RuidaApi, index: int) -> Any:
        minimum, maximum = self._power_range()
        channels = self._laser_channels(api, minimum, maximum)
        legacy_minimum = minimum
        legacy_maximum = maximum
        laser_index = self.key.laser_index
        if channels is not None:
            laser_index = 1
            legacy_minimum = channels[0].min_power_percent
            legacy_maximum = channels[0].max_power_percent
        values: dict[str, Any] = {
            "index": index,
            "speed_mm_s": self.key.speed_mm_s,
            "min_power_percent": legacy_minimum,
            "max_power_percent": legacy_maximum,
            "events": tuple(self.events),
            "kind": self.key.kind,
            "air_assist": self.key.air_assist,
            "color_rgb": self.key.color_rgb,
            "laser_index": laser_index,
            "laser_channels": channels,
            "frequency_hz": self.frequency_hz,
            "pulse_width_ns": self.pulse_width_ns,
            "z_offset_mm": self.z_offset_mm,
        }
        if self.key.kind == "raster":
            values["raster_processing"] = self.key.raster_processing
            if self.key.raster_processing == "planned-path":
                values["raster_sections"] = tuple(
                    api.RasterSection(tuple(events))
                    for _section_id, events in self.raster_sections
                )
            else:
                values["scan_axis"] = self._raster_axis()
                values["raster_strategy"] = self._raster_strategy()
        return api.LayerPlan(**values)

    def _power_range(self) -> tuple[float, float]:
        if self.key.kind == "raster":
            return self._raster_power_range()
        if (
            self.uses_dynamic_power
            and self.process is not None
            and self.process.power_mode == "dynamic"
        ):
            minimum = self.process.min_power
            maximum = self.process.max_power
            if minimum is None or maximum is None:
                raise RuidaEncodingError(
                    "Dynamic vector power requires declared bounds"
                )
            return minimum * 100, maximum * 100
        if self.key.power_percent is None:
            raise RuidaEncodingError("Vector motion requires layer power")
        return self.key.power_percent, self.key.power_percent

    def _laser_channels(
        self,
        api: _RuidaApi,
        minimum: float,
        maximum: float,
    ) -> tuple[Any, ...] | None:
        if not self.key.explicit_channels:
            return None
        if self.inactive_power is None:
            inactive = 2 if self.key.laser_index == 1 else 1
            raise RuidaEncodingError(
                f"Ruida laser {inactive} inactive stored powers must be "
                "configured explicitly"
            )
        result = []
        for channel in (1, 2):
            enabled = channel == self.key.laser_index
            power_range = (
                (minimum, maximum) if enabled else self.inactive_power
            )
            result.append(
                api.LaserChannelPlan(
                    index=channel,
                    enabled=enabled,
                    min_power_percent=power_range[0],
                    max_power_percent=power_range[1],
                )
            )
        return tuple(result)

    def _raster_power_range(self) -> tuple[float, float]:
        if self.process is None:
            raise RuidaEncodingError("Raster motion requires process metadata")
        if self.process.kind == "mixed":
            if self.key.power_percent is not None:
                return self.key.power_percent, self.key.power_percent
            if self.raster_powers:
                return min(self.raster_powers), max(self.raster_powers)
            raise RuidaEncodingError(
                "Mixed raster motion has no explicit power regime"
            )
        return self._metadata_raster_power_range()

    def _metadata_raster_power_range(self) -> tuple[float, float]:
        assert self.process is not None
        minimum = self.process.raster_min_power
        maximum = self.process.raster_max_power
        if minimum is None or maximum is None:
            raise RuidaEncodingError(
                "Raster metadata requires min and max output power"
            )
        if minimum > maximum:
            raise RuidaEncodingError(
                "Raster minimum power cannot exceed maximum power"
            )
        return minimum * 100, maximum * 100

    def _raster_axis(self) -> str:
        if len(self.raster_axes) != 1:
            raise RuidaEncodingError(
                "One Ruida raster layer cannot mix scan axes"
            )
        inferred = next(iter(self.raster_axes))
        if inferred != self.key.raster_axis:
            raise RuidaEncodingError(
                "Raster layer axis disagrees with its motion"
            )
        if self.process is None:
            raise RuidaEncodingError("Raster motion requires process metadata")
        return inferred

    def _raster_strategy(self) -> str:
        inferred = (
            "bidirectional"
            if len(set(self.raster_directions)) > 1
            else "unidirectional"
        )
        if self.process is None:
            raise RuidaEncodingError("Raster motion requires process metadata")
        declared = self.process.raster_strategy
        if declared is None and self.process.kind == "mixed":
            return inferred
        if declared not in ("unidirectional", "bidirectional"):
            raise RuidaEncodingError(
                f"Unsupported raster strategy {declared!r}"
            )
        if declared == "unidirectional" and inferred != declared:
            raise RuidaEncodingError(
                "Unidirectional raster metadata has opposing scan moves"
            )
        return declared


class RuidaOpsAdapter:
    """Lower emission-ready planar Ops into a ruida-re JobPlan."""

    def __init__(
        self,
        machine: Machine,
        profile: str = DEFAULT_RUIDA_JOB_PROFILE,
        *,
        config: _RuidaEncoderConfig | None = None,
    ):
        self.api = _load_ruida_api()
        self.config = config or _snapshot_ruida_encoder_config(
            machine,
            profile,
        )
        self.profile_name = self.config.profile_name
        self.profile = self.api.profiles[self.profile_name]
        self.state = _MachineState()
        self.current_pos: tuple[float, float, float] | None = None
        self.pending_travels: list[Any] = []
        self.builders: list[_LayerBuilder] = []
        self.active_builder: _LayerBuilder | None = None
        self.active_process: _ProcessMetadata | None = None
        self.process_state_fields: set[str] = set()
        self.active_layer_uid: str | None = None
        self.section_kind: Literal["vector", "raster"] | None = None
        self.section_type_name: str | None = None
        self.section_raster_mode: str | None = None
        self.raster_section_id: int | None = None
        self.next_raster_section_id = 0
        self.resolved_section_raster_axis: (
            Literal["horizontal", "vertical"] | None
        ) = None
        self.warnings: list[str] = []
        if self.profile_name == "planned-path-research":
            self.warnings.append(
                _(
                    "The selected Ruida planned-path research profile has "
                    "limited hardware evidence from five-line, single-section "
                    "diagonal coupons on a Boss LS2040 over USB serial: "
                    "motion without visible marks at 10%, and visible marks "
                    "at 15%, including one job generated end to end by "
                    "Rayforge. All ran at 100 mm/s; other accepted "
                    "combinations remain unvalidated"
                )
            )
        elif self.profile_name == "dynamic-power-research":
            self.warnings.append(
                _(
                    "The selected Ruida dynamic-power research profile has "
                    "limited hardware evidence from two one-layer vector "
                    "coupons on a Boss LS2040 at 100 mm/s. A coupon planned "
                    "as 15%-10%-15% looked solid; one planned as 15%-5%-15% "
                    "visibly marked only its first 30 mm. The latter payload "
                    "omitted baseline restoration after its reduced span. "
                    "Rayforge now requires explicit restoration support, "
                    "but the corrected sequence has offline evidence only "
                    "and remains hardware-unvalidated"
                )
            )
        elif self.profile_name != DEFAULT_RUIDA_JOB_PROFILE:
            self.warnings.append(
                _(
                    "The selected Ruida research profile has offline "
                    "fixture evidence only and no hardware execution "
                    "validation"
                )
            )

    def build_plan(self, ops: Ops) -> Any:
        for index in range(ops.len()):
            self._handle_command(ops, index)
        self._finish()
        if not self.builders:
            raise RuidaEncodingError("Ruida jobs must contain marking motion")
        builders = self._coalesced_builders()
        self._validate_profile_scope(builders)
        layers = tuple(
            builder.to_layer_plan(self.api, index)
            for index, builder in enumerate(builders)
        )
        return self.api.JobPlan(layers=layers)

    def _validate_profile_scope(
        self,
        builders: list[_LayerBuilder],
    ) -> None:
        if self.profile_name == "proven":
            return
        if self.profile_name == "planned-path-research":
            if (
                len(builders) != 1
                or builders[0].key.kind != "raster"
                or builders[0].key.raster_processing != "planned-path"
            ):
                raise RuidaEncodingError(
                    "The planned-path-research profile requires exactly "
                    "one planned-path raster layer"
                )
            return
        if self.profile_name == "z-research":
            if (
                len(builders) != 1
                or builders[0].key.kind != "raster"
                or builders[0].key.raster_processing != "native"
                or builders[0].z_offset_mm is None
            ):
                raise RuidaEncodingError(
                    "The z-research profile requires exactly one native "
                    "raster layer with a typed logical Z offset"
                )
            return
        if len(builders) != 1 or builders[0].key.kind != "vector":
            raise RuidaEncodingError(
                f"Ruida job profile {self.profile_name!r} requires exactly "
                "one vector layer"
            )
        if (
            self.profile_name == "dynamic-power-research"
            and builders[0].key.laser_index != 1
        ):
            raise RuidaEncodingError(
                "Dynamic Ruida vector power has evidence for laser head 1 only"
            )

    def _coalesced_builders(self) -> list[_LayerBuilder]:
        result: list[_LayerBuilder] = []
        for builder in self.builders:
            if result and result[-1].can_merge(builder):
                result[-1].merge(builder)
            else:
                result.append(builder)
        return result

    def _handle_command(self, ops: Ops, index: int) -> None:
        command = ops.command_type(index)
        if self._handle_state_command(command, ops, index):
            return
        if self._handle_motion_command(command, ops, index):
            return
        if self._handle_marker_command(command, ops, index):
            return
        self._reject_command(command)

    def _handle_state_command(
        self,
        command: CommandType,
        ops: Ops,
        index: int,
    ) -> bool:
        if command == CommandType.SET_POWER:
            self.state.power = _fraction(ops.power(index), "laser power")
            self._record_process_state("power")
            return True
        if command == CommandType.SET_FEED_RATE:
            self.state.feed_rate_mm_min = self._positive_rate(ops, index)
            self._record_process_state("feed")
            return True
        if command == CommandType.SET_RAPID_RATE:
            self.state.rapid_rate_mm_min = self._positive_rate(ops, index)
            self._record_process_state("rapid")
            warning = _(
                "Ruida TravelTo uses the controller-configured rapid rate"
            )
            if warning not in self.warnings:
                self.warnings.append(warning)
            return True
        if command == CommandType.SET_AIR_ASSIST:
            self.state.air_assist = ops.air_assist(index) == AirAssistMode.ON
            self._record_process_state("air")
            return True
        if command == CommandType.SET_HEAD:
            self.state.head_uid = ops.head_uid(index)
            self._record_process_state("head")
            return True
        if command == CommandType.SET_FREQUENCY:
            if self.active_process is None:
                raise RuidaEncodingError(
                    "SET_FREQUENCY requires active process metadata"
                )
            frequency = _integer(ops.frequency(index), "laser frequency")
            if frequency <= 0:
                raise RuidaEncodingError("laser frequency must be positive")
            self.state.frequency_hz = frequency
            self._record_process_state("frequency")
            return True
        if command == CommandType.SET_PULSE_WIDTH:
            if self.active_process is None:
                raise RuidaEncodingError(
                    "SET_PULSE_WIDTH requires active process metadata"
                )
            self.state.pulse_width_ns = _pulse_width_ns(
                ops.pulse_width(index),
                "laser pulse width",
            )
            self._record_process_state("pulse_width")
            return True
        return False

    def _handle_motion_command(
        self,
        command: CommandType,
        ops: Ops,
        index: int,
    ) -> bool:
        if command == CommandType.MOVE_TO:
            self._handle_travel(ops, index)
            return True
        if command == CommandType.LINE_TO:
            self._handle_line(ops, index)
            return True
        if command == CommandType.SCAN_LINE:
            self._handle_scan(ops, index)
            return True
        if command == CommandType.DWELL:
            self._handle_dwell(ops, index)
            return True
        if command in (
            CommandType.ARC_TO,
            CommandType.BEZIER_TO,
            CommandType.QUADRATIC_BEZIER_TO,
        ):
            raise RuidaEncodingError(
                f"{command.name} must be linearized before Ruida encoding"
            )
        return False

    def _handle_marker_command(
        self,
        command: CommandType,
        ops: Ops,
        index: int,
    ) -> bool:
        if command == CommandType.LAYER_START:
            self._start_layer(ops.layer_uid(index))
            return True
        if command == CommandType.LAYER_END:
            self._end_layer(ops.layer_uid(index))
            return True
        if command == CommandType.OPS_SECTION_START:
            self._start_section(ops, index)
            return True
        if command == CommandType.OPS_SECTION_END:
            self._end_section(ops, index)
            return True
        if command.name == "PROCESS_START":
            self._start_process(ops, index)
            return True
        if command.name == "PROCESS_END":
            self._end_process(ops, index)
            return True
        if command in (
            CommandType.JOB_START,
            CommandType.JOB_END,
            CommandType.WORKPIECE_START,
            CommandType.WORKPIECE_END,
            CommandType.STATE_BLOCK_START,
            CommandType.STATE_BLOCK_END,
        ):
            self._attach_pending_travels()
            return True
        return False

    @staticmethod
    def _reject_command(command: CommandType) -> None:
        if command in (
            CommandType.SET_COOLANT,
            CommandType.SET_HEAD_COOLANT,
            CommandType.SET_SPINDLE_RPM,
        ):
            raise RuidaEncodingError(
                f"Laser Ruida jobs do not support {command.name}"
            )
        raise RuidaEncodingError(
            f"Unsupported Ops command for Ruida: {command.name}"
        )

    @staticmethod
    def _positive_rate(ops: Ops, index: int) -> float:
        rate = _finite_number(ops.rate(index), "motion rate")
        if rate <= 0:
            raise RuidaEncodingError("motion rate must be positive")
        return rate

    def _handle_travel(self, ops: Ops, index: int) -> None:
        end = self._planar_endpoint(ops, index)
        self.pending_travels.append(self.api.TravelTo(end[0], end[1]))
        self.current_pos = end

    def _handle_dwell(self, ops: Ops, index: int) -> None:
        self._require_position()
        process = self._require_process()
        if process.kind != "vector" or self._motion_kind("vector") != "vector":
            raise RuidaEncodingError(
                "Ruida DWELL has controlled evidence only for vector motion"
            )
        if self.profile_name != "stationary-research":
            raise RuidaEncodingError(
                "Ruida DWELL requires the stationary-research job profile"
            )
        duration = _finite_number(
            ops.dwell_duration(index),
            "dwell duration",
        )
        if duration <= 0:
            raise RuidaEncodingError("dwell duration must be positive")
        if duration > 200:
            raise RuidaEncodingError(
                "Ruida stationary dwell cannot exceed 200 ms"
            )
        builder = self._builder_for("vector")
        self._attach_pending_travels(builder, self.current_pos)
        builder.append_event(self.api.Dwell(duration), None)

    def _handle_line(self, ops: Ops, index: int) -> None:
        start = self._require_position()
        end = self._planar_endpoint(ops, index)
        if _same_wire_position(start, end):
            self.current_pos = end
            return
        kind = self._motion_kind("vector")
        process = self._require_process()
        raster_mode = self.section_raster_mode or process.raster_mode
        if kind == "raster" and raster_mode == "VARIABLE_POWER":
            raise RuidaEncodingError(
                "Variable-power raster motion must use ScanLine"
            )
        if (
            kind == "raster"
            and process.kind == "mixed"
            and "power" in self.process_state_fields
            and self.state.power == 0
        ):
            self.pending_travels.append(self.api.TravelTo(end[0], end[1]))
            self.current_pos = end
            return
        if (
            kind == "vector"
            and process.power_mode == "dynamic"
            and self.state.power == 0
        ):
            self.pending_travels.append(self.api.TravelTo(end[0], end[1]))
            self.current_pos = end
            return
        raster_processing = None
        raster_axis = None
        if kind == "raster":
            raster_processing, raster_axis = self._raster_layout(
                start,
                end,
                raster_mode,
            )
        if kind == "raster" and process.kind != "mixed":
            self._validate_static_raster_line(process)
        builder = self._builder_for(
            kind,
            raster_axis,
            raster_processing=raster_processing,
            static_raster=kind == "raster",
        )
        self._attach_pending_travels(builder, start)
        if kind == "raster" and raster_processing == "native":
            builder.append_raster_direction(start, end)
        dynamic_power = (
            kind == "vector"
            and process.power_mode == "dynamic"
            and process.power is not None
            and not math.isclose(
                self.state.power,
                process.power,
                rel_tol=0,
                abs_tol=1e-9,
            )
        )
        if dynamic_power:
            if self.profile_name != "dynamic-power-research":
                raise RuidaEncodingError(
                    "Reduced positive vector power requires the "
                    "dynamic-power-research job profile"
                )
            if (
                self.config.dynamic_power_restore_contract != 1
                or self.api.dynamic_power_restore_contract != 1
            ):
                raise RuidaEncodingError(
                    "Reduced positive vector power requires a ruida-re "
                    "compiler with dynamic power restoration contract 1"
                )
            builder.uses_dynamic_power = True
            event = self.api.MarkWithPower(
                end[0],
                end[1],
                self._event_laser_channels(builder),
            )
        else:
            event = self.api.MarkTo(end[0], end[1])
        builder.append_event(event, self._planned_section_id(builder))
        self.current_pos = end

    def _handle_scan(self, ops: Ops, index: int) -> None:
        start = self._require_position()
        end = self._planar_endpoint(ops, index)
        if self._motion_kind("raster") != "raster":
            raise RuidaEncodingError(
                "ScanLine cannot appear in a vector process section"
            )
        samples = bytes(ops.scanline_data(index))
        if not samples:
            raise RuidaEncodingError("Ruida scan lines require power samples")
        process = self._require_process()
        self._validate_scan_samples(process, samples)
        if _same_wire_position(start, end):
            self.current_pos = end
            return
        raster_mode = self.section_raster_mode or process.raster_mode
        raster_processing, raster_axis = self._raster_layout(
            start,
            end,
            raster_mode,
        )
        if raster_processing == "planned-path":
            self._validate_planned_scan_samples(process, samples)
        if not any(samples):
            self.pending_travels.append(self.api.TravelTo(end[0], end[1]))
            self.current_pos = end
            return
        builder = self._builder_for(
            "raster",
            raster_axis,
            raster_processing=raster_processing,
        )
        self._attach_pending_travels(builder, start)
        if raster_processing == "native":
            builder.append_raster_direction(start, end)
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        for run_end, sample in _sample_runs(samples):
            fraction = run_end / len(samples)
            x = start[0] + dx * fraction
            y = start[1] + dy * fraction
            percent = sample * 100 / 255
            if sample:
                if raster_processing == "native":
                    builder.append_event(
                        self.api.SetModulation(percent),
                        None,
                    )
                builder.append_event(
                    self.api.MarkTo(x, y),
                    self._planned_section_id(builder),
                )
                builder.raster_powers.append(percent)
            else:
                builder.append_event(
                    self.api.TravelTo(x, y),
                    self._planned_section_id(builder),
                )
        self.current_pos = end

    def _raster_layout(
        self,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
        raster_mode: str | None,
    ) -> tuple[
        Literal["native", "planned-path"],
        Literal["horizontal", "vertical"] | None,
    ]:
        process = self._require_process()
        try:
            _raster_axis(start, end)
        except RuidaEncodingError as error:
            if raster_mode != "CONSTANT_POWER":
                raise RuidaEncodingError(
                    "Diagonal variable/grayscale or depth raster is not "
                    "supported by the evidenced Ruida profile"
                ) from error
            return "planned-path", None
        if process.raster_cross_hatch and raster_mode == "CONSTANT_POWER":
            return "planned-path", None
        return "native", self._resolve_raster_axis(start, end)

    def _validate_planned_scan_samples(
        self,
        process: _ProcessMetadata,
        samples: bytes,
    ) -> None:
        if process.power_mode != "static" or process.power is None:
            raise RuidaEncodingError(
                "Planned-path raster supports constant binary power only"
            )
        expected = process.power
        for sample in samples:
            if sample == 0:
                continue
            if not math.isclose(
                sample / 255,
                expected,
                rel_tol=0,
                abs_tol=_U8_POWER_TOLERANCE,
            ):
                raise RuidaEncodingError(
                    "Diagonal variable/grayscale raster modulation is not "
                    "supported by the evidenced Ruida profile"
                )

    def _resolve_raster_axis(
        self,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
    ) -> Literal["horizontal", "vertical"]:
        axis = _raster_axis(start, end)
        process = self._require_process()
        if process.kind != "raster" or process.raster_cross_hatch:
            return axis
        if self.resolved_section_raster_axis is None:
            self.resolved_section_raster_axis = axis
        elif self.resolved_section_raster_axis != axis:
            raise RuidaEncodingError(
                "Non-cross-hatch raster process mixes machine-space axes"
            )
        return axis

    @staticmethod
    def _validate_scan_samples(
        process: _ProcessMetadata,
        samples: bytes,
    ) -> None:
        if process.kind == "mixed":
            return
        if process.sample_power_encoding != "absolute_u8":
            raise RuidaEncodingError(
                "Ruida raster requires absolute_u8 power samples"
            )
        minimum = process.raster_min_power
        maximum = process.raster_max_power
        if minimum is None or maximum is None:
            raise RuidaEncodingError(
                "Raster metadata requires min and max output power"
            )
        for sample in samples:
            if sample == 0:
                continue
            power = sample / 255
            if not (
                minimum - _U8_POWER_TOLERANCE
                <= power
                <= maximum + _U8_POWER_TOLERANCE
            ):
                raise RuidaEncodingError(
                    "Raster sample exceeds its declared power range"
                )

    def _planar_endpoint(
        self,
        ops: Ops,
        index: int,
    ) -> tuple[float, float, float]:
        x, y, z = ops.endpoint(index)
        values = (
            _finite_number(x, "motion X coordinate"),
            _finite_number(y, "motion Y coordinate"),
            _finite_number(z, "motion Z coordinate"),
        )
        if abs(values[2]) > _POSITION_TOLERANCE:
            raise RuidaEncodingError(
                "Ruida job compiler supports planar Z=0 motion only"
            )
        extra = ops.extra_axes(index)
        if extra:
            raise RuidaEncodingError(
                "Ruida job compiler does not support extra-axis motion"
            )
        return values

    def _require_position(self) -> tuple[float, float, float]:
        if self.current_pos is None:
            raise RuidaEncodingError(
                "The first Ruida marking command requires an explicit MoveTo"
            )
        return self.current_pos

    def _motion_kind(
        self,
        inferred: Literal["vector", "raster"],
    ) -> Literal["vector", "raster"]:
        process = self._require_process()
        declared = self.section_kind
        if process.kind in ("vector", "raster"):
            if declared is not None and declared != process.kind:
                raise RuidaEncodingError(
                    "Process kind disagrees with Ops section kind"
                )
            declared = process.kind
        elif process.kind == "mixed" and declared is None:
            raise RuidaEncodingError(
                "Mixed processes require explicit Ops sections"
            )
        if (
            declared is not None
            and inferred == "raster"
            and declared != inferred
        ):
            raise RuidaEncodingError(
                "Raster motion cannot appear in a vector process"
            )
        return declared or inferred

    def _builder_for(
        self,
        kind: Literal["vector", "raster"],
        raster_axis: Literal["horizontal", "vertical"] | None = None,
        *,
        raster_processing: Literal["native", "planned-path"] | None = None,
        static_raster: bool = False,
    ) -> _LayerBuilder:
        process = self._require_process()
        self._validate_process_state(process)
        speed, power, air_assist = self._regime(process)
        if (
            kind == "vector"
            and process.kind != "mixed"
            and process.power_mode not in ("static", "dynamic")
        ):
            raise RuidaEncodingError(
                "Vector Ruida layers require explicit process power"
            )
        if (
            kind == "vector"
            and process.power_mode == "dynamic"
            and "power" not in self.process_state_fields
        ):
            raise RuidaEncodingError(
                "Dynamic vector power requires explicit Ops power state"
            )
        if kind == "raster" and raster_processing not in (
            "native",
            "planned-path",
        ):
            raise RuidaEncodingError(
                "Raster layers require an explicit processing mode"
            )
        if raster_processing == "native" and raster_axis is None:
            raise RuidaEncodingError("Native raster requires a scan axis")
        if raster_processing == "planned-path" and raster_axis is not None:
            raise RuidaEncodingError(
                "Planned-path raster cannot declare a native scan axis"
            )
        if (
            raster_processing == "planned-path"
            and self.profile_name != "planned-path-research"
        ):
            raise RuidaEncodingError(
                "Planned-path raster requires the "
                "planned-path-research job profile"
            )
        laser_index = self._laser_index(process)
        explicit_channels = self._requires_explicit_channels(laser_index)
        key = _LayerKey(
            kind=kind,
            speed_mm_s=speed / 60,
            power_percent=(
                power * 100 if kind == "vector" or static_raster else None
            ),
            air_assist=air_assist,
            laser_index=laser_index,
            color_rgb=process.color_rgb,
            process_uid=process.uid,
            layer_uid=self.active_layer_uid,
            raster_axis=raster_axis,
            raster_processing=(
                raster_processing if kind == "raster" else None
            ),
            explicit_channels=explicit_channels,
        )
        if self.active_builder is not None and self.active_builder.key == key:
            return self.active_builder
        builder = _LayerBuilder(
            key=key,
            process=process,
            inactive_power=self._inactive_power(
                laser_index,
                explicit_channels,
            ),
            frequency_hz=process.frequency_hz,
            pulse_width_ns=self._process_pulse_width_ns(process),
            z_offset_mm=process.z_offset_mm,
        )
        self.builders.append(builder)
        self.active_builder = builder
        return builder

    def _requires_explicit_channels(self, laser_index: int) -> bool:
        return laser_index == 2 or self.profile.laser_channel_mode is not None

    def _process_pulse_width_ns(
        self,
        process: _ProcessMetadata,
    ) -> int | None:
        if process.pulse_width_us is not None:
            return _pulse_width_ns(process.pulse_width_us, "pulse width")
        if self.profile_name == "fiber-research":
            return 0
        return None

    def _inactive_power(
        self,
        laser_index: int,
        required: bool,
    ) -> tuple[float, float] | None:
        if not required:
            return None
        inactive_index = 2 if laser_index == 1 else 1
        if not self.config.inactive_power_confirmed(inactive_index):
            raise RuidaEncodingError(
                f"Inactive laser {inactive_index} channel powers must be "
                "explicitly confirmed"
            )
        minimum, maximum = self.config.inactive_power(inactive_index)
        if minimum is None or maximum is None:
            raise RuidaEncodingError(
                f"Ruida laser {inactive_index} inactive stored powers must "
                "be configured explicitly"
            )
        return minimum, maximum

    def _event_laser_channels(
        self,
        builder: _LayerBuilder,
    ) -> tuple[Any, ...]:
        if not builder.key.explicit_channels:
            raise RuidaEncodingError(
                "Dynamic vector power requires explicit laser channels"
            )
        inactive = builder.inactive_power
        if inactive is None:
            raise RuidaEncodingError(
                "Dynamic vector inactive channel powers are not configured"
            )
        process = builder.process
        if process is None or process.min_power is None:
            raise RuidaEncodingError(
                "Dynamic vector layer minimum power is not declared"
            )
        effective_minimum = process.min_power * 100
        effective_maximum = self.state.power * 100
        channels = []
        for index in (1, 2):
            enabled = index == builder.key.laser_index
            minimum, maximum = (
                (effective_minimum, effective_maximum) if enabled else inactive
            )
            channels.append(
                self.api.LaserChannelPlan(
                    index=index,
                    enabled=enabled,
                    min_power_percent=minimum,
                    max_power_percent=maximum,
                )
            )
        return tuple(channels)

    def _regime(
        self,
        process: _ProcessMetadata,
    ) -> tuple[float, float, bool]:
        if process.kind == "mixed":
            required = {"power", "feed", "air"}
            missing = required - self.process_state_fields
            if missing:
                fields = ", ".join(sorted(missing))
                raise RuidaEncodingError(
                    f"Mixed process requires explicit Ops state: {fields}"
                )
            if self.state.feed_rate_mm_min is None:
                raise RuidaEncodingError(
                    "Mixed process requires an explicit feed rate"
                )
            if self.state.feed_rate_mm_min <= 0:
                raise RuidaEncodingError(
                    "Ruida marking motion requires a positive feed rate"
                )
            return (
                self.state.feed_rate_mm_min,
                self.state.power,
                self.state.air_assist,
            )
        if process.air_assist is None:
            raise RuidaEncodingError(
                "Laser process metadata requires air assist state"
            )
        if process.power is None:
            raise RuidaEncodingError(
                "Laser process metadata requires power settings"
            )
        return self._cut_speed(), process.power, process.air_assist

    @staticmethod
    def _validate_static_raster_line(process: _ProcessMetadata) -> None:
        powers = (
            process.power,
            process.raster_min_power,
            process.raster_max_power,
        )
        if process.power_mode != "static" or any(
            value is None for value in powers
        ):
            raise RuidaEncodingError(
                "Raster LineTo requires static layer power; "
                "dynamic raster motion must use ScanLine"
            )
        power = process.power
        minimum = process.raster_min_power
        maximum = process.raster_max_power
        assert power is not None
        assert minimum is not None
        assert maximum is not None
        if not all(
            math.isclose(
                value,
                power,
                rel_tol=0,
                abs_tol=1e-9,
            )
            for value in (minimum, maximum)
        ):
            raise RuidaEncodingError(
                "Raster LineTo requires equal process and layer power"
            )

    def _cut_speed(self) -> float:
        speed = self._require_process().cut_speed_mm_min
        if speed <= 0:
            raise RuidaEncodingError(
                "Ruida marking motion requires a positive feed rate"
            )
        return speed

    def _laser_index(self, process: _ProcessMetadata | None) -> int:
        if process is None:
            raise RuidaEncodingError("Ruida motion requires process metadata")
        if process.head_uid is None or process.head_tool_number is None:
            raise RuidaEncodingError(
                "Laser process metadata requires a head UID and tool number"
            )
        head = next(
            (
                mapping
                for mapping in self.config.head_mappings
                if mapping[0] == process.head_uid
            ),
            None,
        )
        if head is None or head[1] != process.head_tool_number:
            raise RuidaEncodingError(
                "Process head metadata disagrees with the machine"
            )
        if self.profile_name == "fiber-research" and head[2] != "fiber":
            raise RuidaEncodingError(
                "The fiber-research profile requires a fiber laser head"
            )
        laser_index = process.head_tool_number + 1
        if laser_index not in (1, 2):
            raise RuidaEncodingError(
                "Ruida job profiles support selected laser heads 1 or 2 only"
            )
        if laser_index == 2 and self.profile_name != "dual-laser-research":
            raise RuidaEncodingError(
                f"Ruida job profile {self.profile_name!r} does not support "
                "explicit laser head 2"
            )
        return laser_index

    def _require_process(self) -> _ProcessMetadata:
        if self.active_process is None:
            raise RuidaEncodingError(
                "Ruida marking motion requires ProcessStart metadata"
            )
        return self.active_process

    def _record_process_state(self, name: str) -> None:
        if self.active_process is not None:
            self.process_state_fields.add(name)

    def _validate_process_state(self, process: _ProcessMetadata) -> None:
        if process.kind != "mixed":
            checks = (
                (
                    "feed",
                    self.state.feed_rate_mm_min,
                    process.cut_speed_mm_min,
                    "cut speed",
                ),
                (
                    "rapid",
                    self.state.rapid_rate_mm_min,
                    process.travel_speed_mm_min,
                    "rapid speed",
                ),
            )
            for field_name, actual, expected, label in checks:
                if field_name not in self.process_state_fields:
                    continue
                if expected is None or actual is None:
                    raise RuidaEncodingError(
                        f"Process metadata and Ops {label} disagree"
                    )
                if math.isclose(
                    actual,
                    expected,
                    rel_tol=0,
                    abs_tol=1e-9,
                ):
                    continue
                raise RuidaEncodingError(
                    f"Process metadata and Ops {label} disagree"
                )
            if "power" in self.process_state_fields:
                expected = process.power
                if expected is None:
                    raise RuidaEncodingError(
                        "Process metadata and Ops power disagree"
                    )
                if process.power_mode == "dynamic":
                    minimum = process.min_power
                    maximum = (
                        process.max_power
                        if process.kind == "vector"
                        else expected
                    )
                    if (
                        minimum is None
                        or maximum is None
                        or not (
                            minimum - 1e-9
                            <= self.state.power
                            <= maximum + 1e-9
                        )
                    ):
                        raise RuidaEncodingError(
                            "Process metadata and Ops power disagree"
                        )
                elif not math.isclose(
                    self.state.power,
                    expected,
                    rel_tol=0,
                    abs_tol=1e-9,
                ):
                    raise RuidaEncodingError(
                        "Process metadata and Ops power disagree"
                    )
            if (
                "air" in self.process_state_fields
                and self.state.air_assist != process.air_assist
            ):
                raise RuidaEncodingError(
                    "Process metadata and Ops air assist disagree"
                )
        if (
            "head" in self.process_state_fields
            and self.state.head_uid != process.head_uid
        ):
            raise RuidaEncodingError(
                "Process metadata and Ops head UID disagree"
            )
        state_checks = (
            (
                "frequency",
                self.state.frequency_hz,
                process.frequency_hz,
                "frequency",
            ),
            (
                "pulse_width",
                self.state.pulse_width_ns,
                self._process_pulse_width_ns(process),
                "pulse width",
            ),
        )
        for field_name, actual, expected, label in state_checks:
            if field_name not in self.process_state_fields:
                continue
            if actual != expected:
                raise RuidaEncodingError(
                    f"Process metadata and Ops {label} disagree"
                )

    def _attach_pending_travels(
        self,
        builder: _LayerBuilder | None = None,
        start: tuple[float, float, float] | None = None,
    ) -> None:
        target = builder or self.active_builder
        if target is None:
            return
        if self.pending_travels:
            target.extend_events(
                self.pending_travels,
                self._planned_section_id(target),
            )
            self.pending_travels.clear()
        elif (
            not target.events
            and not target.raster_sections
            and start is not None
        ):
            target.append_event(
                self.api.TravelTo(start[0], start[1]),
                self._planned_section_id(target),
            )

    def _planned_section_id(
        self,
        builder: _LayerBuilder,
    ) -> int | None:
        if builder.key.raster_processing != "planned-path":
            return None
        if self.raster_section_id is None:
            self.raster_section_id = self.next_raster_section_id
            self.next_raster_section_id += 1
        return self.raster_section_id

    def _start_layer(self, uid: str) -> None:
        self._attach_pending_travels()
        if self.active_layer_uid is not None:
            raise RuidaEncodingError("Nested layer markers are invalid")
        self.active_layer_uid = uid
        self.active_builder = None

    def _end_layer(self, uid: str) -> None:
        self._attach_pending_travels()
        if self.active_layer_uid != uid:
            raise RuidaEncodingError("Layer end does not match layer start")
        self.active_layer_uid = None
        self.active_builder = None

    def _start_section(self, ops: Ops, index: int) -> None:
        if self.section_kind is not None:
            raise RuidaEncodingError("Nested Ops sections are invalid")
        section_type, _workpiece_uid, raster_mode = ops.section_params(index)
        kind: Literal["vector", "raster"] = (
            "raster" if section_type.name == "RASTER_FILL" else "vector"
        )
        raster_mode_name = (
            raster_mode.name if raster_mode is not None else None
        )
        self._validate_section(kind, raster_mode_name)
        if kind == "raster":
            self.active_builder = None
            self.resolved_section_raster_axis = None
            self.raster_section_id = self.next_raster_section_id
            self.next_raster_section_id += 1
        else:
            self._attach_pending_travels()
            if (
                self.active_builder is not None
                and self.active_builder.key.kind != kind
            ):
                self.active_builder = None
        self.section_kind = kind
        self.section_type_name = section_type.name
        self.section_raster_mode = raster_mode_name

    def _end_section(self, ops: Ops, index: int) -> None:
        self._attach_pending_travels()
        if self.section_kind is None:
            raise RuidaEncodingError("Ops section end has no matching start")
        section_type, _workpiece_uid, raster_mode = ops.section_params(index)
        kind = "raster" if section_type.name == "RASTER_FILL" else "vector"
        raster_mode_name = (
            raster_mode.name if raster_mode is not None else None
        )
        if (
            kind != self.section_kind
            or section_type.name != self.section_type_name
            or raster_mode_name != self.section_raster_mode
        ):
            raise RuidaEncodingError("Ops section markers do not match")
        if kind == "raster":
            self.active_builder = None
            self.resolved_section_raster_axis = None
            self.raster_section_id = None
        self.section_kind = None
        self.section_type_name = None
        self.section_raster_mode = None

    def _validate_section(
        self,
        kind: Literal["vector", "raster"],
        raster_mode: str | None,
    ) -> None:
        process = self._require_process()
        if kind == "vector":
            if raster_mode is not None:
                raise RuidaEncodingError(
                    "Vector Ops sections cannot declare a raster mode"
                )
            if process.kind == "raster":
                raise RuidaEncodingError(
                    "Process kind disagrees with Ops section kind"
                )
            return
        if process.kind == "vector":
            raise RuidaEncodingError(
                "Process kind disagrees with Ops section kind"
            )
        if raster_mode not in (
            "VARIABLE_POWER",
            "CONSTANT_POWER",
            "DEPTH_MAP",
        ):
            raise RuidaEncodingError(
                f"Unsupported Ops raster mode {raster_mode!r}"
            )
        if process.kind == "raster" and process.raster_mode != raster_mode:
            raise RuidaEncodingError(
                "Process raster mode disagrees with Ops section mode"
            )

    def _start_process(self, ops: Ops, index: int) -> None:
        self._attach_pending_travels()
        if self.active_process is not None:
            raise RuidaEncodingError("Nested process markers are invalid")
        uid = _process_uid(ops, index)
        process = _ProcessMetadata.from_json(
            uid,
            _process_params(ops, index),
        )
        self._validate_process(process)
        self.active_process = process
        self.resolved_section_raster_axis = None
        self.raster_section_id = None
        self.process_state_fields.clear()
        self.active_builder = None

    def _end_process(self, ops: Ops, index: int) -> None:
        self._attach_pending_travels()
        uid = _process_uid(ops, index)
        if self.active_process is None or self.active_process.uid != uid:
            raise RuidaEncodingError(
                "Process end does not match process start"
            )
        self._validate_process_state(self.active_process)
        self.active_process = None
        self.resolved_section_raster_axis = None
        self.raster_section_id = None
        self.process_state_fields.clear()
        self.active_builder = None

    def _validate_process(self, process: _ProcessMetadata) -> None:
        RuidaOpsAdapter._validate_process_motion(process)
        RuidaOpsAdapter._validate_process_power(process)
        self._validate_process_features(process)
        if process.kind == "raster":
            RuidaOpsAdapter._validate_raster_metadata(process)
        RuidaOpsAdapter._validate_raster_angle(process)

    @staticmethod
    def _validate_process_motion(process: _ProcessMetadata) -> None:
        if process.cut_speed_mm_min <= 0:
            raise RuidaEncodingError("Process cut speed must be positive")
        if process.travel_speed_mm_min <= 0:
            raise RuidaEncodingError("Process travel speed must be positive")
        if (
            process.head_tool_number is not None
            and process.head_tool_number < 0
        ):
            raise RuidaEncodingError(
                "Process head tool number cannot be negative"
            )

    @staticmethod
    def _validate_process_power(process: _ProcessMetadata) -> None:
        if process.power_mode not in (None, "static", "dynamic"):
            raise RuidaEncodingError(
                f"Unsupported process power mode {process.power_mode!r}"
            )
        power_values = (
            process.power,
            process.min_power,
            process.max_power,
        )
        if process.power_mode is None and any(
            value is not None for value in power_values
        ):
            raise RuidaEncodingError(
                "Power values require an explicit process power mode"
            )
        if process.power_mode is not None and any(
            value is None for value in power_values
        ):
            raise RuidaEncodingError(
                "Process power metadata requires value, min, and max"
            )
        if (
            process.min_power is not None
            and process.max_power is not None
            and process.min_power > process.max_power
        ):
            raise RuidaEncodingError(
                "Process minimum power cannot exceed maximum power"
            )
        if process.power_mode == "static":
            power = process.power
            minimum = process.min_power
            maximum = process.max_power
            assert power is not None
            assert minimum is not None
            assert maximum is not None
            if any(
                not math.isclose(
                    value,
                    power,
                    rel_tol=0,
                    abs_tol=1e-9,
                )
                for value in (minimum, maximum)
            ):
                raise RuidaEncodingError(
                    "Static process power requires equal value, min, and max"
                )
        if process.power_mode == "dynamic":
            power = process.power
            minimum = process.min_power
            maximum = process.max_power
            assert power is not None
            assert minimum is not None
            assert maximum is not None
            if not 0 <= minimum <= maximum <= power <= 1:
                raise RuidaEncodingError(
                    "Dynamic process power requires min <= max <= value"
                )

    def _validate_process_features(self, process: _ProcessMetadata) -> None:
        if process.frequency_hz is not None:
            if process.kind != "vector":
                raise RuidaEncodingError(
                    "Ruida layer frequency has evidence for vector layers only"
                )
            if self.profile_name != "rf-research":
                raise RuidaEncodingError(
                    "Ruida layer frequency requires the rf-research "
                    "job profile"
                )
            if not 10_000 <= process.frequency_hz <= 20_000:
                raise RuidaEncodingError(
                    "Ruida RF frequency must be between 10000 and 20000 Hz"
                )
        if process.pulse_width_us is not None:
            if process.kind != "vector":
                raise RuidaEncodingError(
                    "Ruida fiber pulse width has evidence for vector "
                    "layers only"
                )
            if self.profile_name != "fiber-research":
                raise RuidaEncodingError(
                    "Ruida fiber pulse width requires the fiber-research "
                    "job profile"
                )
            pulse_width_ns = _pulse_width_ns(
                process.pulse_width_us,
                "pulse width",
            )
            if not 0 <= pulse_width_ns <= 200:
                raise RuidaEncodingError(
                    "Ruida fiber pulse width must be between 0 and 200 ns"
                )
        if process.z_offset_mm is not None:
            if self.profile_name != "z-research":
                raise RuidaEncodingError(
                    "Logical layer Z offset requires the z-research "
                    "job profile"
                )
            if process.kind != "raster":
                raise RuidaEncodingError(
                    "Logical layer Z offset has evidence for raster "
                    "layers only"
                )
            if not 0 < abs(process.z_offset_mm) <= 1:
                raise RuidaEncodingError(
                    "Logical layer Z offset must be nonzero and within 1 mm"
                )
        if process.rotary:
            raise RuidaEncodingError(
                "Ruida job compiler does not support rotary motion"
            )
        if process.rotary_mode is not None or process.rotary_axis is not None:
            raise RuidaEncodingError(
                "Inactive rotary metadata must not declare mode or axis"
            )
        if process.z_motion:
            raise RuidaEncodingError(
                "Rayforge has no typed Ruida layer Z-offset contract; "
                "generic Z motion intent remains unsupported"
            )

    @staticmethod
    def _validate_raster_metadata(process: _ProcessMetadata) -> None:
        if process.raster_depth_mode not in (
            "power_modulated",
            "mask_scan",
            "dither",
            "multi_pass",
        ):
            raise RuidaEncodingError("Raster process metadata is required")
        if process.sample_power_encoding != "absolute_u8":
            raise RuidaEncodingError(
                "Ruida raster requires absolute_u8 power samples"
            )
        if process.raster_mode not in (
            "VARIABLE_POWER",
            "CONSTANT_POWER",
            "DEPTH_MAP",
        ):
            raise RuidaEncodingError(
                f"Unsupported raster mode {process.raster_mode!r}"
            )
        if process.raster_strategy not in (
            "unidirectional",
            "bidirectional",
        ):
            raise RuidaEncodingError("Raster scan strategy must be explicit")
        if process.raster_scan_axis not in (
            "horizontal",
            "vertical",
            "arbitrary",
            "mixed",
        ):
            raise RuidaEncodingError(
                f"Unsupported raster scan axis {process.raster_scan_axis!r}"
            )
        if process.raster_scan_mode not in (
            "segmented",
            "full_sweep",
        ):
            raise RuidaEncodingError("Raster scan mode must be explicit")
        if process.raster_cross_hatch != (process.raster_scan_axis == "mixed"):
            raise RuidaEncodingError(
                "Raster cross-hatch state disagrees with scan axis"
            )
        RuidaOpsAdapter._validate_raster_power_range(process)

    @staticmethod
    def _validate_raster_power_range(process: _ProcessMetadata) -> None:
        ranges = (
            (
                process.raster_min_power,
                process.min_power,
                "minimum",
            ),
            (
                process.raster_max_power,
                process.max_power,
                "maximum",
            ),
        )
        for raster_value, process_value, label in ranges:
            if (
                raster_value is None
                or process_value is None
                or not math.isclose(
                    raster_value,
                    process_value,
                    rel_tol=0,
                    abs_tol=1e-9,
                )
            ):
                raise RuidaEncodingError(
                    f"Raster {label} power disagrees with process power"
                )

    @staticmethod
    def _validate_raster_angle(process: _ProcessMetadata) -> None:
        angle = process.raster_scan_angle
        if angle is None:
            return
        declared = process.raster_scan_axis
        if declared is None:
            raise RuidaEncodingError(
                "Raster scan angle requires a source scan axis"
            )
        if declared == "mixed":
            return
        if declared != _source_raster_axis(angle):
            raise RuidaEncodingError(
                "Raster scan angle disagrees with source scan axis"
            )

    def _finish(self) -> None:
        self._attach_pending_travels()
        if self.active_process is not None:
            raise RuidaEncodingError("Process start has no matching end")
        if self.section_kind is not None:
            raise RuidaEncodingError("Ops section start has no matching end")
        if self.active_layer_uid is not None:
            raise RuidaEncodingError("Layer start has no matching end")


class RuidaEncoder(OpsEncoder):
    """Compile planar Rayforge Ops through ruida-re's public job API."""

    def __init__(
        self,
        profile: str = DEFAULT_RUIDA_JOB_PROFILE,
        *,
        config: _RuidaEncoderConfig | None = None,
    ) -> None:
        profile_name = normalize_ruida_job_profile(profile)
        if config is not None and config.profile_name != profile_name:
            raise RuidaEncodingError(
                "Ruida encoder profile disagrees with its config snapshot"
            )
        self.profile_name = profile_name
        self._config = config

    @classmethod
    def from_machine(cls, machine: Machine) -> RuidaEncoder:
        config = _snapshot_ruida_encoder_config(machine)
        return cls(config.profile_name, config=config)

    @classmethod
    def context_from_machine(
        cls,
        machine: Machine,
    ) -> tuple[RuidaEncoder, dict[str, Any]]:
        config = _snapshot_ruida_encoder_config(machine)
        return (
            cls(config.profile_name, config=config),
            config.token_payload(),
        )

    @staticmethod
    def token_payload(machine: Machine) -> dict[str, Any]:
        return ruida_encoder_token_payload(machine)

    def encode(
        self,
        ops: Ops,
        machine: Machine,
        doc: Doc,
    ) -> EncodedOutput:
        del doc
        if ops.len() == 0:
            return EncodedOutput(
                text="",
                op_map=MachineCodeOpMap(),
                driver_data={"binary": b"", "container": "rd"},
                payload=b"",
            )

        config = self._config or _snapshot_ruida_encoder_config(
            machine,
            self.profile_name,
        )
        adapter = RuidaOpsAdapter(
            machine,
            self.profile_name,
            config=config,
        )
        plan = adapter.build_plan(ops)
        result = adapter.api.RuidaJobCompiler(
            profile=adapter.profile,
        ).compile(plan)
        payload = result.encode_rd()
        text, op_map = self._display(ops, result)
        bounds = result.bounds
        return EncodedOutput(
            text=text,
            op_map=op_map,
            driver_data={
                "binary": payload,
                "container": "rd",
                "profile": result.profile.identifier,
                "bounds_mm": (
                    bounds.min_x_mm,
                    bounds.min_y_mm,
                    bounds.max_x_mm,
                    bounds.max_y_mm,
                ),
                "layer_count": len(plan.layers),
            },
            payload=payload,
            warnings=tuple(adapter.warnings),
        )

    @staticmethod
    def _display(ops: Ops, result: Any) -> tuple[str, MachineCodeOpMap]:
        bounds = result.bounds
        lines = [
            f"; RUIDA PROFILE {result.profile.identifier}",
            (
                "; BOUNDS "
                f"X:{bounds.min_x_mm:.3f}..{bounds.max_x_mm:.3f} "
                f"Y:{bounds.min_y_mm:.3f}..{bounds.max_y_mm:.3f}"
            ),
            f"; LAYERS {len(result.layer_bounds)}",
        ]
        op_map = MachineCodeOpMap()
        for index in range(ops.len()):
            line_number = len(lines)
            command = ops.command_type(index)
            lines.append(f"{index:04d} {command.name}")
            op_map.op_to_machine_code[index] = [line_number]
            op_map.machine_code_to_op[line_number] = index
        return "\n".join(lines), op_map
