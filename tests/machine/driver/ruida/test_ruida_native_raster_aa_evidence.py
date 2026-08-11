"""Verify scoped Boss LS2040 native-raster AA evidence."""

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
    / "boss-ls2040-usb-serial-rayforge-native-raster-aa-v1"
)
MANIFEST = EVIDENCE / "manifest-v1.json"
ARTIFACT = EVIDENCE / ("boss-ls2040-native-raster-aa-20pct-y47-offline-v1.rd")
EXPECTED_SHA256 = (
    "d90990c0acfb4fc07fdee57ea50cbf6d7b3201a5ae8bae201ac716371eb49024"
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

    assert len(raw) == artifact["size_bytes"] == 525
    assert sha256(raw).hexdigest() == artifact["sha256"] == EXPECTED_SHA256
    assert artifact["file"] == ARTIFACT.name
    assert program.issues == artifact["issues"] == []
    assert len(program.records) == artifact["records"] == 82
    assert len(records) == artifact["known_records"] == 82
    assert artifact["opaque_records"] == 0
    assert program.source_checksum_basis == artifact["checksum"] == 28182
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
    manifest = _manifest()
    wire = manifest["process"]["wire_motion"]
    names = [record.name for record in records]
    opcodes = [record.opcode for record in records]
    cuts = [record for record in records if record.name.startswith("cut_")]
    signed_chunks = [record.values["dx_mm"] for record in cuts]

    assert [record.name for record in cuts] == ["cut_horizontal"] * 13
    assert [record.opcode for record in cuts] == ["aa"] * 13
    assert signed_chunks == wire["signed_chunk_lengths_mm"]
    assert max(abs(length) for length in signed_chunks) == 4.0
    assert wire["maximum_absolute_chunk_length_mm"] == 4.0
    assert set(wire["forbidden_records_absent"]).isdisjoint(names)
    assert set(wire["forbidden_opcodes_absent"]).isdisjoint(opcodes)


def test_motion_has_two_planned_marks_and_a_semantic_travel_gap() -> None:
    _, _, records = _program()
    process = _manifest()["process"]
    motion = _motion(records)

    assert motion[0] == ("move_absolute", 120.0, 48.052)
    assert motion[7] == ("cut_horizontal", 95.0, 48.052)
    assert motion[8] == ("move_absolute", 84.0, 48.052)
    assert motion[-1] == ("cut_horizontal", 60.0, 48.052)
    assert process["logical_plan"] == [
        {"type": "mark_to", "distance_mm": 25.0},
        {"type": "travel_to", "distance_mm": 11.0},
        {"type": "mark_to", "distance_mm": 24.0},
    ]
    assert process["controller_bounds_mm"] == {
        "min_x": 60.0,
        "min_y": 48.052,
        "max_x": 120.0,
        "max_y": 48.052,
    }
    assert process["power_mode"] == "constant"
    assert process["speed_mm_s"] == 100.0
    assert process["requested_power_percent"] == 20.0
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
    encoded_power = process["encoded_power_percent"]
    power_names = (
        "layer_laser_1_min_power",
        "layer_laser_1_max_power",
        "layer_laser_2_min_power",
        "layer_laser_2_max_power",
        "laser_1_min_power",
        "laser_1_max_power",
        "laser_2_min_power",
        "laser_2_max_power",
    )
    for name in power_names:
        values = _record_values(records, name)
        assert [value["power_percent"] for value in values] == [encoded_power]
    assert _record_values(records, "external_io") == []


def test_observation_and_transport_claims_remain_scoped() -> None:
    manifest = _manifest()
    host_log = manifest["transmission"]["host_log"]
    result = manifest["result"]
    relationship = manifest["relationship_to_prior_negative_evidence"]

    assert manifest["operator_observation"] == {
        "reported_verbatim": "Everything looks right to me",
        "instrumented_metrology": False,
    }
    assert host_log == {
        "scope": "host-side driver transfer summary",
        "packets": 1,
        "payload_bytes": 525,
        "retries": 0,
        "controller_acknowledgement": False,
        "execution_acknowledgement": False,
    }
    assert result["status"] == "scoped-native-raster-aa-pass"
    assert result["dimensional_metrology"] == "not-performed"
    assert result["variable_power_raster"] == "not-tested"
    assert result["broad_profile_conclusion"] == "not-established"
    assert relationship["disposition"] == ("remains-quarantined-do-not-resend")
    assert relationship["superseded"] is False
    assert relationship["causal_attribution"] == "not-established"
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
    prior = EVIDENCE.parent / relationship["identifier"]
    assert not list(prior.glob("*.rd"))
    assert len(list(prior.glob("*.rd.quarantined"))) == 1
