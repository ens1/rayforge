"""Laser-domain step base class.

Intermediate base for all laser steps. Declares the laser process
attributes and the laser-specific behaviour (initial ops, summary,
settlers, serialization of the laser keys).
"""

from gettext import gettext as _
from typing import TYPE_CHECKING, Any, cast

from raygeo.ops import Ops
from raygeo.ops.state import AirAssistMode

from rayforge.core.step import Step
from rayforge.core.varset import (
    BoolVar,
    FloatVar,
    SliderFloatVar,
    VarSet,
)
from rayforge.machine.models.laser import LaserHead
from rayforge.shared.units.formatter import format_value

from ..capabilities import LaserHeadVar

if TYPE_CHECKING:
    from rayforge.machine.models.machine import Machine


class LaserStep(Step):
    """Base for all laser-domain steps. Owns laser attributes."""

    PROCESS_KIND = "vector"

    def __init__(self, typelabel, name=None):
        self.power: float = 1.0
        self.max_power: int = 1000
        self.air_assist: bool = False
        self.tab_power: float = 0.0
        self.frequency: int = 0
        self.pulse_width: float = 0.0
        self.z_offset_mm: float = 0.0
        super().__init__(typelabel, name=name)

    @classmethod
    def recipe_varset(cls) -> VarSet:
        return VarSet(
            vars=[
                LaserHeadVar(
                    description=_("Optionally force a specific laser head")
                ),
                SliderFloatVar(
                    key="power",
                    label=_("Power"),
                    default=0.8,
                    min_val=0.0,
                    max_val=1.0,
                    show_value=True,
                    format_suffix="%",
                ),
                *Step.recipe_varset().vars,
                SliderFloatVar(
                    key="tab_power",
                    label=_("Tab Power"),
                    description=_(
                        "Laser power at tab positions (% of cut power)"
                    ),
                    default=0.0,
                    min_val=0.0,
                    max_val=1.0,
                    show_value=True,
                    format_suffix="%",
                ),
                BoolVar(
                    key="air_assist",
                    label=_("Air Assist"),
                    default=False,
                ),
                FloatVar(
                    key="z_offset_mm",
                    label=_("Logical Layer Z Offset"),
                    description=_(
                        "Balanced relative Z offset for encoders that "
                        "support a layer-level Z envelope"
                    ),
                    default=0.0,
                    min_val=-1.0,
                    max_val=1.0,
                ),
            ]
        )

    @classmethod
    def recipe_varset_groups(cls) -> list[tuple[str, VarSet]]:
        """Split into a "Laser" group (inherited process settings) and a
        "Step Settings" group (attributes the concrete step adds)."""
        full = cls.recipe_varset()
        base_keys = {v.key for v in LaserStep.recipe_varset()}
        laser_vars = [v for v in full if v.key in base_keys]
        step_vars = [v for v in full if v.key not in base_keys]
        groups: list[tuple[str, VarSet]] = []
        if laser_vars:
            groups.append((_("Laser"), VarSet(vars=laser_vars)))
        if step_vars:
            groups.append((_("Step Settings"), VarSet(vars=step_vars)))
        return groups or [(_("Laser"), VarSet(vars=laser_vars))]

    def create_initial_ops(self) -> Ops:
        """Build the initial Ops object with step-wide machine settings."""
        ops = Ops()
        ops.set_power(self.power)
        ops.set_feed_rate(self.cut_speed)
        ops.set_rapid_rate(self.travel_speed)
        ops.set_air_assist(
            AirAssistMode.ON if self.air_assist else AirAssistMode.OFF
        )
        if self.frequency:
            ops.set_frequency(self.frequency)
        if self.pulse_width:
            ops.set_pulse_width(self.pulse_width)
        return ops

    def populate_payload(self, payload, machine: "Machine"):
        super().populate_payload(payload, machine)
        payload.power = self.power
        payload.air_assist = self.air_assist
        payload.frequency = self.frequency
        payload.pulse_width = self.pulse_width

    def get_process_metadata(self, machine, layer=None) -> dict[str, Any]:
        metadata = super().get_process_metadata(machine, layer)
        reduced_tab_power = self.power * self.tab_power
        dynamic_tabs = (
            0 < self.tab_power < 1 and reduced_tab_power != self.power
        )
        power_mode = "dynamic" if dynamic_tabs else "static"
        minimum = reduced_tab_power if dynamic_tabs else self.power
        metadata.update(
            {
                "power": {
                    "mode": power_mode,
                    "value": self.power,
                    "min": minimum,
                    "max": self.power,
                },
                "air_assist": self.air_assist,
                "frequency_hz": self.frequency or None,
                "pulse_width_us": self.pulse_width or None,
                "z_offset_mm": self.z_offset_mm or None,
            }
        )
        return metadata

    def get_settings(self) -> dict[str, Any]:
        """
        Bundles all physical process parameters into a dictionary.
        Only includes settings of the step itself, and not of producer,
        transformer, etc.
        """
        return {
            "power": self.power,
            "cut_speed": self.cut_speed,
            "travel_speed": self.travel_speed,
            "air_assist": self.air_assist,
            "pixels_per_mm": self.pixels_per_mm,
            "tab_power": self.tab_power,
            "frequency": self.frequency,
            "pulse_width": self.pulse_width,
            "z_offset_mm": self.z_offset_mm,
            "generated_workpiece_uid": self.generated_workpiece_uid,
        }

    def apply_import_settings(self, settings: dict[str, Any]) -> None:
        """Apply importer-provided laser settings this step owns."""
        super().apply_import_settings(settings)
        power = settings.get("power")
        if power is not None:
            self.set_power(power)

    def get_cache_params(self) -> dict[str, Any]:
        params = super().get_cache_params()
        params.update(
            {
                "power": self.power,
                "max_power": self.max_power,
                "air_assist": self.air_assist,
                "tab_power": self.tab_power,
                "frequency": self.frequency,
                "pulse_width": self.pulse_width,
                "z_offset_mm": self.z_offset_mm,
            }
        )
        return params

    def get_selected_laser(self, machine: "Machine") -> LaserHead | None:
        """Typed convenience — returns the selected LaserHead or None."""
        head = self.get_selected_head(machine)
        if isinstance(head, LaserHead):
            return head
        return None

    def apply_pwm_defaults(
        self,
        machine: "Machine",
        head: LaserHead,
    ) -> None:
        """Apply supported PWM defaults to evidenced vector processes."""
        if self.PROCESS_KIND != "vector":
            return
        params = machine.get_pwm_params(head)
        if params is None:
            return
        if params.frequency is not None:
            self.frequency = params.frequency
        if params.pulse_width is not None:
            self.pulse_width = params.pulse_width

    def set_power(self, power: float):
        if not (0.0 <= power <= 1.0):
            raise ValueError("Power must be between 0.0 and 1.0")
        if self.power != power:
            self.power = power
            self.updated.send(self)

    def set_air_assist(self, enabled: bool):
        if self.air_assist != enabled:
            self.air_assist = bool(enabled)
            self.updated.send(self)

    def set_tab_power(self, power: float):
        if not (0.0 <= power <= 1.0):
            raise ValueError("Tab power must be between 0.0 and 1.0")
        if self.tab_power != power:
            self.tab_power = power
            self.updated.send(self)

    def set_frequency(self, frequency: int):
        if self.frequency != frequency:
            self.frequency = int(frequency)
            self.updated.send(self)

    def set_pulse_width(self, width: float):
        if self.pulse_width != width:
            self.pulse_width = float(width)
            self.updated.send(self)

    def set_z_offset_mm(self, offset: float):
        offset = float(offset)
        if not -1.0 <= offset <= 1.0:
            raise ValueError("Logical layer Z offset must be within 1 mm")
        if self.z_offset_mm != offset:
            self.z_offset_mm = offset
            self.updated.send(self)

    def get_summary(self) -> str:
        power_percent = round(self.power * 100)
        speed_str = format_value(self.cut_speed, "speed")
        return _("{power_percent}% power, {speed_str}").format(
            power_percent=power_percent, speed_str=speed_str
        )

    def get_operation_color(self, head) -> str | None:
        """The head's cut color, used to represent cutting operations."""
        if isinstance(head, LaserHead):
            return head.cut_color
        return None

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result.update(
            {
                "power": self.power,
                "max_power": self.max_power,
                "air_assist": self.air_assist,
                "tab_power": self.tab_power,
                "frequency": self.frequency,
                "pulse_width": self.pulse_width,
                "z_offset_mm": self.z_offset_mm,
            }
        )
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LaserStep":
        step = cast("LaserStep", super().from_dict(data))
        step.power = data.get("power", step.power)
        step.max_power = data.get("max_power", step.max_power)
        step.air_assist = data.get("air_assist", step.air_assist)
        step.tab_power = data.get("tab_power", step.tab_power)
        step.frequency = data.get("frequency", step.frequency)
        step.pulse_width = data.get("pulse_width", step.pulse_width)
        step.z_offset_mm = data.get("z_offset_mm", step.z_offset_mm)
        return step

    @classmethod
    def _serialized_keys(cls) -> frozenset[str]:
        return super()._serialized_keys() | frozenset(
            {
                "power",
                "max_power",
                "air_assist",
                "tab_power",
                "frequency",
                "pulse_width",
                "z_offset_mm",
            }
        )
