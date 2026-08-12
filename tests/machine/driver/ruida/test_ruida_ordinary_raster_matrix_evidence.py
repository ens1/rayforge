"""Verify scoped Boss LS2040 ordinary raster-matrix evidence."""

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
    / "boss-ls2040-usb-serial-rayforge-ordinary-raster-matrix-v1"
)
MANIFEST = EVIDENCE / "manifest-v1.json"
ARTIFACT = EVIDENCE / (
    "boss-ls2040-proven-ordinary-raster-matrix-20pct-100mms-"
    "x76p2-y76p2-offline-v3.rd"
)
EXPECTED_SHA256 = (
    "bca7ea59721450e38bfef513ec95ca3a6db5d3fec1987664858848523349c6c4"
)


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _records() -> tuple[bytes, Any, tuple[KnownCommand, ...]]:
    raw = ARTIFACT.read_bytes()
    program = RuidaCodec(context="job").decode(raw, container="rd")
    records = tuple(
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    )
    return raw, program, records


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
    raw, program, records = _records()
    artifact = _manifest()["artifact"]
    codec = RuidaCodec(context="job")

    assert len(raw) == artifact["size_bytes"] == 746
    assert sha256(raw).hexdigest() == artifact["sha256"] == EXPECTED_SHA256
    assert artifact["file"] == ARTIFACT.name
    assert program.issues == artifact["issues"] == []
    assert len(program.records) == artifact["records"] == 131
    assert len(records) == artifact["known_records"] == 131
    assert artifact["opaque_records"] == 0
    assert program.source_checksum_basis == artifact["checksum"] == 40137
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


def test_native_cardinal_layers_and_bounds_are_exact() -> None:
    _, _, records = _records()
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

    assert [record.name for record in horizontal_cuts] == [
        "cut_horizontal"
    ] * 12
    assert [record.opcode for record in horizontal_cuts] == ["aa"] * 12
    assert [record.values["dx_mm"] for record in horizontal_cuts] == wire[
        "horizontal_signed_chunk_lengths_mm"
    ]
    assert [record.name for record in vertical_cuts] == ["cut_vertical"] * 15
    assert [record.opcode for record in vertical_cuts] == ["ab"] * 15
    assert [record.values["dy_mm"] for record in vertical_cuts] == wire[
        "vertical_signed_chunk_lengths_mm"
    ]
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

    names = [record.name for record in records]
    opcodes = [record.opcode for record in records]
    assert set(wire["forbidden_records_absent"]).isdisjoint(names)
    assert set(wire["forbidden_opcodes_absent"]).isdisjoint(opcodes)
    assert (
        _motion_bounds(horizontal)
        == process["layers"][0]["controller_bounds_mm"]
    )
    assert (
        _motion_bounds(vertical)
        == process["layers"][1]["controller_bounds_mm"]
    )
    assert process["controller_bounds_mm"] == {
        "min_x": 76.2,
        "min_y": 76.2,
        "max_x": 88.2,
        "max_y": 106.148,
    }


def test_process_and_observation_claims_remain_scoped() -> None:
    manifest = _manifest()
    process = manifest["process"]
    result = manifest["result"]

    assert process["profile"] == "proven"
    assert process["power_mode"] == "constant"
    assert process["speed_mm_s"] == 100.0
    assert process["requested_power_percent"] == 20.0
    assert [layer["scan_axis"] for layer in process["layers"]] == [
        "horizontal",
        "vertical",
    ]
    assert [layer["raster_strategy"] for layer in process["layers"]] == [
        "bidirectional",
        "bidirectional",
    ]
    assert manifest["placement"]["controller_offset_from_origin_mm"] == {
        "x": 76.2,
        "y": 76.2,
    }
    assert manifest["operator_observation"]["reported_verbatim"] == (
        "Yes, that is what I see"
    )
    assert manifest["operator_observation"]["instrumented_metrology"] is False
    assert manifest["transmission"]["host_log"] == {
        "scope": "host-side driver transfer summary",
        "packets": 1,
        "payload_bytes": 746,
        "retries": 0,
        "controller_acknowledgement": False,
        "execution_acknowledgement": False,
    }
    assert result["status"] == (
        "scoped-ordinary-cardinal-bidirectional-raster-matrix-pass"
    )
    assert result["dimensional_metrology"] == "not-performed"
    assert result["power_metrology"] == "not-performed"
    assert result["gap_zero_optical_output"] == "not-established"
    assert result["unidirectional_raster"] == "not-tested"
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
