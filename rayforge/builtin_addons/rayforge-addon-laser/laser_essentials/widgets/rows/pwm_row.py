"""Laser PWM row widgets."""

from gettext import gettext as _
from typing import Any

from rayforge.ui_gtk.doceditor.step_settings.rows import SpinRow


class _PwmRow(SpinRow):
    """Base for rows shown only when the selected head has PWM."""

    def __init__(
        self,
        editor: Any,
        step: Any,
        attr: str,
        title: str,
        subtitle: str,
        lower: float = 1,
        upper: float = 100000,
        step_increment: float = 1,
        digits: int = 0,
        is_int: bool = True,
    ):
        super().__init__(
            editor,
            step,
            attr,
            title,
            subtitle,
            lower,
            upper,
            step_increment,
            digits,
            is_int=is_int,
        )

    def _sync_dependencies(self):
        machine = self.get_machine()
        head = self.get_selected_head()
        if machine is None or head is None:
            self.set_visible(False)
            return
        params = (
            machine.get_pwm_params(head)
            if self.step.PROCESS_KIND == "vector"
            else None
        )
        supported = (
            params is not None and getattr(params, self.attr, None) is not None
        )
        visible = supported or bool(getattr(self.step, self.attr, 0))
        self.set_visible(visible)
        if not visible:
            return
        if not supported:
            self.set_range(0, 100000)
            self.set_widget_value(getattr(self.step, self.attr, 0))
            return
        assert params is not None
        if self.attr == "frequency":
            if params.frequency_zero_disables:
                lower = 0
                self.widget.set_subtitle(
                    _("0 disables; nonzero must be 10,000–20,000 Hz")
                )
            else:
                lower = params.min_frequency
                self.widget.set_subtitle(self._subtitle)
            upper = params.max_frequency
        else:
            lower = params.min_pulse_width
            upper = params.max_pulse_width
        if lower is not None and upper is not None:
            self.set_range(lower, upper)
            self.set_widget_value(getattr(self.step, self.attr, 0))


class FrequencyRow(_PwmRow):
    """A spin row bound to the ``LaserStep.frequency`` attribute."""

    def __init__(self, editor: Any, step: Any):
        super().__init__(
            editor,
            step,
            "frequency",
            _("Frequency"),
            _("Laser PWM frequency in Hz"),
        )


class PulseWidthRow(_PwmRow):
    """A spin row bound to the ``LaserStep.pulse_width`` attribute."""

    def __init__(self, editor: Any, step: Any):
        super().__init__(
            editor,
            step,
            "pulse_width",
            _("Pulse Width"),
            _("Laser PWM pulse width in µs"),
            lower=0,
            step_increment=0.001,
            digits=3,
            is_int=False,
        )


class ZOffsetRow(SpinRow):
    """A typed logical layer Z offset for Ruida research jobs."""

    def __init__(self, editor: Any, step: Any):
        super().__init__(
            editor,
            step,
            "z_offset_mm",
            _("Logical Layer Z Offset"),
            _("Balanced relative raster-layer offset in mm"),
            -1,
            1,
            0.001,
            3,
            is_int=False,
        )

    def _sync_dependencies(self):
        machine = self.get_machine()
        visible = bool(
            machine
            and self.step.PROCESS_KIND == "raster"
            and machine.driver_name
            in {
                "RuidaDriver",
                "RuidaSerialDriver",
                "RuidaUdpProgramDriver",
            }
            and machine.driver_args.get("job_profile") == "z-research"
        ) or bool(getattr(self.step, "z_offset_mm", 0))
        self.set_visible(visible)
