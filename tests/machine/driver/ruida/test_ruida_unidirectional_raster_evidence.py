"""Verify scoped Boss LS2040 unidirectional-raster evidence."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from ruida_re import KnownCommand, RuidaCodec

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = (
    ROOT
    / "tests"
    / "machine"
    / "driver"
    / "ruida"
    / "fixtures"
    / "hardware"
    / "boss-ls2040-usb-serial-rayforge-unidirectional-raster-v1"
)
MANIFEST = EVIDENCE / "manifest-v1.json"
ARTIFACT = EVIDENCE / (
    "boss-ls2040-proven-unidirectional-raster-matrix-20pct-100mms-"
    "x8-y90-offline-v1.rd"
)
EXPECTED_SHA256 = (
    "255edaf9f35658a53d7a18988e9429ca0a1dfb733a6d7e4909b3edd7215cf1b0"
)


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _program() -> tuple[bytes, Any, tuple[KnownCommand, ...]]:
    raw = ARTIFACT.read_bytes()
    program = RuidaCodec(context="job").decode(raw, container="rd")
    records = tuple(
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    )
    return raw, program, records


def _record_values(
    records: tuple[KnownCommand, ...],
    name: str,
) -> list[dict[str, Any]]:
    return [record.values for record in records if record.name == name]


def _layer_motion(
    records: tuple[KnownCommand, ...],
) -> dict[int, tuple[KnownCommand, ...]]:
    current_layer: int | None = None
    grouped: dict[int, list[KnownCommand]] = {}
    for record in records:
        if record.name == "select_layer":
            current_layer = record.values["layer"]
        elif record.name.startswith(("move_", "cut_")):
            assert current_layer is not None
            grouped.setdefault(current_layer, []).append(record)
    return {key: tuple(value) for key, value in grouped.items()}


def _motion_bounds(
    records: tuple[KnownCommand, ...],
) -> dict[str, float]:
    x: float | None = None
    y: float | None = None
    points: list[tuple[float, float]] = []
    for record in records:
        if record.name == "move_absolute":
            x = record.values["x_mm"]
            y = record.values["y_mm"]
        elif record.name in {"move_horizontal", "cut_horizontal"}:
            assert x is not None and y is not None
            x = round(x + record.values["dx_mm"], 3)
        elif record.name in {"move_vertical", "cut_vertical"}:
            assert x is not None and y is not None
            y = round(y + record.values["dy_mm"], 3)
        else:
            continue
        points.append((cast(float, x), cast(float, y)))
    return {
        "min_x": min(x_value for x_value, _ in points),
        "min_y": min(y_value for _, y_value in points),
        "max_x": max(x_value for x_value, _ in points),
        "max_y": max(y_value for _, y_value in points),
    }


def test_artifact_identity_and_exact_roundtrip() -> None:
    raw, program, records = _program()
    artifact = _manifest()["artifact"]
    codec = RuidaCodec(context="job")

    assert len(raw) == artifact["size_bytes"] == 769
    assert sha256(raw).hexdigest() == artifact["sha256"] == EXPECTED_SHA256
    assert artifact["file"] == ARTIFACT.name
    assert program.issues == artifact["issues"] == []
    assert len(program.records) == artifact["records"] == 128
    assert len(records) == artifact["known_records"] == 128
    assert artifact["opaque_records"] == 0
    assert program.source_checksum_basis == artifact["checksum"] == 41166
    assert {path.name for path in EVIDENCE.iterdir()} == {
        "README.md",
        "manifest-v1.json",
        ARTIFACT.name,
    }
    assert codec.encode(program, container="rd") == raw
    assert (
        codec.encode(program, container="rd", checksum_policy="recompute")
        == raw
    )


def test_native_unidirectional_modes_and_motion_are_exact() -> None:
    _, _, records = _program()
    process = _manifest()["process"]
    wire = process["wire_motion"]
    grouped = _layer_motion(records)
    horizontal = grouped[0]
    vertical = grouped[1]
    horizontal_cuts = [
        record for record in horizontal if record.name.startswith("cut_")
    ]
    vertical_cuts = [
        record for record in vertical if record.name.startswith("cut_")
    ]

    assert _record_values(records, "layer_mode_or_attributes") == [
        {"layer": 0, "value": 1},
        {"layer": 1, "value": 3},
    ]
    assert _record_values(records, "layer_control") == [
        {"operation": 2},
        {"operation": 48},
        {"operation": 16},
        {"operation": 18},
        {"operation": 4},
        {"operation": 48},
        {"operation": 16},
        {"operation": 18},
    ]
    assert [record.name for record in horizontal_cuts] == [
        "cut_horizontal"
    ] * 12
    assert [record.opcode for record in horizontal_cuts] == ["aa"] * 12
    assert [record.values["dx_mm"] for record in horizontal_cuts] == wire[
        "horizontal_signed_chunk_lengths_mm"
    ]
    assert {record.values["dx_mm"] > 0 for record in horizontal_cuts} == {
        False
    }
    assert [record.name for record in vertical_cuts] == ["cut_vertical"] * 12
    assert [record.opcode for record in vertical_cuts] == ["ab"] * 12
    assert [record.values["dy_mm"] for record in vertical_cuts] == wire[
        "vertical_signed_chunk_lengths_mm"
    ]
    assert {record.values["dy_mm"] > 0 for record in vertical_cuts} == {True}
    assert (
        max(
            abs(
                cast(
                    float,
                    record.values.get("dx_mm", record.values.get("dy_mm")),
                )
            )
            for record in (*horizontal_cuts, *vertical_cuts)
        )
        == wire["maximum_absolute_mark_chunk_length_mm"]
        == 4.0
    )

    assert [
        record.values
        for record in horizontal
        if record.name == "move_absolute"
    ] == [
        {"x_mm": 24.0, "y_mm": 93.8},
        {"x_mm": 24.0, "y_mm": 95.8},
        {"x_mm": 24.0, "y_mm": 97.8},
    ]
    assert [
        record.values["dx_mm"]
        for record in horizontal
        if record.name == "move_horizontal"
    ] == [-5.0, -5.0, -5.0]
    assert [
        record.values for record in vertical if record.name == "move_absolute"
    ] == [
        {"x_mm": 41.108, "y_mm": 90.0},
        {"x_mm": 43.108, "y_mm": 90.0},
        {"x_mm": 45.108, "y_mm": 90.0},
    ]
    assert [
        record.values["dy_mm"]
        for record in vertical
        if record.name == "move_vertical"
    ] == [5.1, 5.1, 5.1]
    assert (
        _motion_bounds(horizontal)
        == process["layers"][0]["controller_bounds_mm"]
    )
    assert (
        _motion_bounds(vertical)
        == process["layers"][1]["controller_bounds_mm"]
    )


def test_process_is_constant_power_and_excludes_research_commands() -> None:
    _, _, records = _program()
    manifest = _manifest()
    process = manifest["process"]
    wire = process["wire_motion"]
    names = [record.name for record in records]
    opcodes = [record.opcode for record in records]

    assert process["profile"] == "proven"
    assert process["raster_processing"] == "native"
    assert process["power_mode"] == "constant"
    assert process["speed_mm_s"] == 100.0
    assert process["requested_power_percent"] == 20.0
    assert process["air_assist_requested"] is False
    assert [layer["raster_strategy"] for layer in process["layers"]] == [
        "unidirectional",
        "unidirectional",
    ]
    assert _record_values(records, "layer_speed") == [
        {"layer": 0, "speed_mm_s": 100.0},
        {"layer": 1, "speed_mm_s": 100.0},
    ]
    assert _record_values(records, "active_speed") == [
        {"speed_mm_s": 100.0},
        {"speed_mm_s": 100.0},
    ]
    assert _record_values(records, "enable_laser_tube_start") == [
        {"enabled": 1},
        {"enabled": 1},
    ]
    encoded_power = process["encoded_power_percent"]
    for name in (
        "layer_laser_1_min_power",
        "layer_laser_1_max_power",
        "laser_1_min_power",
        "laser_1_max_power",
    ):
        assert [
            value["power_percent"] for value in _record_values(records, name)
        ] == [encoded_power, encoded_power]
    assert set(wire["forbidden_records_absent"]).isdisjoint(names)
    assert set(wire["forbidden_opcodes_absent"]).isdisjoint(opcodes)
    assert manifest["generation"]["serialized_scan_strategy_by_layer"] == [
        "unidirectional",
        "unidirectional",
    ]
    assert manifest["generation"]["hardware_io_during_generation"] is False
    assert manifest["generation"]["transport_during_generation"] is False


def test_observation_and_claims_remain_scoped() -> None:
    manifest = _manifest()
    observation = manifest["operator_observation"]
    result = manifest["result"]

    assert observation["reported_verbatim"] == (
        "I see 12 lines, 2x3 vertical and 2x3 horizontal, no burnt "
        "return moves, Z remained. All looks as expected"
    )
    assert observation["interpreted_visible_pattern"] == {
        "horizontal": "three rows with two visible marks each",
        "vertical": "three columns with two visible marks each",
        "total_visible_marks": 12,
    }
    assert observation["controller_z_reported_unchanged"] is True
    assert observation["instrumented_metrology"] is False
    assert manifest["transmission"]["host_log"] == {
        "scope": "host-side stock-driver transfer summary",
        "packets": 1,
        "payload_bytes": 769,
        "retries": 0,
        "controller_acknowledgement": False,
        "execution_acknowledgement": False,
    }
    assert result["status"] == (
        "scoped-production-path-cardinal-unidirectional-raster-pass"
    )
    assert result["return_travel_burns"] == "not-operator-observed"
    assert result["controller_z_readout"] == "operator-reported-unchanged"
    assert result["serialized_z_motion"] == "absent"
    assert result["dimensional_metrology"] == "not-performed"
    assert result["power_metrology"] == "not-performed"
    assert result["directional_motion_metrology"] == "not-performed"
    assert result["return_zero_optical_output"] == "not-established"
    assert result["profile_promotion"] == "none"
    assert result["broad_profile_conclusion"] == "not-established"

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
