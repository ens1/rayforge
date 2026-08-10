"""Compile Rayforge operations into complete Ruida programs."""

from __future__ import annotations

import importlib
import json
import math
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
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


class RuidaEncodingError(ValueError):
    """Raised when Ops cannot be represented by the proven Ruida API."""


@dataclass(frozen=True)
class _RuidaApi:
    JobPlan: Any
    LayerPlan: Any
    MarkTo: Any
    RuidaJobCompiler: Any
    SetModulation: Any
    TravelTo: Any


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

    required = (
        "JobPlan",
        "LayerPlan",
        "MarkTo",
        "RuidaJobCompiler",
        "SetModulation",
        "TravelTo",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Installed ruida-re lacks required API: {names}")
    return _RuidaApi(**{name: getattr(module, name) for name in required})


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


@dataclass
class _LayerBuilder:
    key: _LayerKey
    events: list[Any] = field(default_factory=list)
    raster_axes: set[str] = field(default_factory=set)
    raster_directions: list[int] = field(default_factory=list)
    raster_powers: list[float] = field(default_factory=list)
    process: _ProcessMetadata | None = None

    def can_merge(self, other: _LayerBuilder) -> bool:
        if self.key != other.key or self.process != other.process:
            return False
        if self.key.kind != "raster":
            return True
        strategy = self.process.raster_strategy if self.process else None
        directions = {*self.raster_directions, *other.raster_directions}
        return strategy != "unidirectional" or len(directions) <= 1

    def merge(self, other: _LayerBuilder) -> None:
        self.events.extend(other.events)
        self.raster_axes.update(other.raster_axes)
        self.raster_directions.extend(other.raster_directions)
        self.raster_powers.extend(other.raster_powers)

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

    def to_layer_plan(self, api: _RuidaApi, index: int) -> Any:
        values: dict[str, Any] = {
            "index": index,
            "speed_mm_s": self.key.speed_mm_s,
            "min_power_percent": self.key.power_percent,
            "max_power_percent": self.key.power_percent,
            "events": tuple(self.events),
            "kind": self.key.kind,
            "air_assist": self.key.air_assist,
            "color_rgb": self.key.color_rgb,
            "laser_index": self.key.laser_index,
        }
        if self.key.kind == "raster":
            minimum, maximum = self._raster_power_range()
            values["min_power_percent"] = minimum
            values["max_power_percent"] = maximum
            values["scan_axis"] = self._raster_axis()
            values["raster_strategy"] = self._raster_strategy()
        return api.LayerPlan(**values)

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

    def __init__(self, machine: Machine):
        self.machine = machine
        self.api = _load_ruida_api()
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
        self.resolved_section_raster_axis: (
            Literal["horizontal", "vertical"] | None
        ) = None
        self.warnings: list[str] = []

    def build_plan(self, ops: Ops) -> Any:
        for index in range(ops.len()):
            self._handle_command(ops, index)
        self._finish()
        if not self.builders:
            raise RuidaEncodingError("Ruida jobs must contain marking motion")
        builders = self._coalesced_builders()
        layers = tuple(
            builder.to_layer_plan(self.api, index)
            for index, builder in enumerate(builders)
        )
        return self.api.JobPlan(layers=layers)

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
            CommandType.SET_FREQUENCY,
            CommandType.SET_PULSE_WIDTH,
            CommandType.DWELL,
        ):
            raise RuidaEncodingError(
                f"Ruida job compiler does not support {command.name}"
            )
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
        raster_axis = (
            self._resolve_raster_axis(start, end) if kind == "raster" else None
        )
        if kind == "raster" and process.kind != "mixed":
            self._validate_static_raster_line(process)
        builder = self._builder_for(
            kind,
            raster_axis,
            static_raster=kind == "raster",
        )
        self._attach_pending_travels(builder, start)
        if kind == "raster":
            builder.append_raster_direction(start, end)
        builder.events.append(self.api.MarkTo(end[0], end[1]))
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
        raster_axis = self._resolve_raster_axis(start, end)
        if not any(samples):
            self.pending_travels.append(self.api.TravelTo(end[0], end[1]))
            self.current_pos = end
            return
        builder = self._builder_for("raster", raster_axis)
        self._attach_pending_travels(builder, start)
        builder.append_raster_direction(start, end)
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        for run_end, sample in _sample_runs(samples):
            fraction = run_end / len(samples)
            x = start[0] + dx * fraction
            y = start[1] + dy * fraction
            percent = sample * 100 / 255
            if sample:
                builder.events.append(self.api.SetModulation(percent))
                builder.events.append(self.api.MarkTo(x, y))
                builder.raster_powers.append(percent)
            else:
                builder.events.append(self.api.TravelTo(x, y))
        self.current_pos = end

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
        static_raster: bool = False,
    ) -> _LayerBuilder:
        process = self._require_process()
        self._validate_process_state(process)
        speed, power, air_assist = self._regime(process)
        if (
            kind == "vector"
            and process.kind != "mixed"
            and process.power_mode != "static"
        ):
            raise RuidaEncodingError(
                "Vector Ruida layers require static process power"
            )
        if (kind == "raster") != (raster_axis is not None):
            raise RuidaEncodingError(
                "Raster layers require exactly one scan axis"
            )
        key = _LayerKey(
            kind=kind,
            speed_mm_s=speed / 60,
            power_percent=(
                power * 100 if kind == "vector" or static_raster else None
            ),
            air_assist=air_assist,
            laser_index=self._laser_index(process),
            color_rgb=process.color_rgb,
            process_uid=process.uid,
            layer_uid=self.active_layer_uid,
            raster_axis=raster_axis,
        )
        if self.active_builder is not None and self.active_builder.key == key:
            return self.active_builder
        builder = _LayerBuilder(key=key, process=process)
        self.builders.append(builder)
        self.active_builder = builder
        return builder

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
                candidate
                for candidate in self.machine.heads
                if candidate.uid == process.head_uid
            ),
            None,
        )
        if head is None or head.tool_number != process.head_tool_number:
            raise RuidaEncodingError(
                "Process head metadata disagrees with the machine"
            )
        laser_index = process.head_tool_number + 1
        if laser_index != 1:
            raise RuidaEncodingError(
                "The proven Ruida profile supports laser index 1 only"
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
                ("power", self.state.power, process.power, "power"),
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

    def _attach_pending_travels(
        self,
        builder: _LayerBuilder | None = None,
        start: tuple[float, float, float] | None = None,
    ) -> None:
        target = builder or self.active_builder
        if target is None:
            return
        if self.pending_travels:
            target.events.extend(self.pending_travels)
            self.pending_travels.clear()
        elif not target.events and start is not None:
            target.events.append(self.api.TravelTo(start[0], start[1]))

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
        self._attach_pending_travels()
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
        elif (
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
        self.process_state_fields.clear()
        self.active_builder = None

    def _end_process(self, ops: Ops, index: int) -> None:
        self._attach_pending_travels()
        uid = _process_uid(ops, index)
        if self.active_process is None or self.active_process.uid != uid:
            raise RuidaEncodingError(
                "Process end does not match process start"
            )
        self.active_process = None
        self.resolved_section_raster_axis = None
        self.process_state_fields.clear()
        self.active_builder = None

    @staticmethod
    def _validate_process(process: _ProcessMetadata) -> None:
        RuidaOpsAdapter._validate_process_motion(process)
        RuidaOpsAdapter._validate_process_power(process)
        RuidaOpsAdapter._validate_process_features(process)
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

    @staticmethod
    def _validate_process_features(process: _ProcessMetadata) -> None:
        if process.frequency_hz:
            raise RuidaEncodingError(
                "Ruida job compiler does not support pulse frequency"
            )
        if process.pulse_width_us:
            raise RuidaEncodingError(
                "Ruida job compiler does not support pulse width"
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
                "Ruida job compiler does not support Z motion intent"
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

        adapter = RuidaOpsAdapter(machine)
        plan = adapter.build_plan(ops)
        result = adapter.api.RuidaJobCompiler().compile(plan)
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
