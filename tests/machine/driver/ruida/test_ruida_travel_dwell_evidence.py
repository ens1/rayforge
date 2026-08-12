"""Verify scoped Boss LS2040 travel-then-dwell evidence."""

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
    / "boss-ls2040-usb-serial-rayforge-travel-dwell-v1"
)
MANIFEST = EVIDENCE / "manifest-v1.json"


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _program(
    artifact: dict[str, Any],
) -> tuple[bytes, Any, tuple[KnownCommand, ...]]:
    raw = (EVIDENCE / artifact["file"]).read_bytes()
    program = RuidaCodec(context="job").decode(raw, container="rd")
    records = tuple(
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    )
    return raw, program, records


def _core(
    records: tuple[KnownCommand, ...],
) -> tuple[tuple[str, dict[str, Any]], ...]:
    ignored = {"additional_delay", "file_checksum"}
    return tuple(
        (record.name, record.values)
        for record in records
        if record.name not in ignored
    )


def test_artifacts_are_content_addressed_and_roundtrip_exactly() -> None:
    manifest = _manifest()
    codec = RuidaCodec(context="job")
    expected_files = {"README.md", "manifest-v1.json"}

    for artifact in manifest["artifacts"].values():
        raw, program, records = _program(artifact)
        expected_files.add(artifact["file"])
        assert sha256(raw).hexdigest() == artifact["sha256"]
        assert len(raw) == artifact["size_bytes"]
        assert program.issues == artifact["issues"] == []
        assert len(program.records) == artifact["records"]
        assert len(records) == artifact["known_records"]
        assert artifact["opaque_records"] == 0
        assert program.source_checksum_basis == artifact["checksum"]
        assert codec.encode(program, container="rd") == raw
        assert (
            codec.encode(
                program,
                container="rd",
                checksum_policy="recompute",
            )
            == raw
        )

    assert {path.name for path in EVIDENCE.iterdir()} == expected_files


def test_stages_differ_only_by_delays_and_checksums() -> None:
    artifacts = _manifest()["artifacts"]
    record_sets = {
        name: _program(artifact)[2] for name, artifact in artifacts.items()
    }
    control_core = _core(record_sets["control"])

    assert _core(record_sets["sentinel"]) == control_core
    assert _core(record_sets["full"]) == control_core
    for name, records in record_sets.items():
        delays = [
            record for record in records if record.name == "additional_delay"
        ]
        assert len(delays) == artifacts[name]["delay_count"]


def test_delays_follow_travel_and_no_mark_follows_anchor() -> None:
    manifest = _manifest()
    process = manifest["process"]
    contract = process["wire_contract"]
    expected_motion = [
        ("move_absolute", {"x_mm": 110.0, "y_mm": 132.0}),
        ("cut_absolute", {"x_mm": 115.0, "y_mm": 132.0}),
        ("move_absolute", {"x_mm": 125.0, "y_mm": 132.0}),
        ("move_absolute", {"x_mm": 145.0, "y_mm": 132.0}),
        ("move_absolute", {"x_mm": 145.0, "y_mm": 147.0}),
        ("move_absolute", {"x_mm": 125.0, "y_mm": 147.0}),
    ]

    for artifact in manifest["artifacts"].values():
        _, _, records = _program(artifact)
        motion = [
            (record.name, record.values)
            for record in records
            if record.name in {"move_absolute", "cut_absolute"}
        ]
        cuts = [record for record in records if record.name.startswith("cut_")]
        assert motion == expected_motion
        assert len(cuts) == 1
        assert cuts[0].name == contract["anchor_record"]
        assert cuts[0].opcode == contract["anchor_opcode"]
        assert not [
            record for record in records if record.name == "laser_interval"
        ]
        for index, record in enumerate(records):
            if record.name != "additional_delay":
                continue
            assert records[index - 1].name == contract["travel_record"]
            assert records[index - 1].opcode == contract["travel_opcode"]
            assert record.opcode == contract["delay_opcode"]
            assert record.raw == contract["delay_logical_bytes"]
            assert record.values == {
                "time_ms": contract["delay_time_ms"],
            }


def test_transport_observations_and_conclusions_remain_scoped() -> None:
    manifest = _manifest()
    artifacts = manifest["artifacts"]
    transmissions = manifest["transmissions"]
    observations = manifest["operator_observations"]
    result = manifest["result"]

    assert observations["control"] == {
        "reported_verbatim": ("I see one faint line, vertical, about 5mm"),
        "instrumented_metrology": False,
    }
    assert observations["sentinel"] == {
        "reported_verbatim": (
            "It looks like it did a rectangle with pauses at the corner? "
            "Nothing other than a horizontal line, about 5mm"
        ),
        "instrumented_metrology": False,
    }
    assert observations["full"] == {
        "reported_verbatim": ("Yes, one faint line, pauses at the corners"),
        "instrumented_metrology": False,
    }
    for name, transmission in transmissions.items():
        assert transmission["explicit_operator_approval"] is True
        assert transmission["artifact_sha256"] == artifacts[name]["sha256"]
        assert transmission["host_log"] == {
            "scope": "host-side driver transfer summary",
            "packets": 1,
            "payload_bytes": artifacts[name]["size_bytes"],
            "retries": 0,
            "controller_acknowledgement": False,
            "execution_acknowledgement": False,
        }
    assert result == {
        "status": "scoped-travel-then-c611-pass",
        "encoded_subset": (
            "one and four exact 100 ms C611 delays immediately after TravelTo"
        ),
        "pause_behavior": "operator-observed",
        "post_anchor_visible_marking": "operator-reported-absent",
        "timing_metrology": "not-performed",
        "dimensional_metrology": "not-performed",
        "mark_adjacent_dwell": "not-tested",
        "dwell_200_ms": "not-tested",
        "stationary_marking_pulse": "not-tested",
        "broad_profile_conclusion": "not-established",
    }
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
