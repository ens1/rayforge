"""Offline checks for Rayforge-owned Ruida hardware evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image
from raygeo.pipeline.execute import execute_stages
from ruida_re import KnownCommand, RuidaCodec

from rayforge.core.doc import Doc
from rayforge.core.vectorization_spec import TraceSpec
from rayforge.core.workpiece import WorkPiece
from rayforge.image.png.importer import PngImporter
from rayforge.machine.driver.ruida.ruida_serial_driver import (
    RuidaSerialDriver,
)
from rayforge.machine.models.machine import Origin
from rayforge.pipeline.intent_builder import IntentBuilder, job_encode_key

FIXTURE = (
    Path(__file__).parent
    / "fixtures/hardware/boss-ls2040-usb-serial-rayforge-planned-path-v1"
)
MANIFEST_PATH = FIXTURE / "manifest-v1.json"
ARTIFACT_PATH = FIXTURE / "boss-ls2040-top-right-diagonal-45-15pct-v2.rd"
SOURCE_PATH = FIXTURE / "opaque-12x18-source.png"
ARTIFACT_SHA256 = (
    "52327453d276567086eb441a61efae887c13e13ef344c6cb55a06cd6fad2c8aa"
)
SOURCE_SHA256 = (
    "1edc07dc386bc3a3cd109fb1154b3bc202cbdd4254d256c3f9116c379ed4eb96"
)
DYNAMIC_FIXTURE = (
    Path(__file__).parent
    / "fixtures/hardware/boss-ls2040-usb-serial-rayforge-dynamic-vector-v1"
)
DYNAMIC_MANIFEST_PATH = DYNAMIC_FIXTURE / "manifest-v1.json"
DYNAMIC_ARTIFACTS = {
    "dynamic-vector-15-10-15-v1": (
        "boss-ls2040-dynamic-vector-tab-15-10pct-v1.rd",
        "ec6a24b47bac882e62fa3ac996727e3b452b81b7717e3137a38809501d851809",
    ),
    "dynamic-vector-15-5-15-v2": (
        "boss-ls2040-dynamic-15-5-15-v2.rd",
        "723f5f8de65db05717ac95d9c7d11774dab9af558338c4b8daa486afb95f129b",
    ),
}
POWER_COMMANDS = {
    "layer_laser_1_min_power",
    "layer_laser_1_max_power",
    "layer_laser_2_min_power",
    "layer_laser_2_max_power",
    "laser_1_min_power",
    "laser_1_max_power",
    "laser_2_min_power",
    "laser_2_max_power",
}


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _program() -> tuple[Any, tuple[KnownCommand, ...]]:
    program = RuidaCodec(context="job").decode(
        ARTIFACT_PATH.read_bytes(),
        container="rd",
    )
    known = tuple(
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    )
    return program, known


def _dynamic_manifest() -> dict[str, Any]:
    return json.loads(DYNAMIC_MANIFEST_PATH.read_text(encoding="utf-8"))


def _dynamic_job(identifier: str) -> dict[str, Any]:
    matches = [
        job
        for job in _dynamic_manifest()["jobs"]
        if job["identifier"] == identifier
    ]
    assert len(matches) == 1
    return matches[0]


def _dynamic_program(
    identifier: str,
) -> tuple[bytes, Any, tuple[KnownCommand, ...]]:
    filename, _ = DYNAMIC_ARTIFACTS[identifier]
    payload = (DYNAMIC_FIXTURE / filename).read_bytes()
    program = RuidaCodec(context="job").decode(payload, container="rd")
    known = tuple(
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    )
    return payload, program, known


def _one(
    records: tuple[KnownCommand, ...],
    name: str,
) -> KnownCommand:
    matches = [record for record in records if record.name == name]
    assert len(matches) == 1
    return matches[0]


def _decoded_motion(
    records: tuple[KnownCommand, ...],
) -> list[dict[str, float | str]]:
    x = None
    y = None
    motion: list[dict[str, float | str]] = []
    for record in records:
        if record.name in {"move_absolute", "cut_absolute"}:
            x = record.values["x_mm"]
            y = record.values["y_mm"]
        elif record.name in {"move_relative", "cut_relative"}:
            assert x is not None
            assert y is not None
            x = round(x + record.values["dx_mm"], 3)
            y = round(y + record.values["dy_mm"], 3)
        else:
            continue
        event_type = (
            "mark_to" if record.name.startswith("cut") else "travel_to"
        )
        motion.append({"type": event_type, "x_mm": x, "y_mm": y})
    return motion


def test_hardware_evidence_is_content_addressed_and_sanitized() -> None:
    manifest = _manifest()
    artifact = ARTIFACT_PATH.read_bytes()
    source = SOURCE_PATH.read_bytes()

    assert (
        manifest["schema"]
        == "rayforge.hardware-ruida-planned-path-validation.v1"
    )
    assert manifest["identifier"].endswith("rayforge-planned-path-v1")
    assert manifest["generating_revisions"] == {
        "rayforge": "22c7a54cdd441e7917206418fd843bc3a23ee476",
        "raygeo": "d14784963ea6abd0c91c18bf1cc6a362d7057519",
        "ruida_re": "7ef0ff5011bd0684a2a70cb72c43e666f9438651",
    }
    assert len(artifact) == manifest["artifact"]["size_bytes"] == 538
    assert hashlib.sha256(artifact).hexdigest() == ARTIFACT_SHA256
    assert manifest["artifact"]["sha256"] == ARTIFACT_SHA256
    assert len(source) == manifest["source"]["size_bytes"] == 108
    assert hashlib.sha256(source).hexdigest() == SOURCE_SHA256
    assert manifest["source"]["sha256"] == SOURCE_SHA256
    reference = manifest["operator_observation"]["direct_coupon_reference"]
    assert reference == {
        "ruida_re_evidence_revision": (
            "b1b9e23e8bfdc05d76be20d51c75bb85c134243f"
        ),
        "ten_percent_artifact_sha256": (
            "1c28e301321fd54bf8e6bc54e7c9b370305fb601c0d1cea5ef4d9976124d953d"
        ),
        "ten_percent_observation": "five movements; no visible marks",
        "fifteen_percent_artifact_sha256": (
            "92c328e7fc9d98ba38da1e8179560d2f2675ac79983b4efeef11889bb8bb1123"
        ),
        "fifteen_percent_observation": (
            "five visible lines; no connecting marks; not over-burnt"
        ),
    }

    serialized = json.dumps(manifest).lower()
    for private_path in (
        "/dev/",
        "/tmp/",
        "/private/",
        "/users/",
        "cu.usb",
        "tty.usb",
        "usbserial-",
        "usbmodem",
    ):
        assert private_path not in serialized


def test_hardware_artifact_decodes_and_roundtrips_exactly() -> None:
    manifest = _manifest()
    artifact = ARTIFACT_PATH.read_bytes()
    program, records = _program()
    codec = RuidaCodec(context="job")

    assert program.issues == []
    assert len(program.records) == 77
    assert len(records) == 77
    assert manifest["artifact"]["decode"] == {
        "records": 77,
        "known_records": 77,
        "opaque_records": 0,
        "issues": [],
        "exact_preserve_roundtrip": True,
        "exact_recompute_roundtrip": True,
    }
    assert (
        codec.encode(
            program,
            container="rd",
            checksum_policy="preserve",
        )
        == artifact
    )
    assert (
        codec.encode(
            program,
            container="rd",
            checksum_policy="recompute",
        )
        == artifact
    )

    checksum = _one(records, "file_checksum").values["value"]
    assert checksum == 25253
    assert checksum == program.source_checksum_basis
    assert checksum == manifest["artifact"]["checksum"]["value"]


def test_current_pipeline_preserves_the_observed_process_scope(
    engrave_step_class,
    test_machine_and_config,
) -> None:
    manifest = _manifest()
    source_config = manifest["source"]
    generation = manifest["generation"]
    machine_model = generation["machine_model"]
    process = generation["process"]
    machine, config = test_machine_and_config
    machine.hydrate()
    machine.name = "Boss LS-2040 Offline Model"
    machine.set_axis_extents(*machine_model["axis_extents_mm"])
    machine.set_origin(Origin(machine_model["origin"]))
    machine.set_reverse_x_axis(machine_model["reverse_x_axis"])
    machine.set_reverse_y_axis(machine_model["reverse_y_axis"])
    machine.set_rotary_enabled_default(machine_model["rotary_enabled"])
    machine.set_active_wcs(machine_model["wcs"])
    machine.update_wcs_offset(
        machine_model["wcs"],
        tuple(machine_model["wcs_offset_mm"]),
    )
    machine.auto_connect = generation["auto_connect"]
    machine.driver_name = RuidaSerialDriver.__name__
    machine.set_dialect_uid(None)
    machine.driver_args.update(
        {
            "job_profile": process["profile"],
            "laser_2_inactive_min_power_percent": 40,
            "laser_2_inactive_max_power_percent": 40,
            "laser_2_inactive_powers_confirmed": True,
        }
    )

    with Image.open(SOURCE_PATH) as image:
        assert list(image.size) == source_config["pixels"]
        assert image.mode == source_config["mode"]
    import_result = PngImporter(
        SOURCE_PATH.read_bytes(),
        SOURCE_PATH,
    ).get_doc_items(vectorization_spec=TraceSpec())
    assert import_result is not None
    payload = import_result.payload
    assert payload is not None
    source = payload.source
    workpiece = cast(WorkPiece, payload.items[0])
    workpiece.set_size(*source_config["world_size_mm"])
    workpiece.pos = machine.get_coordinate_space().machine_item_to_world(
        tuple(source_config["machine_item_position_mm"]),
        workpiece.size,
    )

    step = engrave_step_class.create(
        config,
        name="Boss LS2040 diagonal validation",
    )
    step.set_power(process["requested_power_percent"] / 100)
    step.set_cut_speed(process["speed_mm_s"] * 60)
    step.set_air_assist(process["air_assist_requested"])
    step.depth_mode = process["depth_mode"]
    step.scan_angle = process["source_scan_angle_degrees"]
    step.cross_hatch = process["cross_hatch"]
    step.auto_levels = False
    step.sample_interval_mm = process["sample_interval_mm"]
    step.line_interval_mm = process["line_interval_mm"]
    for transformer in step.per_workpiece_transformers_dicts:
        if transformer.get("name") == "OverscanTransformer":
            transformer["enabled"] = process["overscan_enabled"]

    doc = Doc()
    doc.add_asset(source)
    workflow = doc.active_layer.workflow
    assert workflow is not None
    workflow.add_child(step)
    doc.active_layer.add_child(workpiece)

    completed = {}
    execute_stages(
        IntentBuilder(machine=machine, generation_id=1).build(doc),
        lambda node: completed.__setitem__(node.key, node),
    )
    encode_node = completed[job_encode_key()]
    assert encode_node.error is None, encode_node.error
    regenerated = encode_node.output.payload

    assert generation["hardware_io"] is False
    assert process["rayforge_laser_channel_intent"] == 1

    codec = RuidaCodec(context="job")
    program = codec.decode(regenerated, container="rd")
    records = tuple(
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    )
    assert program.issues == []
    assert len(records) == len(program.records)
    names = [record.name for record in records]
    assert (
        codec.encode(program, container="rd", checksum_policy="preserve")
        == regenerated
    )
    assert (
        codec.encode(program, container="rd", checksum_policy="recompute")
        == regenerated
    )

    motion = _decoded_motion(records)
    assert [event["type"] for event in motion] == [
        "travel_to",
        "mark_to",
    ] * 5
    assert sum(name.startswith("move_") for name in names) == 5
    assert sum(name.startswith("cut_") for name in names) == 5
    for start, end in zip(motion[::2], motion[1::2], strict=True):
        dx = float(end["x_mm"]) - float(start["x_mm"])
        dy = float(end["y_mm"]) - float(start["y_mm"])
        assert dx * dy < 0
        assert abs(abs(dx) - abs(dy)) <= 0.002

    bounds = (
        min(float(event["x_mm"]) for event in motion),
        min(float(event["y_mm"]) for event in motion),
        max(float(event["x_mm"]) for event in motion),
        max(float(event["y_mm"]) for event in motion),
    )
    assert 40 <= bounds[0] <= bounds[2] <= 54
    assert 20 <= bounds[1] <= bounds[3] <= 40
    assert _one(records, "layer_speed").values["speed_mm_s"] == 100.0
    assert _one(records, "active_speed").values["speed_mm_s"] == 100.0
    assert [
        record.values["operation"]
        for record in records
        if record.name == "layer_control"
    ] == [0, 48, 16, 18]
    assert set(names).isdisjoint(
        {
            "additional_delay",
            "layer_fiber_pulse_width",
            "layer_frequency",
            "z_offset_delta",
        }
    )
    for record in records:
        if record.name in POWER_COMMANDS:
            assert record.values["power_percent"] == pytest.approx(
                process["encoded_power_percent"]
            )


def test_hardware_artifact_has_exact_bounded_motion() -> None:
    manifest = _manifest()
    _, records = _program()
    names = [record.name for record in records]
    motion = _decoded_motion(records)

    assert motion == manifest["artifact"]["decoded_motion"]
    assert [event["type"] for event in motion] == [
        "travel_to",
        "mark_to",
    ] * 5
    assert names.count("move_absolute") == 1
    assert names.count("move_relative") == 4
    assert names.count("cut_absolute") == 3
    assert names.count("cut_relative") == 2

    marks = zip(motion[::2], motion[1::2], strict=True)
    for start, end in marks:
        dx = float(end["x_mm"]) - float(start["x_mm"])
        dy = float(end["y_mm"]) - float(start["y_mm"])
        assert dx * dy < 0

    bounds = {
        "min_x": min(float(event["x_mm"]) for event in motion),
        "min_y": min(float(event["y_mm"]) for event in motion),
        "max_x": max(float(event["x_mm"]) for event in motion),
        "max_y": max(float(event["y_mm"]) for event in motion),
    }
    assert bounds == pytest.approx(
        {
            "min_x": 40.927,
            "min_y": 21.112,
            "max_x": 53.187,
            "max_y": 39.196,
        }
    )
    material = manifest["environment"]["material"]
    short_side = min(material["dimensions_mm"])
    assert 0 <= bounds["min_x"] <= bounds["max_x"] <= short_side
    assert 0 <= bounds["min_y"] <= bounds["max_y"] <= short_side
    assert material["axis_orientation"] == "not-recorded"

    advertised = manifest["artifact"]["advertised_bounds_mm"]
    for prefix in ("job", "document"):
        assert _one(records, f"{prefix}_min_point").values == {
            "x_mm": advertised["min_x"],
            "y_mm": advertised["min_y"],
        }
        assert _one(records, f"{prefix}_max_point").values == {
            "x_mm": advertised["max_x"],
            "y_mm": advertised["max_y"],
        }


def test_hardware_artifact_stays_inside_the_observed_process_scope() -> None:
    manifest = _manifest()
    _, records = _program()
    names = {record.name for record in records}
    process = manifest["generation"]["process"]
    plan = manifest["generation"]["adapted_plan"]

    assert {
        key: plan[key]
        for key in (
            "layers",
            "raster_sections",
            "travel_events",
            "mark_events",
        )
    } == {
        "layers": 1,
        "raster_sections": 1,
        "travel_events": 5,
        "mark_events": 5,
    }
    assert plan["controller_bounds_mm"] == pytest.approx(
        {
            "min_x": 40.92705016129855,
            "min_y": 21.11161726804346,
            "max_x": 53.1874578013842,
            "max_y": 39.19603644514689,
        }
    )
    assert _one(records, "layer_count").values == {"count_minus_one": 0}
    assert _one(records, "select_layer").values == {"layer": 0}
    layer_controls = [
        record.values["operation"]
        for record in records
        if record.name == "layer_control"
    ]
    assert layer_controls == [0, 48, 16, 18]
    assert 5 not in layer_controls

    assert _one(records, "layer_speed").values["speed_mm_s"] == 100.0
    assert _one(records, "active_speed").values["speed_mm_s"] == 100.0
    for record in records:
        if record.name in POWER_COMMANDS:
            assert record.values["power_percent"] == pytest.approx(
                process["encoded_power_percent"]
            )
    assert {record.name for record in records} >= POWER_COMMANDS
    assert _one(records, "enable_laser_tube_start").values == {"enabled": 1}

    unsupported = {
        "additional_delay",
        "layer_fiber_pulse_width",
        "layer_frequency",
        "z_offset_delta",
    }
    assert names.isdisjoint(unsupported)
    assert process["cross_hatch"] is False
    assert process["overscan_enabled"] is False
    assert manifest["result"]["mode_wide_execution_evidence"] == (
        "not-observed"
    )
    assert manifest["result"]["default_profile_promotion"] == "withheld"


def test_hardware_receipt_and_observation_have_bounded_meaning() -> None:
    manifest = _manifest()
    host_log = manifest["transmission"]["host_log"]
    observation = manifest["operator_observation"]

    assert host_log == {
        "scope": "host-side driver transfer summary",
        "packets": 1,
        "retries": 0,
        "controller_acknowledgement": False,
        "execution_acknowledgement": False,
    }
    assert "packet_bytes" not in host_log
    assert "transmissions" not in host_log
    assert manifest["transmission"]["artifact_sha256"] == ARTIFACT_SHA256
    assert observation["reported_verbatim"] == (
        "5 lines, just like before. They're more spaced out and going the "
        "opposite direction"
    )
    assert observation["status"] == "operator-reported-five-lines"
    assert manifest["result"]["scoped_execution_evidence"] == (
        "operator-observed"
    )


def test_dynamic_hardware_evidence_is_content_addressed_and_sanitized() -> (
    None
):
    manifest = _dynamic_manifest()

    assert manifest["schema"] == (
        "rayforge.hardware-ruida-dynamic-vector-observation.v1"
    )
    assert manifest["generating_revisions"] == {
        "rayforge": "b288e1960419ce1b14642ab4ead8ac8f6a08b92d",
        "raygeo": "5663bec8c5d47ebb7f3f09d6df0658f5bdac8583",
        "ruida_re": "7ef0ff5011bd0684a2a70cb72c43e666f9438651",
    }
    assert manifest["scope"]["recipe_content_addressed"] is False
    assert len(manifest["jobs"]) == len(DYNAMIC_ARTIFACTS) == 2

    for identifier, (filename, expected_sha256) in DYNAMIC_ARTIFACTS.items():
        artifact = (DYNAMIC_FIXTURE / filename).read_bytes()
        job = _dynamic_job(identifier)
        assert job["artifact"]["file"] == filename
        assert len(artifact) == job["artifact"]["size_bytes"] == 539
        assert hashlib.sha256(artifact).hexdigest() == expected_sha256
        assert job["artifact"]["sha256"] == expected_sha256

    serialized = json.dumps(manifest).lower()
    for private_path in (
        "/dev/",
        "/tmp/",
        "/private/",
        "/users/",
        "cu.usb",
        "tty.usb",
        "usbserial",
        "usbmodem",
    ):
        assert private_path not in serialized


@pytest.mark.parametrize("identifier", tuple(DYNAMIC_ARTIFACTS))
def test_dynamic_hardware_artifacts_decode_and_roundtrip_exactly(
    identifier: str,
) -> None:
    payload, program, records = _dynamic_program(identifier)
    artifact = _dynamic_job(identifier)["artifact"]
    codec = RuidaCodec(context="job")

    assert program.issues == artifact["issues"] == []
    assert len(program.records) == artifact["records"] == 79
    assert len(records) == artifact["known_records"] == 79
    assert artifact["opaque_records"] == 0
    assert (
        codec.encode(
            program,
            container="rd",
            checksum_policy="preserve",
        )
        == payload
    )
    assert (
        codec.encode(
            program,
            container="rd",
            checksum_policy="recompute",
        )
        == payload
    )
    checksum = _one(records, "file_checksum").values["value"]
    assert checksum == program.source_checksum_basis
    assert checksum == artifact["checksum"]


@pytest.mark.parametrize(
    ("identifier", "expected_motion", "span_lengths"),
    (
        (
            "dynamic-vector-15-10-15-v1",
            [(100.0, 56.0), (88.0, 56.0), (82.0, 56.0), (70.0, 56.0)],
            [12.0, 6.0, 12.0],
        ),
        (
            "dynamic-vector-15-5-15-v2",
            [(30.0, 75.0), (60.0, 75.0), (90.0, 75.0), (120.0, 75.0)],
            [30.0, 30.0, 30.0],
        ),
    ),
)
def test_dynamic_hardware_artifacts_capture_the_missing_restore(
    identifier: str,
    expected_motion: list[tuple[float, float]],
    span_lengths: list[float],
) -> None:
    _, _, records = _dynamic_program(identifier)
    job = _dynamic_job(identifier)
    names = [record.name for record in records]
    motion = _decoded_motion(records)

    assert [
        (float(event["x_mm"]), float(event["y_mm"])) for event in motion
    ] == expected_motion
    assert [
        abs(expected_motion[index + 1][0] - expected_motion[index][0])
        for index in range(3)
    ] == span_lengths
    assert [
        record.values["operation"]
        for record in records
        if record.name == "layer_control"
    ] == [0, 48, 16, 18, 5]

    middle_cut_index = max(
        index
        for index, record in enumerate(records)
        if record.name == "cut_absolute"
        and record.values["x_mm"] == expected_motion[2][0]
    )
    assert names[middle_cut_index - 7 : middle_cut_index] == [
        "layer_control",
        "select_layer",
        "laser_1_min_power",
        "laser_1_max_power",
        "laser_2_min_power",
        "laser_2_max_power",
        "external_io",
    ]
    assert records[middle_cut_index - 7].values == {"operation": 5}
    assert names[middle_cut_index + 1] == "cut_absolute"
    assert job["dynamic_envelope"]["explicit_baseline_restore_records"] == 0

    process = job["process"]
    layer_powers = {
        record.name: record.values["power_percent"]
        for record in records
        if record.name.startswith("layer_laser_")
    }
    assert layer_powers == pytest.approx(
        {
            "layer_laser_1_min_power": process["layer_laser_1_power_percent"][
                "minimum"
            ],
            "layer_laser_1_max_power": process["layer_laser_1_power_percent"][
                "maximum"
            ],
            "layer_laser_2_min_power": process[
                "inactive_laser_2_power_percent"
            ]["minimum"],
            "layer_laser_2_max_power": process[
                "inactive_laser_2_power_percent"
            ]["maximum"],
        }
    )
    dynamic_powers = [
        record.values["power_percent"]
        for record in records[middle_cut_index - 5 : middle_cut_index - 1]
    ]
    assert dynamic_powers == pytest.approx(
        [
            process["reduced_laser_1_power_percent"]["minimum"],
            process["reduced_laser_1_power_percent"]["maximum"],
            process["inactive_laser_2_power_percent"]["minimum"],
            process["inactive_laser_2_power_percent"]["maximum"],
        ]
    )
    assert set(names).isdisjoint(
        {
            "additional_delay",
            "layer_fiber_pulse_width",
            "layer_frequency",
            "laser_interval",
            "z_offset_delta",
        }
    )


def test_dynamic_hardware_receipts_and_observations_are_bounded() -> None:
    first = _dynamic_job("dynamic-vector-15-10-15-v1")
    second = _dynamic_job("dynamic-vector-15-5-15-v2")

    for job in (first, second):
        assert job["transmission"]["host_log"] == {
            "scope": "host-side driver transfer summary",
            "packets": 1,
            "retries": 0,
            "controller_acknowledgement": False,
            "execution_acknowledgement": False,
        }
        assert job["transmission"]["explicit_operator_approval"] is True

    assert first["operator_observation"]["reported_verbatim"] == [
        "It looks pretty solid. Maybe go longer and vary more"
    ]
    assert first["result"]["dynamic_power_effect_evidence"] == ("inconclusive")
    assert second["operator_observation"]["reported_verbatim"] == [
        "Motion was good, first 30mm was good, no second 30mm",
        "only the first 30mm",
    ]
    assert second["result"]["automatic_baseline_restore_evidence"] == (
        "contradicted"
    )
    assert second["result"]["power_state_persistence_evidence"] == (
        "operator-observation-consistent"
    )
    conclusion = _dynamic_manifest()["conclusion"]
    assert conclusion["required_encoder_change"] == (
        "Emit an explicit layer-baseline power envelope before a normal "
        "baseline mark that follows a reduced-power mark."
    )
