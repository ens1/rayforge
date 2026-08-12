# flake8: noqa: E402
"""UI tests for the laser step settings pages."""

from typing import Any, cast

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw
from laser_essentials.steps import EngraveStep
from laser_essentials.widgets.contour_page import (
    ContourStepSettingsPage,
    ThresholdRow,
)
from laser_essentials.widgets.material_test_grid_page import (
    MaterialTestGridSettingsPage,
)
from laser_essentials.widgets.raster_page import RasterSettingsPage
from laser_essentials.widgets.rows import (
    AirAssistRow,
    FrequencyRow,
    OffsetRow,
    PowerRow,
)

from rayforge.core.step_registry import step_registry
from rayforge.machine.driver.driver import PWMParams
from rayforge.ui_gtk.doceditor.step_settings.dialog import StepSettingsDialog
from rayforge.ui_gtk.doceditor.step_settings.pages import StepSettingsPage
from rayforge.ui_gtk.doceditor.step_settings.rows import (
    CutSpeedRow,
    HeadRow,
    TravelSpeedRow,
)


def _find(widget, cls):
    for row in widget._rows:
        if isinstance(row, cls):
            return row
    raise AssertionError(f"row {cls.__name__} not found in page")


def _contour_step(ui_context) -> Any:
    step_cls = step_registry.get("ContourStep")
    assert step_cls is not None
    return step_cls.create(ui_context)


@pytest.mark.ui
def test_contour_page_composes_step_and_laser_rows(
    editor, laser_machine, ui_context
):
    step = _contour_step(ui_context)
    page = ContourStepSettingsPage(editor, step)
    laser_page = page.laser_page()

    assert isinstance(page, StepSettingsPage)
    assert isinstance(page, Adw.PreferencesPage)
    assert isinstance(laser_page, StepSettingsPage)

    for cls in (OffsetRow, ThresholdRow):
        _find(page, cls)
    for cls in (
        PowerRow,
        CutSpeedRow,
        TravelSpeedRow,
        AirAssistRow,
        HeadRow,
    ):
        _find(laser_page, cls)


@pytest.mark.ui
def test_path_offset_insensitive_on_centerline(
    editor, laser_machine, ui_context
):
    step = _contour_step(ui_context)
    page = ContourStepSettingsPage(editor, step)
    offset = _find(page, OffsetRow)

    assert step.cut_side == "CENTERLINE"
    assert offset.widget.get_sensitive() is False

    step.cut_side = "OUTSIDE"
    step.updated.send(step)
    assert offset.widget.get_sensitive() is True


@pytest.mark.ui
def test_threshold_visible_only_when_rescanning(
    editor, laser_machine, ui_context
):
    step = _contour_step(ui_context)
    page = ContourStepSettingsPage(editor, step)
    threshold = _find(page, ThresholdRow)

    step.override_threshold = False
    step.updated.send(step)
    assert threshold.widget.get_visible() is False

    step.override_threshold = True
    step.updated.send(step)
    assert threshold.widget.get_visible() is True


@pytest.mark.ui
def test_head_change_does_not_touch_offset(editor, laser_machine, ui_context):
    step = _contour_step(ui_context)
    page = ContourStepSettingsPage(editor, step)
    laser_page = page.laser_page()
    offset_before = step.offset_mm

    target = laser_machine.heads[1]
    laser_page.head_row.head_changed.send(
        laser_page.head_row, head_uid=target.uid
    )
    assert step.selected_head_uid == target.uid
    assert step.offset_mm == offset_before


@pytest.mark.ui
def test_rf_frequency_zero_remains_visible_as_disabled(
    editor,
    laser_machine,
    ui_context,
    mocker,
):
    step = _contour_step(ui_context)
    step.frequency = 0
    mocker.patch.object(
        laser_machine,
        "get_pwm_params",
        return_value=PWMParams(
            frequency=20_000,
            min_frequency=10_000,
            max_frequency=20_000,
            frequency_zero_disables=True,
            pulse_width=None,
            min_pulse_width=None,
            max_pulse_width=None,
        ),
    )

    page = ContourStepSettingsPage(editor, step).laser_page()
    frequency = _find(page, FrequencyRow)
    adjustment = frequency.widget.get_adjustment()

    assert step.frequency == 0
    assert frequency.widget.get_value() == 0
    assert adjustment.get_lower() == 0
    assert adjustment.get_upper() == 20_000
    assert frequency.widget.get_subtitle() == (
        "0 disables; nonzero must be 10,000–20,000 Hz"
    )


@pytest.mark.ui
def test_raster_head_switch_does_not_apply_vector_pwm_defaults(
    editor,
    laser_machine,
    ui_context,
    mocker,
):
    params = PWMParams(
        frequency=20_000,
        min_frequency=10_000,
        max_frequency=20_000,
        frequency_zero_disables=True,
        pulse_width=None,
        min_pulse_width=None,
        max_pulse_width=None,
    )
    get_pwm_params = mocker.patch.object(
        laser_machine,
        "get_pwm_params",
        return_value=params,
    )
    step_cls = step_registry.get("EngraveStep")
    assert step_cls is not None
    step: Any = step_cls.create(ui_context)
    page = RasterSettingsPage(editor, step).laser_page()

    target = laser_machine.heads[1]
    page.head_row.head_changed.send(page.head_row, head_uid=target.uid)

    assert step.selected_head_uid == target.uid
    assert step.frequency == 0
    assert step.pulse_width == 0
    get_pwm_params.assert_not_called()


@pytest.mark.ui
def test_offset_row_uses_user_units(editor, laser_machine, ui_context):
    ui_context.config.unit_preferences["length"] = "in"
    step = _contour_step(ui_context)
    page = ContourStepSettingsPage(editor, step)
    offset = _find(page, OffsetRow)

    step.offset_mm = 25.4
    step.updated.send(step)

    assert offset.widget is not None
    assert offset.widget.get_value_in_base_units() == pytest.approx(25.4)
    assert offset.widget.get_value() == pytest.approx(1.0, abs=1e-2)


@pytest.mark.ui
def test_material_test_page_builds(editor, laser_machine, ui_context):
    step_cls = step_registry.get("MaterialTestStep")
    assert step_cls is not None
    page = MaterialTestGridSettingsPage(editor, step_cls.create(ui_context))
    assert isinstance(page, StepSettingsPage)


@pytest.mark.ui
def test_raster_page_builds(editor, laser_machine, ui_context):
    step_cls = step_registry.get("EngraveStep")
    assert step_cls is not None
    page = RasterSettingsPage(editor, step_cls.create(ui_context))
    assert isinstance(page, StepSettingsPage)


@pytest.mark.ui
def test_raster_scan_direction_updates_step_and_offset_sensitivity(
    editor,
    laser_machine,
    ui_context,
):
    step_cls = step_registry.get("EngraveStep")
    assert step_cls is not None
    step = cast(EngraveStep, step_cls.create(ui_context))
    page = RasterSettingsPage(editor, step)

    assert step.scan_strategy == "bidirectional"
    assert page.scan_strategy_row.get_selected() == 0
    assert page.bidir_x_offset_row.get_sensitive() is True

    page.scan_strategy_row.set_selected(1)

    assert step.scan_strategy == "unidirectional"
    assert page.bidir_x_offset_row.get_sensitive() is False

    page.scan_strategy_row.set_selected(0)

    assert step.scan_strategy == "bidirectional"
    assert page.bidir_x_offset_row.get_sensitive() is True


@pytest.mark.ui
def test_dialog_uses_contour_page(editor, laser_machine, ui_context):
    dialog = StepSettingsDialog(editor, _contour_step(ui_context))
    assert type(dialog.general_view).__name__ == "ContourStepSettingsPage"
    assert [title for title, _, _ in dialog._extra_pages] == ["Laser"]
    assert len(dialog._extra_buttons) == 1
    dialog.close()


@pytest.mark.ui
def test_dialog_initial_laser_page(editor, laser_machine, ui_context):
    dialog = StepSettingsDialog(editor, _contour_step(ui_context))
    dialog.set_initial_page("laser")
    assert dialog._extra_buttons[0].get_active() is True
    assert dialog.btn_step_settings.get_active() is False
    dialog.close()
