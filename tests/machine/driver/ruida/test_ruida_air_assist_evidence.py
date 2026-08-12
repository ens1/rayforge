"""Verify scoped Boss LS2040 air-assist evidence."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from ruida_re import KnownCommand, RuidaCodec, swizzle

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = (
    ROOT
    / "tests"
    / "machine"
    / "driver"
    / "ruida"
    / "fixtures"
    / "hardware"
    / "boss-ls2040-usb-serial-rayforge-air-assist-v1"
)
MANIFEST = EVIDENCE / "manifest-v1.json"
ARTIFACTS = {
    "air_off_control": (
        (
            "boss-ls2040-proven-air-assist-off-control-15pct-100mms-"
            "x100-y75-offline-v1.rd"
        ),
        "b52bee4c14dd9a0346a77684904194ec08eea2e8336546e6368e635d203cca38",
    ),
    "air_on_confirmed": (
        "boss-ls2040-proven-air-assist-on-15pct-100mms-x100-y75-offline-v1.rd",
        "231e88b14ee36fb66d7ec1f28e5177df19a5309353bf4f54709f410ee9d23795",
    ),
}


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _program(
    key: str,
) -> tuple[bytes, Any, tuple[KnownCommand, ...]]:
    filename, _ = ARTIFACTS[key]
    raw = (EVIDENCE / filename).read_bytes()
    program = RuidaCodec(context="job").decode(raw, container="rd")
    records = tuple(
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    )
    return raw, program, records


def _normalized(records: tuple[KnownCommand, ...]) -> list[dict[str, Any]]:
    result = []
    for record in records:
        if record.name == "file_checksum":
            continue
        values = dict(record.values)
        if record.name == "layer_control" and values["operation"] in {
            0x12,
            0x13,
        }:
            values["operation"] = "AIR_STATE"
        result.append(
            {"name": record.name, "opcode": record.opcode, "values": values}
        )
    return result


def test_motion_artifacts_are_content_addressed_and_exact() -> None:
    manifest = _manifest()
    codec = RuidaCodec(context="job")

    assert {path.name for path in EVIDENCE.iterdir()} == {
        "README.md",
        "manifest-v1.json",
        ARTIFACTS["air_off_control"][0],
        ARTIFACTS["air_on_confirmed"][0],
    }
    for key, (filename, expected_digest) in ARTIFACTS.items():
        raw, program, records = _program(key)
        artifact = manifest["motion_artifacts"][key]
        assert artifact["file"] == filename
        assert len(raw) == artifact["size_bytes"] == 580
        assert sha256(raw).hexdigest() == artifact["sha256"]
        assert artifact["sha256"] == expected_digest
        assert program.issues == artifact["issues"] == []
        assert len(program.records) == artifact["records"] == 78
        assert len(records) == artifact["known_records"] == 78
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


def test_motion_pair_changes_only_air_state_and_checksum() -> None:
    _, _, off = _program("air_off_control")
    _, _, on = _program("air_on_confirmed")
    differences = [
        (index, left.name, left.values, right.values)
        for index, (left, right) in enumerate(zip(off, on, strict=True))
        if left.values != right.values
    ]

    assert _normalized(off) == _normalized(on)
    assert differences == [
        (52, "layer_control", {"operation": 18}, {"operation": 19}),
        (76, "file_checksum", {"value": 25935}, {"value": 25936}),
    ]
    assert [
        record.values["operation"]
        for record in off
        if record.name == "layer_control"
        and record.values["operation"] in {0x12, 0x13}
    ] == [0x12]
    assert [
        record.values["operation"]
        for record in on
        if record.name == "layer_control"
        and record.values["operation"] in {0x12, 0x13}
    ] == [0x13]


def test_standalone_sequence_is_only_exact_layer_controls() -> None:
    sequence = _manifest()["standalone_sequence"]
    codec = RuidaCodec(context="job")

    assert sequence["runtime"] == {
        "ruida_re_revision": ("8c483cd17793d84a1cd83d80dc1d0760e6582cc8"),
        "transport_api": "ruida-re SerialTransport and SerialLink",
        "legacy_rayforge_ruida_client_used": False,
        "job_or_layer_envelope_used": False,
        "udp_packet_checksum_used": False,
    }
    assert [item["stage"] for item in sequence["commands"]] == [
        "pre_off",
        "air_on",
        "final_off",
    ]
    assert [item["operation"] for item in sequence["commands"]] == [
        0x12,
        0x13,
        0x12,
    ]
    for item in sequence["commands"]:
        logical = bytes.fromhex(item["logical_hex"])
        wire = bytes.fromhex(item["wire_hex"])
        program = codec.decode(logical, container="logical")
        assert program.issues == []
        assert len(program.records) == 1
        record = program.records[0]
        assert isinstance(record, KnownCommand)
        assert record.name == item["command"] == "layer_control"
        assert record.values == {"operation": item["operation"]}
        assert codec.encode(program, container="logical") == logical
        assert swizzle(logical, 0x88) == wire
        assert len(wire) == item["wire_size_bytes"] == 3
        assert sha256(logical).hexdigest() == item["logical_sha256"]
        assert sha256(wire).hexdigest() == item["wire_sha256"]
        assert item["host_write_and_flush_completed"] is True
        assert item["retries"] == 0
    assert sequence["serialized_command_names"] == ["layer_control"] * 3
    assert (
        sequence["motion_mark_laser_enable_power_dwell_and_pulse_commands"]
        == 0
    )


def test_air_observations_and_claims_remain_scoped() -> None:
    manifest = _manifest()
    motion = manifest["motion_control_transmission"]
    standalone = manifest["standalone_sequence"]
    result = manifest["result"]

    assert manifest["schema"] == "ruida-re.hardware-air-assist-observation.v1"
    assert motion["host_log"] == {
        "scope": "host-side driver transfer summary",
        "packets": 1,
        "payload_bytes": 580,
        "retries": 0,
        "controller_acknowledgement": False,
        "execution_acknowledgement": False,
    }
    assert motion["operator_observation"]["reported_verbatim"] == (
        "I think it had air flow, but it's hard to tell with the sound of "
        "the motors."
    )
    assert motion["operator_observation"]["interpretation"] == "inconclusive"
    air_on = manifest["air_on_motion_transmission"]
    assert air_on == {
        "explicit_operator_approval": True,
        "artifact_sha256": (
            "231e88b14ee36fb66d7ec1f28e5177df19a5309353bf4f54709f410ee9d23795"
        ),
        "transmitted": True,
        "driver": "stock Rayforge RuidaSerialDriver",
        "host_log": {
            "scope": "host-side driver transfer summary",
            "packets": 1,
            "payload_bytes": 580,
            "retries": 0,
            "controller_acknowledgement": False,
            "execution_acknowledgement": False,
        },
        "operator_observation": {
            "reported_verbatim": (
                "Air assist is confirmed, I felt the solenoid turn on then off"
            ),
            "instrumented_metrology": False,
            "observation_mode": "operator tactile observation",
            "interpretation": "scoped full-layer job-context pass",
        },
    }
    assert standalone["host_interval"] == {
        "seconds": 5.002178,
        "clock": "monotonic",
        "scope": "host-side sequence timing only",
        "controller_or_pneumatic_timing_metrology": False,
    }
    assert standalone["host_log"] == {
        "scope": "three direct host serial writes and flushes",
        "writes": 3,
        "payload_bytes": 9,
        "retries": 0,
        "controller_acknowledgement": False,
        "state_acknowledgement": False,
        "execution_acknowledgement": False,
    }
    assert manifest["standalone_operator_observation"][
        "reported_verbatim"
    ] == (
        "No motion or emission.\n\nNo change, no relay clicks, nothing. But "
        "lightburn also seems to have failed on this. I could have bad "
        "hardware. I would expect to hear a relay or solenoid click"
    )
    assert result["status"] == (
        "scoped-full-layer-air-assist-pass-with-standalone-"
        "negative-observation"
    )
    assert result["controller_controlled_air_on_this_setup"] == (
        "scoped-full-layer-job-observed-pass"
    )
    assert result["standalone_ca01_air_semantics"] == (
        "no-response-inconclusive-not-supported"
    )
    assert result["full_job_air_on_semantics"] == (
        "operator-observed-pass-for-exact-artifact"
    )
    assert result["air_assist_timing_metrology"] == "not-performed"
    assert result["pressure_or_flow_metrology"] == "not-performed"
    assert result["relay_routing_current_or_electrical_validation"] == (
        "not-performed"
    )
    assert result["other_controllers_or_firmware"] == "not-tested"
    assert result["udp_transport"] == "not-tested"
    assert result["standalone_machine_side_fault"] == (
        "operator-suggested-inference-only"
    )
    assert result["standalone_no_response_causality"] == "not-established"
    assert result["encoder_or_compiler_change"] == "none"
    assert result["profile_promotion"] == "none"
    assert result["broad_profile_conclusion"] == "not-established"


def test_manifest_and_documentation_contain_no_private_identifiers() -> None:
    manifest_text = MANIFEST.read_text(encoding="utf-8")
    readme_text = (EVIDENCE / "README.md").read_text(encoding="utf-8")
    serialized = (manifest_text + readme_text).lower()
    documented = " ".join(readme_text.split())

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
    assert "do not establish causality" in documented
    assert (
        "scoped pass for normal full-layer job-context air assist"
        in documented
    )
    assert (
        "does not make the standalone sequence a supported manual toggle"
        in (documented)
    )
