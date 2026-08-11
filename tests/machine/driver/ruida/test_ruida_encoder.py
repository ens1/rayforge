"""Tests for the Rayforge-to-ruida-re job adapter."""

import json
from typing import Any

import cairo
import pytest
from raygeo.geo import Geometry
from raygeo.ops import Ops
from raygeo.ops.state import AirAssistMode
from raygeo.ops.transform.tabs import TabsSpec
from raygeo.ops.types import CommandType, RasterMode, SectionType
from raygeo.pipeline.execute import execute_stages
from ruida_re import (
    Dwell,
    KnownCommand,
    LaserChannelPlan,
    MarkTo,
    MarkWithPower,
    RasterSection,
    RuidaCodec,
    SetModulation,
    TravelTo,
)

from rayforge.core.doc import Doc
from rayforge.core.step_registry import step_registry
from rayforge.core.workpiece import WorkPiece
from rayforge.machine.driver.ruida.ruida_encoder import (
    RuidaEncoder,
    RuidaEncodingError,
    RuidaOpsAdapter,
    ruida_job_profile_vars,
)
from rayforge.machine.driver.ruida.ruida_serial_driver import (
    RuidaSerialDriver,
)
from rayforge.machine.models.laser import Laser, LaserType
from rayforge.pipeline.intent_builder import (
    IntentBuilder,
    job_encode_key,
    job_machinexform_key,
)


@pytest.fixture
def machine(isolated_machine):
    head = Laser()
    head.uid = "laser-0"
    head.tool_number = 0
    isolated_machine.heads.clear()
    isolated_machine.add_head(head)
    return isolated_machine


@pytest.fixture
def doc():
    return Doc()


def _opaque_black_surface(_workpiece, width, height):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    context.set_source_rgba(0, 0, 0, 1)
    context.paint()
    return surface


def _metadata(
    *,
    uid="process-1",
    kind="vector",
    power=0.5,
    min_power=None,
    max_power=None,
    cut_speed=600,
    rapid_speed=3000,
    air_assist=False,
    head_uid="laser-0",
    tool_number=0,
    frequency=None,
    pulse_width=None,
    power_mode=None,
    raster_axis="horizontal",
    raster_strategy="bidirectional",
    raster_mode="VARIABLE_POWER",
    depth_mode="power_modulated",
    sample_encoding="absolute_u8",
    scan_angle=0.0,
    z_offset=None,
    z_motion=False,
    rotary=False,
):
    minimum = power if min_power is None else min_power
    maximum = power if max_power is None else max_power
    mode = power_mode or (
        "dynamic"
        if kind == "raster" and depth_mode == "power_modulated"
        else "static"
    )
    raster = None
    if kind == "raster":
        raster = {
            "depth_mode": depth_mode,
            "raster_mode": raster_mode,
            "min_output_power": minimum,
            "max_output_power": maximum,
            "sample_power_encoding": sample_encoding,
            "scan_angle_degrees": scan_angle,
            "scan_axis": raster_axis,
            "scan_strategy": raster_strategy,
            "scan_mode": "full_sweep",
            "cross_hatch": raster_axis == "mixed",
        }
    return {
        "schema": "rayforge.process",
        "version": 1,
        "identity": {
            "uid": uid,
            "step_type": "TestStep",
            "name": "Test process",
            "color_rgb": [17, 34, 51],
        },
        "kind": kind,
        "motion": {
            "cut_speed_mm_min": cut_speed,
            "rapid_speed_mm_min": rapid_speed,
        },
        "head_uid": head_uid,
        "head_tool_number": tool_number,
        "power": {
            "mode": mode,
            "value": power,
            "min": minimum,
            "max": maximum,
        },
        "air_assist": air_assist,
        "frequency_hz": frequency,
        "pulse_width_us": pulse_width,
        "z_offset_mm": z_offset,
        "raster": raster,
        "axes": {
            "z_motion": z_motion,
            "rotary": rotary,
            "rotary_mode": None,
            "rotary_axis": None,
        },
    }


def _process_start(ops, metadata):
    uid = metadata["identity"]["uid"]
    ops.process_start(uid, json.dumps(metadata))


def _state(ops, metadata):
    ops.set_power(metadata["power"]["value"])
    ops.set_feed_rate(metadata["motion"]["cut_speed_mm_min"])
    ops.set_rapid_rate(metadata["motion"]["rapid_speed_mm_min"])
    ops.set_air_assist(
        AirAssistMode.ON if metadata["air_assist"] else AirAssistMode.OFF
    )
    ops.set_head(metadata["head_uid"])


def _process_end(ops, metadata):
    ops.process_end(metadata["identity"]["uid"])


def _vector_ops(metadata=None):
    metadata = metadata or _metadata()
    ops = Ops()
    ops.job_start()
    ops.layer_start("layer-1")
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(SectionType.VECTOR_OUTLINE, "workpiece-1")
    ops.move_to(20, 20)
    ops.line_to(30, 20)
    ops.ops_section_end(SectionType.VECTOR_OUTLINE)
    _process_end(ops, metadata)
    ops.layer_end("layer-1")
    ops.job_end()
    return ops


def _raster_ops(metadata=None):
    metadata = metadata or _metadata(
        kind="raster",
        power=0.9,
        min_power=0.1,
        max_power=0.9,
    )
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece-1",
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    ops.move_to(20, 20)
    ops.scan_to(22, 20, power_values=bytearray([26, 128, 230]))
    ops.move_to(22, 21)
    ops.scan_to(20, 21, power_values=bytearray([230, 128, 26]))
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    _process_end(ops, metadata)
    return ops


def _records(payload):
    program = RuidaCodec().decode(payload, container="rd")
    assert program.issues == []
    return [
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    ]


def _values(records, name):
    return [record.values for record in records if record.name == name]


def _configure_inactive_power(machine, index, minimum, maximum):
    machine.driver_args.update(
        {
            f"laser_{index}_inactive_min_power_percent": minimum,
            f"laser_{index}_inactive_max_power_percent": maximum,
            f"laser_{index}_inactive_powers_confirmed": True,
        }
    )


def _add_second_laser(machine):
    second = Laser()
    second.uid = "laser-1"
    second.tool_number = 1
    machine.add_head(second)
    return second


def test_empty_ops_produces_empty_artifact(machine, doc):
    result = RuidaEncoder().encode(Ops(), machine, doc)

    assert result.payload == b""
    assert result.driver_data["binary"] == b""
    assert result.driver_data["container"] == "rd"
    assert result.text == ""


def test_vector_plan_uses_process_contract(machine):
    plan = RuidaOpsAdapter(machine).build_plan(_vector_ops())

    assert len(plan.layers) == 1
    layer = plan.layers[0]
    assert layer.kind == "vector"
    assert layer.speed_mm_s == pytest.approx(10)
    assert layer.min_power_percent == pytest.approx(50)
    assert layer.max_power_percent == pytest.approx(50)
    assert layer.color_rgb == 0x112233
    assert layer.laser_index == 1
    assert layer.events == (TravelTo(20, 20), MarkTo(30, 20))


def test_encoder_returns_complete_portable_rd(machine, doc):
    result = RuidaEncoder().encode(_vector_ops(), machine, doc)
    records = _records(result.payload)

    assert result.payload == result.driver_data["binary"]
    assert result.driver_data["container"] == "rd"
    assert result.driver_data["profile"] == ("lightburn-2.1.03-ruida-644xs")
    assert records[0].name == "reference_absolute"
    assert records[-1].name == "end_of_file"
    assert _values(records, "layer_speed") == [
        {"layer": 0, "speed_mm_s": 10.0}
    ]
    values = _values(records, "layer_laser_1_max_power")
    assert values[0]["layer"] == 0
    assert values[0]["power_percent"] == pytest.approx(50, abs=0.004)


def test_ruida_contour_pipeline_linearizes_arcs_and_round_trips_rd(
    contour_step_class,
    test_machine_and_config,
):
    machine, context = test_machine_and_config
    machine.hydrate()
    machine.driver_name = RuidaSerialDriver.__name__
    machine.set_dialect_uid(None)
    machine.set_supports_arcs(True)
    step = contour_step_class.create(context, name="Ruida contour")
    step.power = 0.42
    step.cut_speed = 720
    step.travel_speed = 4800
    step.air_assist = True

    geometry = Geometry()
    geometry.move_to(0, 5)
    geometry.arc_to(10, 5, 5, 0, clockwise=True)
    geometry.arc_to(0, 5, -5, 0, clockwise=True)
    geometry.close_path()
    workpiece = WorkPiece(name="part")
    workpiece._edited_boundaries = geometry
    workpiece.set_size(10, 10)
    workpiece.pos = (20, 20)
    doc = Doc()
    workflow = doc.active_layer.workflow
    assert workflow is not None
    workflow.add_child(step)
    doc.active_layer.add_child(workpiece)

    nodes = IntentBuilder(machine=machine, generation_id=1).build(doc)
    completed = {}
    execute_stages(
        nodes,
        lambda node: completed.__setitem__(node.key, node),
    )
    machine_node = completed[job_machinexform_key()]
    assert machine_node.error is None, machine_node.error
    ops = machine_node.output.ops
    command_types = [ops.command_type(i) for i in range(ops.len())]

    assert RuidaSerialDriver.accepts_arc_ops is False
    assert CommandType.ARC_TO not in command_types
    assert command_types.count(CommandType.LINE_TO) > 2

    payload = (
        RuidaEncoder()
        .encode(
            ops,
            machine,
            doc,
        )
        .payload
    )
    assert payload is not None
    codec = RuidaCodec()
    program = codec.decode(payload, container="rd")

    assert program.issues == []
    assert codec.encode(program, container="rd") == payload


@pytest.mark.parametrize(
    (
        "power",
        "min_power_level",
        "max_power_level",
        "expected_min_percent",
        "expected_max_percent",
        "expected_black_sample",
    ),
    (
        pytest.param(0.8, 0.2, 0.6, 16, 48, 122, id="bounded-dynamic"),
        pytest.param(0.2, 0.0, 1.0, 0, 20, 51, id="default-20-percent"),
    ),
)
def test_engrave_pipeline_respects_dynamic_power_contract(
    engrave_step_class,
    test_machine_and_config,
    mocker,
    power,
    min_power_level,
    max_power_level,
    expected_min_percent,
    expected_max_percent,
    expected_black_sample,
):
    machine, context = test_machine_and_config
    machine.hydrate()
    step = engrave_step_class.create(context, name="Ruida engrave")
    step.power = power
    step.min_power_level = min_power_level
    step.max_power_level = max_power_level
    step.num_power_levels = 25
    step.auto_levels = False
    step.sample_interval_mm = 0.5
    step.line_interval_mm = 0.5

    mocker.patch.object(
        WorkPiece,
        "render_to_pixels",
        autospec=True,
        side_effect=_opaque_black_surface,
    )
    workpiece = WorkPiece(name="image")
    workpiece.set_size(3, 2)
    workpiece.pos = (20, 20)
    doc = Doc()
    workflow = doc.active_layer.workflow
    assert workflow is not None
    workflow.add_child(step)
    doc.active_layer.add_child(workpiece)

    nodes = IntentBuilder(machine=machine, generation_id=1).build(doc)
    completed = {}
    execute_stages(
        nodes,
        lambda node: completed.__setitem__(node.key, node),
    )
    machine_node = completed[job_machinexform_key()]
    assert machine_node.error is None, machine_node.error
    ops = machine_node.output.ops
    plan = RuidaOpsAdapter(machine).build_plan(ops)
    samples = [
        sample
        for index in range(ops.len())
        if ops.command_type(index) == CommandType.SCAN_LINE
        for sample in ops.scanline_data(index)
    ]

    assert samples
    assert max(samples) == expected_black_sample
    assert plan.layers[0].min_power_percent == pytest.approx(
        expected_min_percent
    )
    assert plan.layers[0].max_power_percent == pytest.approx(
        expected_max_percent
    )

    result = RuidaEncoder().encode(ops, machine, doc)
    assert result.payload is not None
    program = RuidaCodec().decode(result.payload, container="rd")
    assert program.issues == []


def test_rotated_engrave_resolves_machine_axis_and_round_trips_rd(
    engrave_step_class,
    test_machine_and_config,
    mocker,
):
    machine, context = test_machine_and_config
    machine.hydrate()
    step = engrave_step_class.create(context, name="Rotated Ruida engrave")
    step.auto_levels = False
    step.sample_interval_mm = 0.5
    step.line_interval_mm = 0.5
    mocker.patch.object(
        WorkPiece,
        "render_to_pixels",
        autospec=True,
        side_effect=_opaque_black_surface,
    )
    workpiece = WorkPiece(name="rotated image")
    workpiece.set_size(3, 2)
    workpiece.pos = (20, 20)
    workpiece.angle = 90
    doc = Doc()
    workflow = doc.active_layer.workflow
    assert workflow is not None
    workflow.add_child(step)
    doc.active_layer.add_child(workpiece)

    nodes = IntentBuilder(machine=machine, generation_id=1).build(doc)
    completed = {}
    execute_stages(
        nodes,
        lambda node: completed.__setitem__(node.key, node),
    )
    machine_node = completed[job_machinexform_key()]
    assert machine_node.error is None, machine_node.error
    ops = machine_node.output.ops
    process_documents = [
        json.loads(ops.process_params(index))
        for index in range(ops.len())
        if ops.command_type(index).name == "PROCESS_START"
    ]

    assert len(process_documents) == 1
    assert process_documents[0]["raster"]["scan_axis"] == "horizontal"
    plan = RuidaOpsAdapter(machine).build_plan(ops)
    assert [layer.scan_axis for layer in plan.layers] == ["vertical"]

    result = RuidaEncoder().encode(ops, machine, doc)
    assert result.payload is not None
    codec = RuidaCodec()
    program = codec.decode(result.payload, container="rd")

    assert program.issues == []
    assert codec.encode(program, container="rd") == result.payload


def test_multi_workpiece_engrave_resolves_each_section_axis(
    engrave_step_class,
    test_machine_and_config,
    mocker,
):
    machine, context = test_machine_and_config
    machine.hydrate()
    step = engrave_step_class.create(context, name="Multi-part Ruida engrave")
    step.auto_levels = False
    step.sample_interval_mm = 0.5
    step.line_interval_mm = 0.5
    mocker.patch.object(
        WorkPiece,
        "render_to_pixels",
        autospec=True,
        side_effect=_opaque_black_surface,
    )
    horizontal = WorkPiece(name="horizontal image")
    horizontal.set_size(3, 2)
    horizontal.pos = (20, 20)
    vertical = WorkPiece(name="vertical image")
    vertical.set_size(3, 2)
    vertical.pos = (40, 20)
    vertical.angle = 90
    doc = Doc()
    workflow = doc.active_layer.workflow
    assert workflow is not None
    workflow.add_child(step)
    doc.active_layer.add_child(horizontal)
    doc.active_layer.add_child(vertical)

    nodes = IntentBuilder(machine=machine, generation_id=1).build(doc)
    completed = {}
    execute_stages(
        nodes,
        lambda node: completed.__setitem__(node.key, node),
    )
    machine_node = completed[job_machinexform_key()]
    assert machine_node.error is None, machine_node.error
    ops = machine_node.output.ops
    process_starts = [
        index
        for index in range(ops.len())
        if ops.command_type(index).name == "PROCESS_START"
    ]

    assert len(process_starts) == 1
    plan = RuidaOpsAdapter(machine).build_plan(ops)
    assert [layer.scan_axis for layer in plan.layers] == [
        "horizontal",
        "vertical",
    ]

    result = RuidaEncoder().encode(ops, machine, doc)
    assert result.payload is not None
    codec = RuidaCodec()
    program = codec.decode(result.payload, container="rd")

    assert program.issues == []
    assert codec.encode(program, container="rd") == result.payload


@pytest.mark.parametrize(
    ("test_type", "include_labels"),
    (("Cut", True), ("Engrave", False)),
)
def test_material_pipeline_compiles_explicit_state_regimes(
    contour_step_class,
    test_machine_and_config,
    test_type,
    include_labels,
):
    del contour_step_class
    machine, context = test_machine_and_config
    machine.hydrate()
    material_test_class = step_registry.get("MaterialTestStep")
    assert material_test_class is not None
    step: Any = material_test_class.create(
        context,
        name="Ruida material test",
    )
    step.grid_dimensions = (2, 2)
    step.shape_size = 5
    step.spacing = 2
    step.test_type = test_type
    step.include_labels = include_labels

    workpiece = WorkPiece(name="grid")
    workpiece.set_size(50, 40)
    workpiece.pos = (20, 20)
    doc = Doc()
    workflow = doc.active_layer.workflow
    assert workflow is not None
    workflow.add_child(step)
    doc.active_layer.add_child(workpiece)

    nodes = IntentBuilder(machine=machine, generation_id=1).build(doc)
    completed = {}
    execute_stages(
        nodes,
        lambda node: completed.__setitem__(node.key, node),
    )
    machine_node = completed[job_machinexform_key()]
    assert machine_node.error is None, machine_node.error

    result = RuidaEncoder().encode(
        machine_node.output.ops,
        machine,
        doc,
    )
    assert result.payload is not None
    program = RuidaCodec().decode(result.payload, container="rd")

    assert program.issues == []
    assert result.driver_data["layer_count"] > 1


def test_default_engrave_material_with_labels_round_trips_rd(
    contour_step_class,
    test_machine_and_config,
):
    del contour_step_class
    machine, context = test_machine_and_config
    machine.hydrate()
    material_test_class = step_registry.get("MaterialTestStep")
    assert material_test_class is not None
    step: Any = material_test_class.create(
        context,
        name="Ruida engrave material test",
    )
    assert step.grid_dimensions == (5, 5)
    assert step.include_labels is True
    step.test_type = "Engrave"

    workpiece = WorkPiece(name="grid")
    workpiece.set_size(100, 100)
    workpiece.pos = (20, 20)
    doc = Doc()
    workflow = doc.active_layer.workflow
    assert workflow is not None
    workflow.add_child(step)
    doc.active_layer.add_child(workpiece)

    nodes = IntentBuilder(machine=machine, generation_id=1).build(doc)
    completed = {}
    execute_stages(
        nodes,
        lambda node: completed.__setitem__(node.key, node),
    )
    machine_node = completed[job_machinexform_key()]
    assert machine_node.error is None, machine_node.error
    ops = machine_node.output.ops
    sections = ops.sections()

    assert any(
        section.section_type == SectionType.VECTOR_OUTLINE
        and section.raster_mode is None
        for section in sections
    )
    assert any(
        section.section_type == SectionType.RASTER_FILL
        and section.raster_mode == RasterMode.CONSTANT_POWER
        for section in sections
    )

    result = RuidaEncoder().encode(ops, machine, doc)
    assert result.payload is not None
    codec = RuidaCodec()
    program = codec.decode(result.payload, container="rd")

    assert program.issues == []
    assert codec.encode(program, container="rd") == result.payload


def test_full_power_does_not_wrap_to_zero(machine, doc):
    result = RuidaEncoder().encode(
        _vector_ops(_metadata(power=1.0)),
        machine,
        doc,
    )

    assert _values(_records(result.payload), "laser_1_max_power") == [
        {"power_percent": 100.0}
    ]


def test_raster_samples_are_absolute_not_scaled_by_process_power(machine):
    plan = RuidaOpsAdapter(machine).build_plan(_raster_ops())

    layer = plan.layers[0]
    modulations = [
        event.percent
        for event in layer.events
        if isinstance(event, SetModulation)
    ]
    assert layer.kind == "raster"
    assert layer.min_power_percent == pytest.approx(10)
    assert layer.max_power_percent == pytest.approx(90)
    assert layer.scan_axis == "horizontal"
    assert layer.raster_strategy == "bidirectional"
    assert modulations[:3] == pytest.approx(
        [26 * 100 / 255, 128 * 100 / 255, 230 * 100 / 255]
    )


def test_raster_compiles_immediate_power_records(machine, doc):
    result = RuidaEncoder().encode(_raster_ops(), machine, doc)
    powers = _values(_records(result.payload), "immediate_power_1")

    assert [value["power_percent"] for value in powers[:3]] == pytest.approx(
        [26 * 100 / 255, 128 * 100 / 255, 230 * 100 / 255],
        abs=0.004,
    )


def test_raster_samples_must_stay_within_declared_power_range(machine):
    metadata = _metadata(
        kind="raster",
        power=0.2,
        min_power=0.1,
        max_power=0.2,
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20)
    ops.scan_to(30, 20, power_values=bytearray([255]))

    with pytest.raises(RuidaEncodingError, match="declared power range"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_raster_and_process_power_bounds_must_match(machine):
    metadata = _metadata(
        kind="raster",
        power=0.2,
        min_power=0.1,
        max_power=0.2,
    )
    metadata["raster"]["max_output_power"] = 1.0
    ops = Ops()
    _process_start(ops, metadata)

    with pytest.raises(RuidaEncodingError, match="maximum power disagrees"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_separate_processes_become_ordered_ruida_layers(machine):
    first = _metadata(uid="first", power=0.25)
    second = _metadata(uid="second", power=0.75, cut_speed=1200)
    ops = Ops()
    for metadata, y in ((first, 20), (second, 21)):
        _process_start(ops, metadata)
        _state(ops, metadata)
        ops.move_to(20, y)
        ops.line_to(30, y)
        _process_end(ops, metadata)

    plan = RuidaOpsAdapter(machine).build_plan(ops)

    assert [layer.index for layer in plan.layers] == [0, 1]
    assert [layer.speed_mm_s for layer in plan.layers] == [10, 20]
    assert [layer.max_power_percent for layer in plan.layers] == [25, 75]


def test_same_process_vector_sections_share_one_ruida_layer(machine):
    metadata = _metadata()
    ops = Ops()
    _process_start(ops, metadata)
    for workpiece_uid, y in (("first", 20), ("second", 30)):
        ops.ops_section_start(SectionType.VECTOR_OUTLINE, workpiece_uid)
        ops.move_to(20, y)
        ops.line_to(30, y)
        ops.ops_section_end(SectionType.VECTOR_OUTLINE)
    _process_end(ops, metadata)

    plan = RuidaOpsAdapter(machine).build_plan(ops)

    assert len(plan.layers) == 1
    assert plan.layers[0].events == (
        TravelTo(20, 20),
        MarkTo(30, 20),
        TravelTo(20, 30),
        MarkTo(30, 30),
    )


def test_mixed_process_uses_explicit_state_regimes(machine):
    metadata = _metadata(kind="mixed")
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(SectionType.VECTOR_OUTLINE, "grid")
    ops.set_power(0.25)
    ops.set_feed_rate(600)
    ops.move_to(20, 20)
    ops.line_to(30, 20)
    ops.set_power(0.75)
    ops.set_feed_rate(1200)
    ops.move_to(20, 30)
    ops.line_to(30, 30)
    ops.ops_section_end(SectionType.VECTOR_OUTLINE)
    _process_end(ops, metadata)

    plan = RuidaOpsAdapter(machine).build_plan(ops)

    assert [layer.speed_mm_s for layer in plan.layers] == [10, 20]
    assert [layer.max_power_percent for layer in plan.layers] == [25, 75]


def test_mixed_process_raster_line_uses_explicit_static_state(machine):
    metadata = _metadata(kind="mixed")
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.set_power(0.25)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "grid",
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    ops.move_to(20, 20)
    ops.line_to(30, 20)
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    _process_end(ops, metadata)

    layer = RuidaOpsAdapter(machine).build_plan(ops).layers[0]

    assert layer.kind == "raster"
    assert layer.min_power_percent == pytest.approx(25)
    assert layer.max_power_percent == pytest.approx(25)
    assert layer.events == (TravelTo(20, 20), MarkTo(30, 20))


def test_mixed_variable_power_raster_requires_scan_line(machine):
    metadata = _metadata(kind="mixed")
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "grid",
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    ops.move_to(20, 20)
    ops.line_to(30, 20)

    with pytest.raises(RuidaEncodingError, match="must use ScanLine"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_mixed_raster_zero_power_lines_are_travel(machine):
    metadata = _metadata(kind="mixed")
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "grid",
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    ops.move_to(20, 20)
    ops.set_power(0)
    ops.line_to(21, 20)
    ops.set_power(0.25)
    ops.line_to(30, 20)
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    _process_end(ops, metadata)

    layer = RuidaOpsAdapter(machine).build_plan(ops).layers[0]

    assert layer.events == (
        TravelTo(20, 20),
        TravelTo(21, 20),
        MarkTo(30, 20),
    )


def test_raster_section_mode_must_match_process_metadata(machine):
    metadata = _metadata(kind="raster")
    ops = Ops()
    _process_start(ops, metadata)

    with pytest.raises(RuidaEncodingError, match="raster mode disagrees"):
        ops.ops_section_start(
            SectionType.RASTER_FILL,
            "workpiece",
            raster_mode=RasterMode.CONSTANT_POWER,
        )
        RuidaOpsAdapter(machine).build_plan(ops)


def test_text_contains_profile_bounds_and_op_mapping(machine, doc):
    ops = _vector_ops()
    result = RuidaEncoder().encode(ops, machine, doc)
    lines = result.text.splitlines()

    assert lines[0].startswith("; RUIDA PROFILE ")
    assert lines[1] == "; BOUNDS X:20.000..30.000 Y:20.000..20.000"
    assert lines[2] == "; LAYERS 1"
    assert result.op_map.op_to_machine_code[0] == [3]
    assert result.op_map.machine_code_to_op[3] == 0


def test_rapid_rate_is_explicitly_reported_as_controller_owned(machine, doc):
    result = RuidaEncoder().encode(_vector_ops(), machine, doc)

    assert result.warnings == (
        "Ruida TravelTo uses the controller-configured rapid rate",
    )
    assert "warnings" not in result.driver_data


def test_marking_without_process_metadata_is_rejected(machine):
    ops = Ops()
    ops.move_to(20, 20)
    ops.line_to(30, 20)

    with pytest.raises(RuidaEncodingError, match="ProcessStart"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_invalid_process_schema_is_rejected(machine):
    metadata = _metadata()
    metadata["version"] = 2
    ops = Ops()
    _process_start(ops, metadata)

    with pytest.raises(RuidaEncodingError, match="version 1"):
        RuidaOpsAdapter(machine).build_plan(ops)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (_metadata(frequency=1000), "rf-research"),
        (_metadata(pulse_width=20), "pulse width"),
        (_metadata(rotary=True), "rotary motion"),
        (
            _metadata(kind="raster", sample_encoding="relative_u8"),
            "absolute_u8",
        ),
    ],
)
def test_unsupported_process_features_fail_closed(machine, metadata, message):
    ops = Ops()
    _process_start(ops, metadata)

    with pytest.raises(RuidaEncodingError, match=message):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_source_raster_angle_must_match_source_axis(machine):
    metadata = _metadata(
        kind="raster",
        raster_axis="horizontal",
        scan_angle=45,
    )
    ops = Ops()
    _process_start(ops, metadata)

    with pytest.raises(RuidaEncodingError, match="source scan axis"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_second_ruida_laser_is_rejected_by_profile(machine):
    second = Laser()
    second.uid = "laser-1"
    second.tool_number = 1
    machine.add_head(second)
    metadata = _metadata(head_uid="laser-1", tool_number=1)

    with pytest.raises(RuidaEncodingError, match="laser head 2"):
        RuidaOpsAdapter(machine).build_plan(_vector_ops(metadata))


def test_state_and_process_metadata_must_agree(machine):
    metadata = _metadata(power=0.5)
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.set_power(0.6)
    ops.move_to(20, 20)
    ops.line_to(30, 20)

    with pytest.raises(RuidaEncodingError, match="Ops power disagree"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_nonplanar_geometry_is_rejected(machine):
    metadata = _metadata()
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20, 1)

    with pytest.raises(RuidaEncodingError, match="planar Z=0"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_z_motion_intent_is_rejected_even_with_planar_geometry(machine):
    metadata = _metadata(z_motion=True)
    ops = Ops()
    _process_start(ops, metadata)

    with pytest.raises(RuidaEncodingError, match="Z motion intent"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_host_expanded_depth_map_at_zero_z_is_supported(machine):
    metadata = _metadata(
        kind="raster",
        power=0.4,
        raster_mode="DEPTH_MAP",
        depth_mode="multi_pass",
        z_motion=False,
    )
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece-1",
        raster_mode=RasterMode.DEPTH_MAP,
    )
    ops.move_to(20, 20, 0)
    ops.line_to(30, 20, 0)
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.DEPTH_MAP,
    )
    _process_end(ops, metadata)

    layer = RuidaOpsAdapter(machine).build_plan(ops).layers[0]

    assert layer.kind == "raster"
    assert layer.min_power_percent == pytest.approx(40)
    assert layer.events == (
        TravelTo(20, 20),
        MarkTo(30, 20),
    )

    records = _records(RuidaEncoder().encode(ops, machine, Doc()).payload)
    assert _values(records, "immediate_power_1") == []


def test_dynamic_raster_line_paths_are_rejected(machine):
    metadata = _metadata(
        kind="raster",
        power=0.9,
        min_power=0.1,
        max_power=0.9,
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20)
    ops.line_to(30, 20)

    with pytest.raises(RuidaEncodingError, match="must use ScanLine"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_zero_raster_samples_are_travel_not_zero_power_marks(machine):
    metadata = _metadata(
        kind="raster",
        power=1.0,
        min_power=0.1,
        max_power=1.0,
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20)
    ops.scan_to(
        24,
        20,
        power_values=bytearray([0, 128, 0, 255]),
    )
    _process_end(ops, metadata)

    layer = RuidaOpsAdapter(machine).build_plan(ops).layers[0]

    assert layer.events == (
        TravelTo(20, 20),
        TravelTo(21, 20),
        SetModulation(128 * 100 / 255),
        MarkTo(22, 20),
        TravelTo(23, 20),
        SetModulation(100),
        MarkTo(24, 20),
    )


def test_raster_samples_compile_as_constant_power_spans(machine):
    metadata = _metadata(
        kind="raster",
        power=128 / 255,
        min_power=128 / 255,
        max_power=128 / 255,
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20)
    ops.scan_to(
        26,
        20,
        power_values=bytearray([0, 0, 128, 128, 128, 0]),
    )
    _process_end(ops, metadata)

    layer = RuidaOpsAdapter(machine).build_plan(ops).layers[0]

    assert layer.events == (
        TravelTo(20, 20),
        TravelTo(22, 20),
        SetModulation(128 * 100 / 255),
        MarkTo(25, 20),
        TravelTo(26, 20),
    )


def test_unlinearized_curves_are_rejected(machine):
    metadata = _metadata()
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20)
    ops.arc_to(30, 20, 5, 0)

    with pytest.raises(RuidaEncodingError, match="must be linearized"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_submicron_mark_segments_are_ignored(machine):
    metadata = _metadata()
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20)
    ops.line_to(20.0001, 20.0001)
    ops.line_to(30, 20)
    _process_end(ops, metadata)

    layer = RuidaOpsAdapter(machine).build_plan(ops).layers[0]

    assert layer.events == (TravelTo(20, 20), MarkTo(30, 20))


def test_diagonal_raster_is_rejected(machine):
    metadata = _metadata(
        kind="raster",
        raster_axis="mixed",
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20)
    ops.scan_to(22, 22, power_values=bytearray([128, 128]))

    with pytest.raises(RuidaEncodingError, match="variable/grayscale"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_source_arbitrary_raster_can_resolve_to_cardinal_machine_axis(machine):
    metadata = _metadata(
        kind="raster",
        raster_axis="arbitrary",
        scan_angle=45,
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20)
    ops.scan_to(20, 30, power_values=bytearray([128]))
    ops.move_to(21, 30)
    ops.scan_to(21, 20, power_values=bytearray([128]))
    _process_end(ops, metadata)

    layer = RuidaOpsAdapter(machine).build_plan(ops).layers[0]

    assert layer.scan_axis == "vertical"
    assert layer.raster_strategy == "bidirectional"


def test_raster_axis_uses_wire_quantized_coordinates(machine):
    metadata = _metadata(kind="raster")
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20.0004)
    ops.scan_to(30, 20.00049, power_values=bytearray([128]))
    _process_end(ops, metadata)

    layer = RuidaOpsAdapter(machine).build_plan(ops).layers[0]

    assert layer.scan_axis == "horizontal"


def test_transformed_unidirectional_raster_is_reflection_safe(machine):
    metadata = _metadata(
        kind="raster",
        raster_strategy="unidirectional",
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 30)
    ops.scan_to(20, 20, power_values=bytearray([128]))
    ops.move_to(21, 30)
    ops.scan_to(21, 20, power_values=bytearray([128]))
    _process_end(ops, metadata)

    layer = RuidaOpsAdapter(machine).build_plan(ops).layers[0]

    assert layer.scan_axis == "vertical"
    assert layer.raster_strategy == "unidirectional"


def test_transformed_unidirectional_raster_rejects_opposing_moves(machine):
    metadata = _metadata(
        kind="raster",
        raster_strategy="unidirectional",
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 30)
    ops.scan_to(20, 20, power_values=bytearray([128]))
    ops.move_to(21, 20)
    ops.scan_to(21, 30, power_values=bytearray([128]))
    _process_end(ops, metadata)

    with pytest.raises(RuidaEncodingError, match="opposing scan moves"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_non_cross_hatch_raster_rejects_mixed_machine_axes(machine):
    metadata = _metadata(kind="raster")
    ops = Ops()
    _process_start(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece-1",
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    ops.move_to(20, 20)
    ops.scan_to(30, 20, power_values=bytearray([128]))
    ops.move_to(30, 20)
    ops.scan_to(30, 30, power_values=bytearray([128]))
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    _process_end(ops, metadata)

    with pytest.raises(RuidaEncodingError, match="mixes machine-space axes"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_non_cross_hatch_sections_resolve_axes_independently(machine):
    metadata = _metadata(kind="raster")
    ops = Ops()
    _process_start(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece-1",
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    ops.move_to(20, 20)
    ops.scan_to(30, 20, power_values=bytearray([128]))
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece-2",
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    ops.move_to(40, 20)
    ops.scan_to(40, 30, power_values=bytearray([128]))
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    _process_end(ops, metadata)

    plan = RuidaOpsAdapter(machine).build_plan(ops)

    assert [layer.scan_axis for layer in plan.layers] == [
        "horizontal",
        "vertical",
    ]


def test_reflected_unidirectional_sections_use_separate_layers(machine):
    metadata = _metadata(
        kind="raster",
        raster_strategy="unidirectional",
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece-1",
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    ops.move_to(20, 20)
    ops.scan_to(30, 20, power_values=bytearray([128]))
    ops.move_to(20, 21)
    ops.scan_to(30, 21, power_values=bytearray([128]))
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece-2",
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    ops.move_to(50, 20)
    ops.scan_to(40, 20, power_values=bytearray([128]))
    ops.move_to(50, 21)
    ops.scan_to(40, 21, power_values=bytearray([128]))
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    _process_end(ops, metadata)

    plan = RuidaOpsAdapter(machine).build_plan(ops)

    assert len(plan.layers) == 2
    assert [layer.scan_axis for layer in plan.layers] == [
        "horizontal",
        "horizontal",
    ]
    assert [layer.raster_strategy for layer in plan.layers] == [
        "unidirectional",
        "unidirectional",
    ]


def test_compatible_unidirectional_sections_share_one_layer(machine):
    metadata = _metadata(
        kind="raster",
        raster_strategy="unidirectional",
    )
    ops = Ops()
    _process_start(ops, metadata)
    for uid, y in (("workpiece-1", 20), ("workpiece-2", 30)):
        ops.ops_section_start(
            SectionType.RASTER_FILL,
            uid,
            raster_mode=RasterMode.VARIABLE_POWER,
        )
        ops.move_to(20, y)
        ops.scan_to(30, y, power_values=bytearray([128]))
        ops.ops_section_end(
            SectionType.RASTER_FILL,
            raster_mode=RasterMode.VARIABLE_POWER,
        )
    _process_end(ops, metadata)

    plan = RuidaOpsAdapter(machine).build_plan(ops)

    assert len(plan.layers) == 1
    assert plan.layers[0].scan_axis == "horizontal"
    assert plan.layers[0].raster_strategy == "unidirectional"
    assert plan.layers[0].events == (
        TravelTo(20, 20),
        SetModulation(128 * 100 / 255),
        MarkTo(30, 20),
        TravelTo(20, 30),
        SetModulation(128 * 100 / 255),
        MarkTo(30, 30),
    )


def test_cross_hatch_axes_become_ordered_ruida_layers(machine):
    metadata = _metadata(
        kind="raster",
        raster_axis="mixed",
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece-1",
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    ops.move_to(20, 20)
    ops.scan_to(30, 20, power_values=bytearray([128]))
    ops.move_to(30, 20)
    ops.scan_to(30, 30, power_values=bytearray([128]))
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.VARIABLE_POWER,
    )
    _process_end(ops, metadata)

    plan = RuidaOpsAdapter(machine).build_plan(ops)

    assert [layer.index for layer in plan.layers] == [0, 1]
    assert [layer.scan_axis for layer in plan.layers] == [
        "horizontal",
        "vertical",
    ]


def test_unbalanced_process_markers_are_rejected(machine):
    metadata = _metadata()
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20)
    ops.line_to(30, 20)

    with pytest.raises(RuidaEncodingError, match="no matching end"):
        RuidaOpsAdapter(machine).build_plan(ops)


def _select_research_profile(machine, profile, inactive_index=2):
    machine.driver_args["job_profile"] = profile
    _configure_inactive_power(machine, inactive_index, 40, 40)


def test_planned_path_preserves_ops_raster_section_boundaries(machine, doc):
    machine.driver_args["job_profile"] = "planned-path-research"
    metadata = _metadata(
        kind="raster",
        power=128 / 255,
        min_power=128 / 255,
        max_power=128 / 255,
        power_mode="static",
        raster_axis="mixed",
        raster_mode="CONSTANT_POWER",
        depth_mode="mask_scan",
        scan_angle=45,
    )
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece-1",
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    ops.move_to(20, 20)
    ops.scan_to(22, 22, power_values=bytearray([128, 128]))
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    ops.move_to(40, 40)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece-2",
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    ops.scan_to(42, 38, power_values=bytearray([128, 128]))
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    _process_end(ops, metadata)

    plan = RuidaOpsAdapter(
        machine,
        "planned-path-research",
    ).build_plan(ops)
    layer = plan.layers[0]

    assert layer.raster_processing == "planned-path"
    assert layer.events == ()
    assert layer.raster_sections == (
        RasterSection((TravelTo(20, 20), MarkTo(22, 22))),
        RasterSection((TravelTo(40, 40), MarkTo(42, 38))),
    )
    result = RuidaEncoder("planned-path-research").encode(
        ops,
        machine,
        doc,
    )
    assert result.driver_data["profile"].endswith("planned-path-research")
    assert result.warnings == (
        (
            "The selected Ruida planned-path research profile has limited "
            "hardware evidence from five-line, single-section diagonal "
            "coupons on a Boss LS2040 over USB serial: motion without visible "
            "marks at 10%, and visible marks at 15%, including one job "
            "generated end to end by Rayforge. All ran at 100 mm/s; other "
            "accepted combinations remain unvalidated"
        ),
        "Ruida TravelTo uses the controller-configured rapid rate",
    )


@pytest.mark.parametrize(
    ("cross_hatch", "expected_slope_signs"),
    ((False, 1), (True, 2)),
)
def test_engrave_pipeline_compiles_diagonal_planned_path_sections(
    engrave_step_class,
    test_machine_and_config,
    mocker,
    cross_hatch,
    expected_slope_signs,
):
    machine, context = test_machine_and_config
    machine.hydrate()
    machine.driver_name = RuidaSerialDriver.__name__
    machine.set_dialect_uid(None)
    _select_research_profile(machine, "planned-path-research")
    step = engrave_step_class.create(context, name="Planned Ruida engrave")
    step.depth_mode = "CONSTANT_POWER"
    step.scan_angle = 45
    step.cross_hatch = cross_hatch
    step.auto_levels = False
    step.sample_interval_mm = 0.5
    step.line_interval_mm = 0.5
    mocker.patch.object(
        WorkPiece,
        "render_to_pixels",
        autospec=True,
        side_effect=_opaque_black_surface,
    )
    workpiece = WorkPiece(name="diagonal image")
    workpiece.set_size(3, 2)
    workpiece.pos = (20, 20)
    doc = Doc()
    workflow = doc.active_layer.workflow
    assert workflow is not None
    workflow.add_child(step)
    doc.active_layer.add_child(workpiece)

    completed = {}
    execute_stages(
        IntentBuilder(machine=machine, generation_id=1).build(doc),
        lambda node: completed.__setitem__(node.key, node),
    )
    machine_node = completed[job_machinexform_key()]
    encode_node = completed[job_encode_key()]
    assert machine_node.error is None, machine_node.error
    assert encode_node.error is None, encode_node.error
    ops = machine_node.output.ops
    plan = RuidaOpsAdapter(
        machine,
        "planned-path-research",
    ).build_plan(ops)

    assert len(plan.layers) == 1
    layer = plan.layers[0]
    assert layer.raster_processing == "planned-path"
    source_section_count = sum(
        ops.command_type(index) == CommandType.OPS_SECTION_START
        for index in range(ops.len())
    )
    assert len(layer.raster_sections) == source_section_count
    assert source_section_count >= 1
    slope_signs = set()
    for section in layer.raster_sections:
        position = None
        for event in section.events:
            if isinstance(event, TravelTo):
                position = (event.x_mm, event.y_mm)
            elif isinstance(event, MarkTo) and position is not None:
                dx = event.x_mm - position[0]
                dy = event.y_mm - position[1]
                if abs(dx) > 1e-9 and abs(dy) > 1e-9:
                    slope_signs.add(1 if dx * dy > 0 else -1)
                position = (event.x_mm, event.y_mm)
    assert len(slope_signs) == expected_slope_signs

    payload = encode_node.output.payload
    assert payload
    codec = RuidaCodec()
    program = codec.decode(payload, container="rd")
    assert program.issues == []
    assert codec.encode(program, container="rd") == payload


def test_planned_path_requires_explicit_research_profile(machine):
    metadata = _metadata(
        kind="raster",
        power=128 / 255,
        min_power=128 / 255,
        max_power=128 / 255,
        power_mode="static",
        raster_axis="arbitrary",
        raster_mode="CONSTANT_POWER",
        depth_mode="mask_scan",
        scan_angle=45,
    )
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece",
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    ops.move_to(20, 20)
    ops.scan_to(22, 22, power_values=bytearray([128]))

    with pytest.raises(RuidaEncodingError, match="planned-path-research"):
        RuidaOpsAdapter(machine).build_plan(ops)


def test_diagonal_depth_map_remains_fail_closed(machine):
    machine.driver_args["job_profile"] = "planned-path-research"
    metadata = _metadata(
        kind="raster",
        power=0.5,
        power_mode="static",
        raster_axis="arbitrary",
        raster_mode="DEPTH_MAP",
        depth_mode="multi_pass",
        scan_angle=45,
    )
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece",
        raster_mode=RasterMode.DEPTH_MAP,
    )
    ops.move_to(20, 20)
    ops.scan_to(22, 22, power_values=bytearray([128]))

    with pytest.raises(RuidaEncodingError, match="depth raster"):
        RuidaOpsAdapter(
            machine,
            "planned-path-research",
        ).build_plan(ops)


def test_stationary_dwell_maps_milliseconds_and_compiles(machine, doc):
    _select_research_profile(machine, "stationary-research")
    metadata = _metadata()
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.move_to(20, 20)
    ops.line_to(25, 20)
    ops.dwell(100)
    ops.line_to(30, 20)
    _process_end(ops, metadata)

    plan = RuidaOpsAdapter(machine, "stationary-research").build_plan(ops)

    assert plan.layers[0].events == (
        TravelTo(20, 20),
        MarkTo(25, 20),
        Dwell(100),
        MarkTo(30, 20),
    )
    result = RuidaEncoder("stationary-research").encode(ops, machine, doc)
    assert result.warnings[0] == (
        "The selected Ruida research profile has offline fixture evidence "
        "only and no hardware execution validation"
    )
    records = _records(result.payload)
    assert _values(records, "additional_delay") == [{"time_ms": 100.0}]


def test_stationary_dwell_limit_fails_closed(machine):
    _select_research_profile(machine, "stationary-research")
    metadata = _metadata()
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.move_to(20, 20)
    ops.dwell(200.001)

    with pytest.raises(RuidaEncodingError, match="cannot exceed 200"):
        RuidaOpsAdapter(machine, "stationary-research").build_plan(ops)


def test_rf_frequency_state_metadata_and_layer_are_consistent(machine, doc):
    _select_research_profile(machine, "rf-research")
    metadata = _metadata(frequency=10_000)
    ops = _vector_ops_with_state_override(
        metadata,
        lambda value: value.set_frequency(10_000),
    )

    plan = RuidaOpsAdapter(machine, "rf-research").build_plan(ops)

    assert plan.layers[0].frequency_hz == 10_000
    records = _records(
        RuidaEncoder("rf-research").encode(ops, machine, doc).payload
    )
    assert _values(records, "layer_frequency") == [
        {"laser": 0, "layer": 0, "frequency_khz": 10.0},
        {"laser": 1, "layer": 0, "frequency_khz": 10.0},
    ]


def test_rf_zero_sentinel_omits_frequency(machine, doc):
    _select_research_profile(machine, "rf-research")
    plan = RuidaOpsAdapter(machine, "rf-research").build_plan(_vector_ops())

    assert plan.layers[0].frequency_hz is None
    records = _records(
        RuidaEncoder("rf-research")
        .encode(
            _vector_ops(),
            machine,
            doc,
        )
        .payload
    )
    assert _values(records, "layer_frequency") == []


def _vector_ops_with_state_override(metadata, override):
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    override(ops)
    ops.move_to(20, 20)
    ops.line_to(30, 20)
    _process_end(ops, metadata)
    return ops


def test_rf_mid_process_frequency_mismatch_is_rejected(machine):
    _select_research_profile(machine, "rf-research")
    metadata = _metadata(frequency=10_000)
    ops = _vector_ops_with_state_override(
        metadata,
        lambda value: value.set_frequency(20_000),
    )

    with pytest.raises(RuidaEncodingError, match="frequency disagree"):
        RuidaOpsAdapter(machine, "rf-research").build_plan(ops)


@pytest.mark.parametrize(("width_us", "width_ns"), ((0.1, 100), (0.2, 200)))
def test_fiber_pulse_width_converts_microseconds_exactly(
    machine,
    doc,
    width_us,
    width_ns,
):
    machine.heads[0].laser_type = LaserType.FIBER
    _select_research_profile(machine, "fiber-research")
    metadata = _metadata(pulse_width=width_us)
    ops = _vector_ops_with_state_override(
        metadata,
        lambda value: value.set_pulse_width(width_us),
    )

    plan = RuidaOpsAdapter(machine, "fiber-research").build_plan(ops)

    assert plan.layers[0].pulse_width_ns == width_ns
    records = _records(
        RuidaEncoder("fiber-research").encode(ops, machine, doc).payload
    )
    assert _values(records, "layer_fiber_pulse_width") == [
        {
            "selector_a": 0,
            "selector_b": 0,
            "pulse_width_ns": width_ns,
        }
    ]


def test_fiber_profile_zero_is_explicit_while_proven_omits(machine, doc):
    metadata = _metadata(pulse_width=None)
    proven = RuidaOpsAdapter(machine).build_plan(_vector_ops(metadata))
    assert proven.layers[0].pulse_width_ns is None
    assert (
        _values(
            _records(
                RuidaEncoder()
                .encode(_vector_ops(metadata), machine, doc)
                .payload
            ),
            "layer_fiber_pulse_width",
        )
        == []
    )

    machine.heads[0].laser_type = LaserType.FIBER
    _select_research_profile(machine, "fiber-research")
    fiber = RuidaOpsAdapter(
        machine,
        "fiber-research",
    ).build_plan(_vector_ops(metadata))
    assert fiber.layers[0].pulse_width_ns == 0
    records = _records(
        RuidaEncoder("fiber-research")
        .encode(_vector_ops(metadata), machine, doc)
        .payload
    )
    assert (
        _values(records, "layer_fiber_pulse_width")[0]["pulse_width_ns"] == 0
    )


def test_fiber_fractional_nanosecond_is_rejected(machine):
    machine.heads[0].laser_type = LaserType.FIBER
    _select_research_profile(machine, "fiber-research")
    metadata = _metadata(pulse_width=0.1005)

    with pytest.raises(RuidaEncodingError, match="convert exactly"):
        RuidaOpsAdapter(machine, "fiber-research").build_plan(
            _vector_ops(metadata)
        )


def test_fiber_profile_requires_fiber_head(machine):
    _select_research_profile(machine, "fiber-research")

    with pytest.raises(RuidaEncodingError, match="fiber laser head"):
        RuidaOpsAdapter(machine, "fiber-research").build_plan(_vector_ops())


def test_dual_profile_selects_only_head_two_with_explicit_inactive_power(
    machine,
    doc,
):
    second = _add_second_laser(machine)
    machine.driver_args["job_profile"] = "dual-laser-research"
    _configure_inactive_power(machine, 1, 12, 34)
    metadata = _metadata(
        head_uid=second.uid,
        tool_number=second.tool_number,
    )
    ops = _vector_ops(metadata)

    plan = RuidaOpsAdapter(machine, "dual-laser-research").build_plan(ops)
    channels = plan.layers[0].laser_channels

    assert channels == (
        LaserChannelPlan(1, False, 12, 34),
        LaserChannelPlan(2, True, 50, 50),
    )
    assert sum(channel.enabled for channel in channels) == 1
    result = RuidaEncoder("dual-laser-research").encode(
        ops,
        machine,
        doc,
    )
    assert result.driver_data["profile"].endswith("dual-laser-research")


def test_duplicate_ruida_laser_tool_numbers_fail_closed(machine):
    duplicate = Laser()
    duplicate.uid = "duplicate"
    duplicate.tool_number = 0
    machine.add_head(duplicate)

    with pytest.raises(RuidaEncodingError, match="must be unique"):
        RuidaOpsAdapter(machine)


def test_research_channel_values_require_explicit_confirmation(machine):
    machine.driver_args["job_profile"] = "rf-research"
    machine.driver_args.update(
        {
            "laser_2_inactive_min_power_percent": 0,
            "laser_2_inactive_max_power_percent": 0,
        }
    )
    metadata = _metadata(frequency=10_000)

    with pytest.raises(RuidaEncodingError, match="explicitly confirmed"):
        RuidaOpsAdapter(machine, "rf-research").build_plan(
            _vector_ops_with_state_override(
                metadata,
                lambda value: value.set_frequency(10_000),
            )
        )


def test_unset_inactive_power_defaults_cannot_confirm_zero(machine):
    values = {var.key: var.value for var in ruida_job_profile_vars()}
    values.update(
        {
            "job_profile": "rf-research",
            "laser_2_inactive_powers_confirmed": True,
        }
    )
    machine.driver_args.update(values)
    metadata = _metadata(frequency=10_000)

    assert machine.driver_args["laser_2_inactive_min_power_percent"] == -1
    assert machine.driver_args["laser_2_inactive_max_power_percent"] == -1
    with pytest.raises(RuidaEncodingError, match="configured explicitly"):
        RuidaOpsAdapter(machine, "rf-research").build_plan(
            _vector_ops_with_state_override(
                metadata,
                lambda value: value.set_frequency(10_000),
            )
        )


def test_confirmation_is_scoped_to_the_exact_inactive_channel(machine):
    machine.driver_args["job_profile"] = "dual-laser-research"
    second = _add_second_laser(machine)
    _configure_inactive_power(machine, 2, 40, 40)
    machine.driver_args.update(
        {
            "laser_1_inactive_min_power_percent": 0,
            "laser_1_inactive_max_power_percent": 0,
        }
    )
    metadata = _metadata(
        head_uid=second.uid,
        tool_number=second.tool_number,
    )

    with pytest.raises(
        RuidaEncodingError,
        match="Inactive laser 1 channel powers must be explicitly confirmed",
    ):
        RuidaOpsAdapter(machine, "dual-laser-research").build_plan(
            _vector_ops(metadata)
        )


def _dynamic_vector_ops(with_tabs):
    metadata = _metadata(
        power=0.8,
        min_power=0.2,
        max_power=0.8,
        power_mode="dynamic",
    )
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(SectionType.VECTOR_OUTLINE, "workpiece")
    ops.move_to(0, 0)
    ops.line_to(10, 0)
    ops.ops_section_end(SectionType.VECTOR_OUTLINE)
    _process_end(ops, metadata)
    if with_tabs:
        ops.apply_transformers([TabsSpec(0.25, 0.8, [(5.0, 0.0, 2.0)])])
    return ops


def test_dynamic_tab_power_emits_power_events_without_deduping(machine, doc):
    _select_research_profile(machine, "dynamic-power-research")
    ops = _dynamic_vector_ops(with_tabs=True)

    plan = RuidaOpsAdapter(
        machine,
        "dynamic-power-research",
    ).build_plan(ops)
    layer = plan.layers[0]
    effective = (
        LaserChannelPlan(1, True, 20, 20),
        LaserChannelPlan(2, False, 40, 40),
    )

    assert layer.laser_channels == (
        LaserChannelPlan(1, True, 20, 80),
        LaserChannelPlan(2, False, 40, 40),
    )
    assert layer.events == (
        TravelTo(0, 0),
        MarkTo(4, 0),
        MarkWithPower(6, 0, effective),
        MarkTo(10, 0),
    )
    result = RuidaEncoder("dynamic-power-research").encode(
        ops,
        machine,
        doc,
    )
    assert result.payload


def test_repeated_dynamic_marks_are_not_deduplicated(machine):
    _select_research_profile(machine, "dynamic-power-research")
    metadata = _metadata(
        power=0.8,
        min_power=0.2,
        max_power=0.8,
        power_mode="dynamic",
    )
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.move_to(0, 0)
    ops.set_power(0.2)
    ops.line_to(5, 0)
    ops.line_to(10, 0)
    ops.set_power(0.8)
    ops.line_to(15, 0)
    _process_end(ops, metadata)

    layer = (
        RuidaOpsAdapter(
            machine,
            "dynamic-power-research",
        )
        .build_plan(ops)
        .layers[0]
    )

    assert [type(event) for event in layer.events] == [
        TravelTo,
        MarkWithPower,
        MarkWithPower,
        MarkTo,
    ]


def test_dynamic_metadata_without_reduced_marks_uses_proven_static_plan(
    machine,
):
    ops = _dynamic_vector_ops(with_tabs=False)
    ops.apply_transformers([TabsSpec(0.25, 0.8, [])])
    layer = RuidaOpsAdapter(machine).build_plan(ops).layers[0]

    assert layer.min_power_percent == pytest.approx(80)
    assert layer.max_power_percent == pytest.approx(80)
    assert layer.laser_channels is None
    assert not any(isinstance(event, MarkWithPower) for event in layer.events)


def test_reduced_dynamic_marks_require_research_profile(machine):
    with pytest.raises(RuidaEncodingError, match="Reduced positive"):
        RuidaOpsAdapter(machine).build_plan(
            _dynamic_vector_ops(with_tabs=True)
        )


def test_typed_logical_z_offset_compiles_balanced_raster_envelope(
    machine, doc
):
    machine.driver_args["job_profile"] = "z-research"
    metadata = _metadata(
        kind="raster",
        power=0.5,
        power_mode="static",
        raster_mode="CONSTANT_POWER",
        depth_mode="mask_scan",
        z_offset=1.0,
    )
    ops = Ops()
    _process_start(ops, metadata)
    _state(ops, metadata)
    ops.ops_section_start(
        SectionType.RASTER_FILL,
        "workpiece",
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    ops.move_to(20, 20)
    ops.line_to(30, 20)
    ops.ops_section_end(
        SectionType.RASTER_FILL,
        raster_mode=RasterMode.CONSTANT_POWER,
    )
    _process_end(ops, metadata)

    plan = RuidaOpsAdapter(machine, "z-research").build_plan(ops)

    assert plan.layers[0].z_offset_mm == 1
    records = _records(
        RuidaEncoder("z-research").encode(ops, machine, doc).payload
    )
    assert _values(records, "z_offset_delta") == [
        {"delta_mm": -1.0},
        {"delta_mm": 1.0},
    ]


def test_z_profile_does_not_reinterpret_endpoint_z(machine):
    machine.driver_args["job_profile"] = "z-research"
    metadata = _metadata(
        kind="raster",
        power=0.5,
        power_mode="static",
        raster_mode="CONSTANT_POWER",
        depth_mode="mask_scan",
        z_offset=1.0,
    )
    ops = Ops()
    _process_start(ops, metadata)
    ops.move_to(20, 20, 1)

    with pytest.raises(RuidaEncodingError, match="planar Z=0"):
        RuidaOpsAdapter(machine, "z-research").build_plan(ops)


def test_ruida_encode_token_tracks_profile_channels_and_head_type(machine):
    machine.driver_name = RuidaSerialDriver.__name__
    builder = IntentBuilder(machine=machine)
    before = builder._encode_token(Doc(), {}, machine_transform_token=1)

    _select_research_profile(machine, "rf-research")
    after_profile = builder._encode_token(
        Doc(),
        {},
        machine_transform_token=1,
    )
    machine.heads[0].laser_type = LaserType.FIBER
    after_head_type = builder._encode_token(
        Doc(),
        {},
        machine_transform_token=1,
    )
    machine.driver_args["laser_2_inactive_max_power_percent"] = 41
    after_channel = builder._encode_token(
        Doc(),
        {},
        machine_transform_token=1,
    )
    machine.driver_args["laser_2_inactive_powers_confirmed"] = False
    after_confirmation = builder._encode_token(
        Doc(),
        {},
        machine_transform_token=1,
    )

    assert (
        len(
            {
                before,
                after_profile,
                after_head_type,
                after_channel,
                after_confirmation,
            }
        )
        == 5
    )


def test_encoder_uses_one_immutable_ruida_config_snapshot(machine, doc):
    _select_research_profile(machine, "rf-research")
    encoder = RuidaEncoder.from_machine(machine)
    metadata = _metadata(frequency=10_000)
    ops = _vector_ops_with_state_override(
        metadata,
        lambda value: value.set_frequency(10_000),
    )

    machine.driver_args["job_profile"] = "proven"
    machine.driver_args["laser_2_inactive_powers_confirmed"] = False
    machine.heads[0].tool_number = 1
    result = encoder.encode(ops, machine, doc)

    assert result.driver_data["profile"].endswith("rf-research")


def test_manual_encoder_does_not_reuse_snapshot_across_machines(machine, doc):
    encoder = RuidaEncoder()
    assert encoder.encode(_vector_ops(), machine, doc).payload

    machine.heads[0].uid = "replacement-laser"
    metadata = _metadata(head_uid="replacement-laser")

    assert encoder.encode(_vector_ops(metadata), machine, doc).payload


def test_intent_encoder_context_pairs_snapshot_and_token(
    machine,
    mocker,
):
    machine.driver_name = RuidaSerialDriver.__name__
    machine.set_dialect_uid(None)
    _select_research_profile(machine, "rf-research")
    original = RuidaSerialDriver.create_encoder_context
    captured = {}

    def capture_then_mutate(machine_arg):
        encoder, payload = original(machine_arg)
        captured["payload"] = payload
        machine_arg.driver_args["job_profile"] = "proven"
        return encoder, payload

    context = mocker.patch.object(
        RuidaSerialDriver,
        "create_encoder_context",
        side_effect=capture_then_mutate,
    )
    doc = Doc()
    builder = IntentBuilder(machine=machine)
    nodes = []

    builder._build_encoder_node(doc, nodes, {}, 1)

    context.assert_called_once_with(machine)
    node = next(item for item in nodes if item.key == job_encode_key())
    assert node.version_token == builder._encode_token(
        doc,
        {},
        1,
        captured["payload"],
    )
