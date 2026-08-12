"""Verify scoped Boss LS2040 logical-Z readout evidence."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from ruida_re import KnownCommand, RuidaCodec

EVIDENCE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "hardware"
    / "boss-ls2040-usb-serial-rayforge-logical-z-v1"
)
MANIFEST = EVIDENCE / "manifest-v1.json"
README = EVIDENCE / "README.md"
CASES = {
    "positive": {
        "file": (
            "boss-ls2040-native-raster-z-plus-1mm-held-20pct-"
            "x20-y115-offline-v5.rd"
        ),
        "sha256": (
            "3a48300c78ac3416044a962dbb7fe78e8e7e1d4380a53986fa0aa2b65f8afa82"
        ),
        "checksum": 35715,
        "z_deltas_mm": (-1.0, 1.0),
    },
    "negative": {
        "file": (
            "boss-ls2040-native-raster-z-minus-1mm-held-20pct-"
            "x30-y133-offline-v6.rd"
        ),
        "sha256": (
            "36fcf52b5029777383e31b5e1885c9b6bf3865ca5c621dce1a1bc4a64c540d43"
        ),
        "checksum": 35518,
        "z_deltas_mm": (1.0, -1.0),
    },
}


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _program(
    kind: str,
) -> tuple[Path, bytes, Any, tuple[KnownCommand, ...]]:
    path = EVIDENCE / CASES[kind]["file"]
    raw = path.read_bytes()
    program = RuidaCodec(context="job").decode(raw, container="rd")
    records = tuple(
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    )
    return path, raw, program, records


def _values(
    records: tuple[KnownCommand, ...],
    name: str,
) -> list[dict[str, Any]]:
    return [record.values for record in records if record.name == name]


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
        assert x is not None and y is not None
        points.append((x, y))
    return {
        "min_x": min(point[0] for point in points),
        "min_y": min(point[1] for point in points),
        "max_x": max(point[0] for point in points),
        "max_y": max(point[1] for point in points),
    }


def test_artifact_identity_decode_and_roundtrip_are_exact() -> None:
    artifacts = _manifest()["artifacts"]
    codec = RuidaCodec(context="job")

    for kind, expected in CASES.items():
        path, raw, program, records = _program(kind)
        artifact = artifacts[kind]
        checksum = expected["checksum"]
        assert len(raw) == artifact["size_bytes"] == 673
        assert sha256(raw).hexdigest() == artifact["sha256"]
        assert artifact["sha256"] == expected["sha256"]
        assert artifact["file"] == path.name == expected["file"]
        assert program.issues == artifact["issues"] == []
        assert len(program.records) == artifact["records"] == 114
        assert len(records) == artifact["known_records"] == 114
        assert artifact["opaque_records"] == 0
        assert program.source_checksum_basis == artifact["checksum"]
        assert artifact["checksum"] == checksum
        assert _values(records, "file_checksum") == [{"value": checksum}]
        assert (
            codec.encode(
                program,
                container="rd",
                checksum_policy="preserve",
            )
            == raw
        )
        assert (
            codec.encode(
                program,
                container="rd",
                checksum_policy="recompute",
            )
            == raw
        )
    assert {path.name for path in EVIDENCE.iterdir()} == {
        "README.md",
        "manifest-v1.json",
        *(expected["file"] for expected in CASES.values()),
    }


def test_balanced_z_wrappers_and_native_raster_are_exact() -> None:
    process = _manifest()["process"]
    expected_prefix = [
        (item["name"], item["values"]) for item in process["z_wrapper_prefix"]
    ]
    expected_chunks = process["decoded_marking_plan"]["signed_mark_chunks_mm"]
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

    for kind, expected in CASES.items():
        _, _, _, records = _program(kind)
        case = process["cases"][kind]
        deltas = expected["z_deltas_mm"]
        z_indices = [
            index
            for index, record in enumerate(records)
            if record.name == "z_offset_delta"
        ]
        z_values = _values(records, "z_offset_delta")
        assert z_values == [{"delta_mm": value} for value in deltas]
        assert [value["delta_mm"] for value in z_values] == (
            case["wire_z_offset_deltas_mm"]
        )
        assert (
            sum(value["delta_mm"] for value in z_values)
            == (case["wire_z_offset_net_mm"])
            == 0.0
        )
        assert _values(records, "enable_laser_tube_start") == [
            {"enabled": 3},
            {"enabled": 1},
            {"enabled": 3},
        ]
        assert len(z_indices) == 2
        for index, delta in zip(z_indices, deltas, strict=True):
            context = records[index - len(expected_prefix) : index]
            assert [
                (record.name, record.values) for record in context
            ] == expected_prefix
            assert records[index].opcode == "8003"
            assert records[index].values == {"delta_mm": delta}
            assert not any(
                record.name.startswith(("move_", "cut_"))
                for record in (*context, records[index])
            )

        cut_indices = [
            index
            for index, record in enumerate(records)
            if record.name.startswith("cut_")
        ]
        assert min(cut_indices) > z_indices[0]
        assert max(cut_indices) < z_indices[1] - len(expected_prefix)

        cuts = [record for record in records if record.name.startswith("cut_")]
        assert [record.name for record in cuts] == ["cut_horizontal"] * 20
        assert [record.opcode for record in cuts] == ["aa"] * 20
        assert [record.values["dx_mm"] for record in cuts] == expected_chunks
        assert max(abs(value) for value in expected_chunks) == 4.0
        assert len(_values(records, "move_absolute")) == 6
        assert len(_values(records, "move_vertical")) == 4
        assert _motion_bounds(records) == case["controller_bounds_mm"]

        names = {record.name for record in records}
        assert set(process["forbidden_records_absent"]).isdisjoint(names)
        assert _values(records, "layer_speed") == [
            {"layer": 0, "speed_mm_s": 100.0}
        ]
        assert _values(records, "active_speed") == [{"speed_mm_s": 100.0}]
        powers = [
            _values(records, name)[0]["power_percent"] for name in power_names
        ]
        assert len(set(powers)) == 1
        assert powers[0] == process["encoded_power_percent"]


def test_transfer_and_controller_readout_claims_remain_scoped() -> None:
    manifest = _manifest()
    transmissions = manifest["transmissions"]
    observations = manifest["operator_observations"]
    result = manifest["result"]

    assert manifest["schema"] == (
        "ruida-re.hardware-logical-z-readout-observation.v1"
    )
    expected_driver_log = (
        "Ruida program transfer completed: 1 packet(s), 0 retry(s). "
        "Controller execution is not monitored."
    )
    expected_host_log = {
        "scope": "host-side driver transfer summary",
        "packets": 1,
        "payload_bytes": 673,
        "retries": 0,
        "controller_acknowledgement": False,
        "execution_acknowledgement": False,
    }
    for kind, expected in CASES.items():
        transmission = transmissions[kind]
        assert transmission["explicit_operator_approval"] is True
        assert transmission["artifact_sha256"] == expected["sha256"]
        assert transmission["driver"] == "stock Rayforge RuidaSerialDriver"
        assert transmission["driver_log_verbatim"] == expected_driver_log
        assert transmission["host_log"] == expected_host_log

    positive = observations["positive"]
    negative = observations["negative"]
    assert positive["reported_verbatim"] == (
        "It went to 17.2mm during cutting, then returned to 18.2"
    )
    assert positive["starting_readout_mm"] == 18.2
    assert positive["during_cutting_readout_mm"] == 17.2
    assert positive["final_readout_mm"] == 18.2
    assert positive["calculated_transient_change_mm"] == -1.0
    assert positive["calculated_final_difference_mm"] == 0.0
    assert positive["instrumented_metrology"] is False
    assert positive["independent_position_instrumentation"] is False
    assert (
        positive["decoded_marking_pattern_confirmed_by_this_report"] is False
    )
    assert negative["reported_verbatim"] == (
        "Yes, went to 19.2 and is now at 18.2. No collision or unexpected "
        "movement. Marks look great, as expected"
    )
    assert negative["starting_readout_mm"] == 18.2
    assert negative["during_cutting_readout_mm"] == 19.2
    assert negative["final_readout_mm"] == 18.2
    assert negative["calculated_transient_change_mm"] == 1.0
    assert negative["calculated_final_difference_mm"] == 0.0
    assert negative["instrumented_metrology"] is False
    assert negative["independent_position_instrumentation"] is False
    assert negative["decoded_marking_pattern_confirmation"] == (
        "qualitative expected-looking marks"
    )
    assert negative["collision_or_unexpected_movement"] == (
        "not-operator-observed"
    )

    assert result["status"] == (
        "scoped-paired-logical-z-controller-readout-pass"
    )
    assert result["positive_logical_z"] == {
        "serialized_offset_mm": 1.0,
        "wire_entry_delta_mm": -1.0,
        "wire_restore_delta_mm": 1.0,
        "operator_reported_readout_change_mm": -1.0,
        "operator_reported_final_readout_difference_mm": 0.0,
    }
    assert result["negative_logical_z"] == {
        "serialized_offset_mm": -1.0,
        "wire_entry_delta_mm": 1.0,
        "wire_restore_delta_mm": -1.0,
        "operator_reported_readout_change_mm": 1.0,
        "operator_reported_final_readout_difference_mm": 0.0,
    }
    assert result["completed_job_readout_restore"] == (
        "operator-observed-for-both-exact-artifacts"
    )
    assert result["paired_controller_readout_sign_response"] == (
        "operator-observed-for-both-exact-artifacts"
    )
    assert result["physical_axis_direction"] == "not-established"
    assert result["bed_or_head_motion"] == "not-established"
    assert result["mechanical_displacement_metrology"] == "not-performed"
    assert result["absolute_position_accuracy"] == "not-established"
    assert result["backlash_or_repeatability"] == "not-established"
    assert result["positive_decoded_marking_pattern"] == (
        "not-confirmed-by-its-report"
    )
    assert result["negative_marking_appearance"] == (
        "operator-reported-as-expected"
    )
    assert result["interrupted_or_failed_restore"] == "not-tested"
    assert result["profile_promotion"] == "none"
    assert result["encoder_or_compiler_change"] == "none"
    assert result["broad_profile_conclusion"] == "not-established"


def test_published_evidence_has_no_private_identifiers() -> None:
    serialized = (
        MANIFEST.read_text(encoding="utf-8")
        + README.read_text(encoding="utf-8")
    ).lower()
    for private_value in (
        "/dev/",
        "/tmp/",
        "/private/",
        "/users/",
        "cu.usb",
        "tty.usb",
        "usbserial",
        "usbmodem",
    ):
        assert private_value not in serialized
    assert "physical displacement accuracy" in serialized
    assert "does not promote the default profile" in serialized
