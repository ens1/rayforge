"""Offline checks for the supervised Ruida batch evidence."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest
from ruida_re import KnownCommand, RuidaCodec

RAYFORGE_ROOT = Path(__file__).resolve().parents[4]
HARDWARE_FIXTURES = Path(__file__).parent / "fixtures/hardware"
PLANNED_FIXTURE = (
    HARDWARE_FIXTURES / "boss-ls2040-usb-serial-rayforge-planned-path-v1"
)
DYNAMIC_FIXTURE = (
    HARDWARE_FIXTURES / "boss-ls2040-usb-serial-rayforge-dynamic-vector-v1"
)
ZERO_FIXTURE = (
    HARDWARE_FIXTURES / "boss-ls2040-usb-serial-zero-power-safety-v1"
)
CROSS_MANIFEST_PATH = PLANNED_FIXTURE / ("cross-hatch-observation-v1.json")
DYNAMIC_MANIFEST_PATH = DYNAMIC_FIXTURE / (
    "dynamic-repeated-observation-v4.json"
)
ZERO_MANIFEST_PATH = ZERO_FIXTURE / "manifest-v1.json"
EXPECTED_HOST_LOG = {
    "scope": "host-side driver transfer summary",
    "packets": 1,
    "retries": 0,
    "controller_acknowledgement": False,
    "execution_acknowledgement": False,
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_cases() -> tuple[tuple[Path, dict[str, Any]], ...]:
    cross = _read_json(CROSS_MANIFEST_PATH)
    dynamic = _read_json(DYNAMIC_MANIFEST_PATH)
    zero = _read_json(ZERO_MANIFEST_PATH)
    return (
        (PLANNED_FIXTURE / cross["artifact"]["file"], cross["artifact"]),
        (
            DYNAMIC_FIXTURE / dynamic["artifact"]["file"],
            dynamic["artifact"],
        ),
        (
            ZERO_FIXTURE / zero["artifacts"]["control"]["file"],
            zero["artifacts"]["control"],
        ),
        (
            ZERO_FIXTURE / zero["artifacts"]["dwell"]["file"],
            zero["artifacts"]["dwell"],
        ),
    )


def _decode(
    path: Path,
) -> tuple[bytes, Any, tuple[KnownCommand, ...]]:
    payload = path.read_bytes()
    program = RuidaCodec(context="job").decode(payload, container="rd")
    records = tuple(
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    )
    return payload, program, records


def _motion(
    records: tuple[KnownCommand, ...],
) -> list[tuple[str, float, float, KnownCommand]]:
    x = None
    y = None
    result: list[tuple[str, float, float, KnownCommand]] = []
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
        result.append((event_type, x, y, record))
    return result


def _issues(metadata: dict[str, Any]) -> list[Any]:
    decode = metadata.get("decode")
    if decode is not None:
        return decode["issues"]
    return metadata["issues"]


def _known_count(metadata: dict[str, Any]) -> int:
    decode = metadata.get("decode")
    if decode is not None:
        return decode["known_records"]
    return metadata["known_records"]


def test_batch_manifests_are_content_addressed_and_sanitized() -> None:
    manifests = (
        _read_json(CROSS_MANIFEST_PATH),
        _read_json(DYNAMIC_MANIFEST_PATH),
        _read_json(ZERO_MANIFEST_PATH),
    )
    assert [manifest["schema"] for manifest in manifests] == [
        "rayforge.hardware-ruida-cross-hatch-observation.v1",
        "rayforge.hardware-ruida-dynamic-repeated-observation.v1",
        "rayforge.hardware-ruida-zero-power-negative-evidence.v1",
    ]

    for path, metadata in _artifact_cases():
        payload = path.read_bytes()
        assert len(payload) == metadata["size_bytes"]
        assert hashlib.sha256(payload).hexdigest() == metadata["sha256"]

    serialized = json.dumps(manifests).lower()
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


def test_quarantined_artifacts_are_not_send_job_files() -> None:
    manifest = _read_json(ZERO_MANIFEST_PATH)
    published_files = {item["file"] for item in manifest["artifacts"].values()}
    original_files = {
        item["original_capture_filename"]
        for item in manifest["artifacts"].values()
    }

    assert all(name.endswith(".rd.quarantined") for name in published_files)
    assert all(name.endswith(".rd") for name in original_files)
    assert not any(ZERO_FIXTURE.glob("*.rd"))
    assert {
        path.name for path in ZERO_FIXTURE.iterdir() if path.is_file()
    } == {"manifest-v1.json", *published_files}
    manifest_rules = (RAYFORGE_ROOT / "MANIFEST.in").read_text(
        encoding="utf-8"
    )
    assert "*.rd.quarantined" in manifest_rules
    assert "test_ruida_batch_hardware_evidence.py" in manifest_rules


@pytest.mark.parametrize(
    ("path", "metadata"),
    _artifact_cases(),
    ids=lambda value: value.name if isinstance(value, Path) else None,
)
def test_batch_artifacts_decode_and_roundtrip_exactly(
    path: Path,
    metadata: dict[str, Any],
) -> None:
    payload, program, records = _decode(path)
    codec = RuidaCodec(context="job")

    assert program.issues == _issues(metadata) == []
    assert len(program.records) == _known_count(metadata)
    assert len(records) == _known_count(metadata)
    assert len(records) == len(program.records)
    checksum = next(
        record.values["value"]
        for record in records
        if record.name == "file_checksum"
    )
    assert checksum == metadata["checksum"]
    assert checksum == program.source_checksum_basis
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


def test_cross_hatch_has_two_dark_separated_diagonal_sections() -> None:
    manifest = _read_json(CROSS_MANIFEST_PATH)
    artifact = manifest["artifact"]
    path = PLANNED_FIXTURE / artifact["file"]
    _, _, records = _decode(path)
    motion = _motion(records)
    names = [record.name for record in records]

    assert [event[0] for event in motion] == [
        "travel_to",
        "mark_to",
    ] * 10
    slopes = []
    for start, end in zip(motion[::2], motion[1::2], strict=True):
        dx = end[1] - start[1]
        dy = end[2] - start[2]
        slopes.append(-1 if dx * dy < 0 else 1)
    assert slopes == [-1] * 5 + [1] * 5
    assert [
        record.values["operation"]
        for record in records
        if record.name == "layer_control" and record.values["operation"] == 5
    ] == [5]
    assert names.count("laser_interval") == 0
    assert names.count("additional_delay") == 0

    final_record = motion[-1][3]
    edge = artifact["edge_clipped_mark"]
    assert final_record.name == edge["command"] == "cut_relative"
    assert final_record.values == {
        "dx_mm": edge["delta_mm"]["x"],
        "dy_mm": edge["delta_mm"]["y"],
    }
    assert math.hypot(
        final_record.values["dx_mm"],
        final_record.values["dy_mm"],
    ) == pytest.approx(edge["decoded_length_mm"])
    assert edge["c610_laser_interval_records"] == 0

    assert manifest["transmission"]["host_log"] == EXPECTED_HOST_LOG
    assert manifest["operator_observation"]["reported_verbatim"] == (
        "Crosshatch is good. Both directions are visible, no connection "
        "burns, and no burns. I can see the one small edge, the beam "
        "obviously pulsed at the top left of the crosshatch"
    )
    assert manifest["result"]["status"] == "scoped-pass"
    assert manifest["result"]["c610_pulse_execution_evidence"] == (
        "not-observed"
    )


def test_repeated_dynamic_artifact_has_four_exact_envelopes() -> None:
    manifest = _read_json(DYNAMIC_MANIFEST_PATH)
    artifact = manifest["artifact"]
    path = DYNAMIC_FIXTURE / artifact["file"]
    _, _, records = _decode(path)
    names = [record.name for record in records]
    motion = _motion(records)

    assert [event[0] for event in motion] == ["travel_to"] + ["mark_to"] * 5
    assert [(event[1], event[2]) for event in motion] == [
        (55.0, 112.0),
        (71.0, 112.0),
        (87.0, 112.0),
        (103.0, 112.0),
        (119.0, 112.0),
        (135.0, 112.0),
    ]
    assert [motion[index + 1][1] - motion[index][1] for index in range(5)] == [
        16.0,
        16.0,
        16.0,
        16.0,
        16.0,
    ]

    envelope_indices = [
        index
        for index, record in enumerate(records)
        if record.name == "layer_control" and record.values == {"operation": 5}
    ]
    cut_indices = [
        index
        for index, record in enumerate(records)
        if record.name == "cut_absolute"
    ]
    assert len(envelope_indices) == 4
    assert [index + 7 for index in envelope_indices] == cut_indices[1:]
    envelope_names = [
        "layer_control",
        "select_layer",
        "laser_1_min_power",
        "laser_1_max_power",
        "laser_2_min_power",
        "laser_2_max_power",
        "external_io",
    ]
    maximums = []
    for index in envelope_indices:
        envelope = records[index : index + 7]
        assert [record.name for record in envelope] == envelope_names
        maximums.append(envelope[3].values["power_percent"])
    assert maximums == pytest.approx(
        [
            4.999084416773485,
            14.997253250320455,
            4.999084416773485,
            14.997253250320455,
        ]
    )
    assert [item["role"] for item in artifact["dynamic_envelopes"]] == [
        "reduce",
        "restore",
        "reduce",
        "restore",
    ]
    assert set(names).isdisjoint(
        {
            "additional_delay",
            "layer_fiber_pulse_width",
            "layer_frequency",
            "laser_interval",
            "z_offset_delta",
        }
    )

    assert manifest["transmission"]["host_log"] == EXPECTED_HOST_LOG
    assert manifest["operator_observation"]["reported_verbatim"] == (
        "Yes, I see 3 lines, maybe 20mm each, two gaps"
    )
    assert manifest["result"]["status"] == "scoped-pass"
    assert (
        manifest["result"]["repeated_reduce_restore_execution_evidence"]
        == "operator-observed"
    )


def _comparable_records(
    records: tuple[KnownCommand, ...],
) -> tuple[tuple[str, dict[str, Any]], ...]:
    comparable = []
    for record in records:
        if record.name == "additional_delay":
            continue
        values = dict(record.values)
        if record.name == "file_checksum":
            values["value"] = 0
        comparable.append((record.name, values))
    return tuple(comparable)


def test_zero_power_control_is_p0_negative_evidence() -> None:
    manifest = _read_json(ZERO_MANIFEST_PATH)
    artifacts = manifest["artifacts"]
    control_path = ZERO_FIXTURE / artifacts["control"]["file"]
    dwell_path = ZERO_FIXTURE / artifacts["dwell"]["file"]
    _, _, control = _decode(control_path)
    _, _, dwell = _decode(dwell_path)
    control_names = [record.name for record in control]
    dwell_names = [record.name for record in dwell]

    assert [
        record.name
        for record in control
        if record.name in {"move_absolute", "cut_absolute"}
    ] == ["move_absolute", "cut_absolute"] * 4
    assert [
        record.values["power_percent"]
        for record in control
        if record.name
        in {
            "layer_laser_1_min_power",
            "layer_laser_1_max_power",
            "laser_1_min_power",
            "laser_1_max_power",
        }
    ] == [0.0] * 4
    assert [
        record.values["power_percent"]
        for record in control
        if record.name
        in {
            "layer_laser_2_min_power",
            "layer_laser_2_max_power",
            "laser_2_min_power",
            "laser_2_max_power",
        }
    ] == pytest.approx([39.99877922236465] * 4)
    assert [
        record.values
        for record in control
        if record.name == "enable_laser_tube_start"
    ] == [{"enabled": 1}]
    assert [
        record.values["power_percent"]
        for record in control
        if record.name in {"through_power_1", "through_power_2"}
    ] == pytest.approx([0.0061038881767686015] * 2)
    assert control_names.count("additional_delay") == 0
    assert control_names.count("laser_interval") == 0

    delays = [
        record.values["time_ms"]
        for record in dwell
        if record.name == "additional_delay"
    ]
    assert delays == [200.0] * 4
    assert dwell_names.count("laser_interval") == 0
    assert _comparable_records(control) == _comparable_records(dwell)

    assert manifest["transmission"]["control"]["host_log"] == (
        EXPECTED_HOST_LOG
    )
    assert manifest["transmission"]["dwell"] == {
        "transmitted": False,
        "packets": 0,
        "retries": 0,
        "controller_acknowledgement": False,
        "execution_acknowledgement": False,
    }
    assert artifacts["dwell"]["quarantine"] == "do-not-send"
    assert manifest["operator_observation"]["reported_verbatim"] == (
        "There was laser emission. I see a clearly drawn rectangle, maybe "
        "25mmx50mm"
    )
    result = manifest["result"]
    assert result["status"] == "p0-negative-evidence"
    assert result["causal_mechanism"] == "not-isolated"
    assert result["c611_dwell_execution_evidence"] == "untested"
    assert result["dwell_artifact_disposition"] == "do-not-send"
