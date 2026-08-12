"""Verify scoped Boss LS2040 variable-raster evidence."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

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
    / "boss-ls2040-usb-serial-rayforge-variable-raster-v1"
)
MANIFEST = EVIDENCE / "manifest-v1.json"
ARTIFACT = EVIDENCE / "boss-ls2040-variable-raster-5-15pct-v1.rd"
EXPECTED_SHA256 = (
    "1699e0173a30bcbf25e07ff15a3a27534cfbda239c91e67b1ec1559ef9c56c2a"
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


def _motion(
    records: tuple[KnownCommand, ...],
) -> tuple[tuple[str, float, float], ...]:
    x: float | None = None
    y: float | None = None
    motion = []
    for record in records:
        if record.name == "move_absolute":
            x = record.values["x_mm"]
            y = record.values["y_mm"]
        elif record.name == "cut_horizontal":
            assert x is not None
            assert y is not None
            x = round(x + record.values["dx_mm"], 3)
        else:
            continue
        motion.append((record.name, x, y))
    return tuple(motion)


def test_artifact_is_content_addressed_and_roundtrips_exactly() -> None:
    raw, program, records = _program()
    artifact = _manifest()["artifact"]
    codec = RuidaCodec(context="job")

    assert len(raw) == artifact["size_bytes"] == 555
    assert sha256(raw).hexdigest() == artifact["sha256"] == EXPECTED_SHA256
    assert artifact["file"] == ARTIFACT.name
    assert program.issues == artifact["issues"] == []
    assert len(program.records) == artifact["records"] == 92
    assert len(records) == artifact["known_records"] == 92
    assert artifact["opaque_records"] == 0
    assert program.source_checksum_basis == artifact["checksum"] == 29720
    assert {path.name for path in EVIDENCE.iterdir()} == {
        "README.md",
        "manifest-v1.json",
        ARTIFACT.name,
    }
    assert codec.encode(program, container="rd") == raw
    assert (
        codec.encode(
            program,
            container="rd",
            checksum_policy="recompute",
        )
        == raw
    )


def test_marks_use_only_bounded_native_horizontal_chunks() -> None:
    _, _, records = _program()
    wire = _manifest()["process"]["wire_motion"]
    names = [record.name for record in records]
    opcodes = [record.opcode for record in records]
    cuts = [record for record in records if record.name.startswith("cut_")]
    signed_chunks = [record.values["dx_mm"] for record in cuts]

    assert [record.name for record in cuts] == ["cut_horizontal"] * 15
    assert [record.opcode for record in cuts] == ["aa"] * 15
    assert signed_chunks == wire["signed_chunk_lengths_mm"]
    assert max(abs(length) for length in signed_chunks) == 4.0
    assert wire["maximum_absolute_chunk_length_mm"] == 4.0
    assert names.count("move_absolute") == wire["move_absolute_records"] == 2
    assert set(wire["forbidden_records_absent"]).isdisjoint(names)
    assert set(wire["forbidden_opcodes_absent"]).isdisjoint(opcodes)


def test_normalized_modulation_and_planned_geometry_are_exact() -> None:
    _, _, records = _program()
    process = _manifest()["process"]
    motion = _motion(records)
    modulation_1 = [
        value["power_percent"]
        for value in _record_values(records, "immediate_power_1")
    ]
    modulation_3 = [
        value["power_percent"]
        for value in _record_values(records, "immediate_power_3")
    ]

    assert modulation_1 == modulation_3
    assert modulation_1 == process["normalized_modulation_percent"]
    for index, record in enumerate(records):
        if record.name != "immediate_power_1":
            continue
        paired = records[index + 1]
        assert paired.name == "immediate_power_3"
        assert paired.values == record.values

    minimum = process["decoded_layer_power_percent"]["minimum"]
    maximum = process["decoded_layer_power_percent"]["maximum"]
    effective = [
        minimum + value / 100 * (maximum - minimum) for value in modulation_1
    ]
    assert effective == process["modeled_effective_output_percent"]
    assert (
        max(effective) == process["maximum_modeled_effective_output_percent"]
    )

    assert motion[0] == ("move_absolute", 80.0, 24.052)
    assert motion[6] == ("cut_horizontal", 102.0, 24.052)
    assert motion[7] == ("cut_horizontal", 103.0, 24.052)
    assert motion[8] == ("move_absolute", 114.0, 24.052)
    assert motion[9] == ("cut_horizontal", 115.0, 24.052)
    assert motion[-1] == ("cut_horizontal", 140.0, 24.052)
    assert process["logical_plan"] == [
        {
            "type": "mark_group",
            "distance_mm": 23.0,
            "modulated_mark_segments_mm": [22.0, 1.0],
        },
        {"type": "travel_to", "distance_mm": 11.0},
        {
            "type": "mark_group",
            "distance_mm": 26.0,
            "modulated_mark_segments_mm": [1.0, 25.0],
        },
    ]
    assert process["controller_bounds_mm"] == {
        "min_x": 80.0,
        "min_y": 24.052,
        "max_x": 140.0,
        "max_y": 24.052,
    }


def test_layer_envelope_observation_and_claims_remain_scoped() -> None:
    _, _, records = _program()
    manifest = _manifest()
    process = manifest["process"]
    host_log = manifest["transmission"]["host_log"]
    result = manifest["result"]
    prior = manifest["relationships"]["prior_negative_evidence"]

    assert process["power_mode"] == "variable"
    assert process["speed_mm_s"] == 100.0
    assert process["requested_layer_power_percent"] == {
        "minimum": 5.0,
        "maximum": 15.0,
    }
    assert _record_values(records, "layer_speed") == [
        {"layer": 0, "speed_mm_s": 100.0}
    ]
    assert _record_values(records, "active_speed") == [{"speed_mm_s": 100.0}]
    assert _record_values(records, "layer_mode_or_attributes") == [
        {"layer": 0, "value": 2}
    ]
    assert _record_values(records, "layer_control") == [
        {"operation": 1},
        {"operation": 48},
        {"operation": 16},
        {"operation": 18},
    ]
    assert _record_values(records, "enable_laser_tube_start") == [
        {"enabled": 1}
    ]
    minimum = process["decoded_layer_power_percent"]["minimum"]
    maximum = process["decoded_layer_power_percent"]["maximum"]
    for name in (
        "layer_laser_1_min_power",
        "layer_laser_2_min_power",
        "laser_1_min_power",
        "laser_2_min_power",
    ):
        assert [
            value["power_percent"] for value in _record_values(records, name)
        ] == [minimum]
    for name in (
        "layer_laser_1_max_power",
        "layer_laser_2_max_power",
        "laser_1_max_power",
        "laser_2_max_power",
    ):
        assert [
            value["power_percent"] for value in _record_values(records, name)
        ] == [maximum]

    assert manifest["operator_observation"]["reported_verbatim"] == (
        "Everything is as expected"
    )
    assert manifest["operator_observation"]["instrumented_metrology"] is False
    assert host_log == {
        "scope": "host-side driver transfer summary",
        "packets": 1,
        "payload_bytes": 555,
        "retries": 0,
        "controller_acknowledgement": False,
        "execution_acknowledgement": False,
    }
    assert result["status"] == "scoped-variable-native-raster-pass"
    assert result["dimensional_metrology"] == "not-performed"
    assert result["power_metrology"] == "not-performed"
    assert result["gap_zero_optical_output"] == "not-established"
    assert result["broader_scan_modes"] == "not-tested"
    assert result["profile_promotion"] == "none"
    assert result["broad_profile_conclusion"] == "not-established"
    assert prior["disposition"] == "remains-quarantined-do-not-resend"
    assert prior["superseded"] is False
    assert prior["causal_attribution"] == "not-established"

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
    prior_dir = EVIDENCE.parent / prior["identifier"]
    assert not list(prior_dir.glob("*.rd"))
    assert len(list(prior_dir.glob("*.rd.quarantined"))) == 1
