"""Verify scoped Boss LS2040 DA000400 status evidence."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from ruida_re import KnownCommand, RuidaCodec, swizzle, unswizzle

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = (
    ROOT
    / "tests"
    / "machine"
    / "driver"
    / "ruida"
    / "fixtures"
    / "hardware"
    / "boss-ls2040-usb-serial-status-da000400-v1"
)
MANIFEST = EVIDENCE / "manifest-v1.json"

EXPECTED_RUNS = {
    "stage_a_idle_baseline": [(0x00000000, 17)],
    "stage_b_manual_panel_jogs": [(0x00000000, 101)],
    "stage_c_natural_job": [
        (0x00000000, 16),
        (0x00010401, 71),
        (0x00010405, 5),
        (0x00010600, 49),
    ],
    "stage_d_panel_pause_resume": [
        (0x00010600, 17),
        (0x00010401, 10),
        (0x00410403, 1),
        (0x00010403, 191),
        (0x00830401, 1),
        (0x00010401, 52),
        (0x00010405, 6),
        (0x00010600, 190),
    ],
    "stage_e_programmed_dwell": [
        (0x00010600, 16),
        (0x00010401, 27),
        (0x00010405, 5),
        (0x00010600, 44),
    ],
    "stage_f_software_stop": [
        (0x00010600, 16),
        (0x00010401, 10),
        (0x00510600, 1),
        (0x00010600, 114),
    ],
}

FINAL_CAPTURE_HASHES = {
    "stage_a_idle_baseline": (
        "5392b8280b0d2f6d40bcdef4745f1bc464f296599157726784e964be5d3d841f"
    ),
    "stage_b_manual_panel_jogs": (
        "125f61ec378e213e2e819de1de57bf9ad9b07511d111c355b8c40fbc75c94f3f"
    ),
    "stage_c_natural_job": (
        "4fd85aab652a92aba086811f56cb476081b4332e80794d2b13d9c4f431734ff0"
    ),
    "stage_d_panel_pause_resume": (
        "2ea9865a474249d42fcfe0e6257831941690897551855c3f0b23d0b4cbf305ba"
    ),
    "stage_e_programmed_dwell": (
        "ccd681300a347bd035f0ff1333b92de865359a3b1adc8907bf776fdd4a044065"
    ),
    "stage_f_software_stop": (
        "90b97c90ad9f3716ce721b6fedb3f8151f225d9d0fce1bd0a15da3a65663b738"
    ),
}

PRE_ANNOTATION_HASHES = {
    "stage_c_natural_job": (
        "0e5de1b40848985b6ebd43834d1e5a40fc8cdd4d233e5b5fb88d3a224ab0751f"
    ),
    "stage_d_panel_pause_resume": (
        "8d871a48866055dc67fbaaebbaf21533e18ec8f522c74f7292a07a21cae0e2e4"
    ),
    "stage_e_programmed_dwell": (
        "edf552386cc034ef617d4c9960a8711c448bd72b4b4b57857aebc03da9fa82d9"
    ),
    "stage_f_software_stop": (
        "6d89468e1961ab8d808502e4e4baa3ad86f66412ab9d0410a0ffedc78e4f88cd"
    ),
}


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _status_words(manifest: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {item["raw_word"]: item for item in manifest["status_words"]}


def test_protocol_bytes_and_status_replies_are_exact() -> None:
    manifest = _manifest()
    protocol = manifest["protocol"]
    request = protocol["request"]
    magic = manifest["scope"]["transport"]["magic"]
    logical = bytes.fromhex(request["logical_hex"])
    wire = bytes.fromhex(request["serial_wire_hex"])

    assert manifest["schema"] == (
        "ruida-re.hardware-machine-status-observation.v1"
    )
    assert protocol["semantic_address"] == 0x0200
    assert protocol["encoded_address_groups_hex"] == "0400"
    assert logical == bytes.fromhex("da000400")
    assert wire == bytes.fromhex("d4898d89")
    assert swizzle(logical, magic) == wire
    assert protocol["udp_frame"] == {
        "wire_hex": "0273d4898d89",
        "derivation": (
            "standard two-byte UDP checksum prefix plus the swizzled request"
        ),
        "hardware_observed": False,
    }

    request_program = RuidaCodec(
        magic=magic,
        context="request",
    ).decode(logical, container="logical")
    assert request_program.issues == []
    request_record = request_program.records[0]
    assert isinstance(request_record, KnownCommand)
    assert request_record.name == request["command"] == "get_setting"
    assert request_record.values == {"address": 0x0200}

    words = _status_words(manifest)
    assert set(words) == {
        0x00000000,
        0x00010401,
        0x00010403,
        0x00010405,
        0x00010600,
        0x00410403,
        0x00510600,
        0x00830401,
    }
    for raw_word, item in words.items():
        reply_logical = bytes.fromhex(item["reply_logical_hex"])
        reply_wire = bytes.fromhex(item["reply_serial_wire_hex"])
        assert len(reply_logical) == protocol["reply"]["logical_length_bytes"]
        assert unswizzle(reply_wire, magic) == reply_logical
        assert swizzle(reply_logical, magic) == reply_wire
        reply = RuidaCodec(
            magic=magic,
            context="reply",
        ).decode(reply_logical, container="logical")
        assert reply.issues == []
        record = reply.records[0]
        assert isinstance(record, KnownCommand)
        assert record.name == protocol["reply"]["command"]
        assert record.values == {"address": 0x0200, "value": raw_word}


def test_capture_hashes_and_relative_runs_are_preserved() -> None:
    captures = _manifest()["captures"]

    assert set(captures) == set(EXPECTED_RUNS)
    for name, expected_runs in EXPECTED_RUNS.items():
        capture = captures[name]
        provenance = capture["provenance"]
        actual_runs = [
            (run["raw_word"], run["count"]) for run in capture["runs"]
        ]
        assert actual_runs == expected_runs
        assert (
            sum(count for _, count in actual_runs) == capture["sample_count"]
        )
        assert capture["error_count"] == 0
        assert (
            provenance["final_capture_sha256"] == (FINAL_CAPTURE_HASHES[name])
        )
        assert len(provenance["final_capture_sha256"]) == 64
        for run in capture["runs"]:
            assert run["first_sample_relative_seconds"] >= 0
            assert (
                run["last_sample_relative_seconds"]
                >= (run["first_sample_relative_seconds"])
            )
            assert run["sample_span_seconds"] >= 0

    for name, expected_hash in PRE_ANNOTATION_HASHES.items():
        assert (
            captures[name]["provenance"]["pre_annotation_sha256"]
            == expected_hash
        )
    for name in (
        "stage_a_idle_baseline",
        "stage_b_manual_panel_jogs",
    ):
        provenance = captures[name]["provenance"]
        assert provenance["pre_annotation_sha256"] is None
        assert provenance["pre_annotation_hash_availability"] == (
            "not-recorded"
        )


def test_natural_completion_requires_observed_active_then_stable_idle() -> (
    None
):
    manifest = _manifest()
    captures = manifest["captures"]
    profile = manifest["boss_status_profile"]
    natural = profile["natural_completion_rule"]

    assert profile["preflight_idle_words"] == [0, 0x00010600]
    assert profile["post_active_completion_word"] == 0x00010600
    assert profile["post_active_zero_is_completion"] is False
    assert profile["unknown_word_policy"] == "fail-closed-unconfirmed"
    assert natural == {
        "same_client_session_required": True,
        "preflight_idle_required": True,
        "successful_send_required": True,
        "observed_active_word_required": True,
        "stable_post_active_word": 0x00010600,
        "minimum_consecutive_stable_samples": 3,
        "missed_active_short_job_outcome": "manual-confirmation-required",
    }

    stage_c = captures["stage_c_natural_job"]
    assert [run["raw_word"] for run in stage_c["runs"]] == [
        0x00000000,
        0x00010401,
        0x00010405,
        0x00010600,
    ]
    assert (
        stage_c["runs"][-1]["count"]
        >= natural["minimum_consecutive_stable_samples"]
    )
    for name in (
        "stage_c_natural_job",
        "stage_d_panel_pause_resume",
        "stage_e_programmed_dwell",
    ):
        receipt = captures[name]["receipt"]
        assert receipt["job_submissions"] == 1
        assert receipt["retries"] == 0


def test_pause_and_dwell_are_active_not_completion() -> None:
    manifest = _manifest()
    captures = manifest["captures"]
    words = _status_words(manifest)
    profile = manifest["boss_status_profile"]

    assert {
        raw_word: item["scoped_interpretation"]
        for raw_word, item in words.items()
    } == {
        0x00000000: "idle-preflight-only",
        0x00010401: "active-running-or-dwell",
        0x00010403: "active-paused",
        0x00010405: "active-finishing-tail",
        0x00010600: "idle-or-post-active-terminal",
        0x00410403: "active-pause-transition",
        0x00510600: "software-stop-transition-nonterminal",
        0x00830401: "active-resume-transition",
    }
    assert words[0x00010403]["legacy_decoder_fields"]["part_end"] is True
    assert words[0x00410403]["legacy_decoder_fields"]["part_end"] is True
    assert words[0x00010403]["scoped_interpretation"] == "active-paused"
    assert words[0x00410403]["scoped_interpretation"] == (
        "active-pause-transition"
    )
    assert profile["paused_words"] == [0x00010403]
    assert 0x00010403 in profile["active_or_noncomplete_words"]
    assert 0x00410403 in profile["active_or_noncomplete_words"]
    assert (
        profile["post_active_completion_word"]
        not in profile["active_or_noncomplete_words"]
    )

    stage_d = captures["stage_d_panel_pause_resume"]
    paused_run = stage_d["runs"][3]
    assert paused_run["raw_word"] == 0x00010403
    assert paused_run["count"] == 191
    assert paused_run["sample_span_seconds"] == 38.008427917
    assert stage_d["operator_marker_timestamps"] == {
        "capture_monotonic_correlation_available": False,
        "reason": "stdin marker channel was unavailable",
    }

    stage_e = captures["stage_e_programmed_dwell"]
    dwell = manifest["artifacts"]["programmed_dwell"]["process"]
    active_run = stage_e["runs"][1]
    assert active_run["raw_word"] == 0x00010401
    assert (
        active_run["sample_span_seconds"]
        >= dwell["programmed_stationary_dwell_seconds"]
    )
    assert stage_e["conclusion"] == (
        "0x00010401 remained active across the complete programmed "
        "five-second dwell"
    )


def test_split_replies_reassemble_to_observed_status_words() -> None:
    manifest = _manifest()
    magic = manifest["scope"]["transport"]["magic"]
    stage_e = manifest["captures"]["stage_e_programmed_dwell"]
    words = _status_words(manifest)

    assert len(stage_e["split_reply_observations"]) == 2
    for observation in stage_e["split_reply_observations"]:
        raw_word = observation["raw_word"]
        logical = b"".join(
            bytes.fromhex(fragment)
            for fragment in observation["logical_fragments_hex"]
        )
        wire = b"".join(
            bytes.fromhex(fragment)
            for fragment in observation["serial_wire_fragments_hex"]
        )
        assert logical.hex() == words[raw_word]["reply_logical_hex"]
        assert wire.hex() == words[raw_word]["reply_serial_wire_hex"]
        assert unswizzle(wire, magic) == logical


def test_software_stop_is_cancelled_and_not_an_emergency_stop() -> None:
    manifest = _manifest()
    stage_f = manifest["captures"]["stage_f_software_stop"]
    stop = stage_f["stop_receipt"]
    observation = stage_f["post_stop_status_observation"]
    rule = manifest["boss_status_profile"]["software_stop_rule"]
    words = _status_words(manifest)

    logical = bytes.fromhex(stop["logical_hex"])
    wire = bytes.fromhex(stop["serial_wire_hex"])
    assert logical == bytes.fromhex("d801")
    assert wire == bytes.fromhex("d209")
    assert swizzle(logical, 0x88) == wire
    assert sha256(wire).hexdigest() == stop["packet_sha256"]
    assert stop["packet_count"] == stop["transmissions"] == 1
    assert stop["completed_packets"] == 1
    assert stop["retries"] == 0
    assert [run["raw_word"] for run in stage_f["runs"]] == [
        0x00010600,
        0x00010401,
        0x00510600,
        0x00010600,
    ]
    assert words[0x00510600]["scoped_interpretation"] == (
        "software-stop-transition-nonterminal"
    )
    assert rule["outcome"] == "cancelled"
    assert rule["counts_as_natural_completion"] is False
    assert rule["counts_as_machine_hours_success"] is False
    assert rule["is_emergency_stop"] is False
    assert rule["terminology"] == "software process stop"
    assert rule["minimum_consecutive_stable_samples"] == 3
    assert rule["minimum_stable_elapsed_seconds"] == 0.6
    assert rule["stability_requirements_combination"] == "all-required"
    assert rule["minimum_controller_query_budget_seconds"] >= 1.0
    assert (
        stage_f["runs"][-1]["count"]
        >= rule["minimum_consecutive_stable_samples"]
    )
    assert observation["first_query_raw_word"] == 0x00510600
    assert observation["first_query_response_latency_seconds"] == (0.468629709)
    assert (
        round(
            observation["first_query_end_relative_seconds"]
            - observation["first_query_start_relative_seconds"],
            9,
        )
        == observation["first_query_response_latency_seconds"]
    )
    assert (
        observation["first_query_response_latency_seconds"]
        < rule["minimum_controller_query_budget_seconds"]
    )
    assert (
        observation["third_sample_stable_elapsed_seconds"]
        < rule["minimum_stable_elapsed_seconds"]
    )
    assert (
        observation[
            "first_sample_satisfying_both_gates_index_within_stable_run"
        ]
        >= rule["minimum_consecutive_stable_samples"]
    )
    assert (
        observation[
            "first_sample_satisfying_both_gates_stable_elapsed_seconds"
        ]
        >= rule["minimum_stable_elapsed_seconds"]
    )


def test_artifact_receipts_are_content_addressed_and_no_retry() -> None:
    manifest = _manifest()
    artifacts = manifest["artifacts"]
    captures = manifest["captures"]
    long_hash = artifacts["long_travel"]["sha256"]
    dwell_hash = artifacts["programmed_dwell"]["sha256"]

    assert long_hash == (
        "c43631b61b6a7f9d0f05f3bf794b13de858490102bbcb42516bf444f16fc554f"
    )
    assert dwell_hash == (
        "f5d449e9138638e2137917b3a47685061570ef513ddb5d3a645a79345a3dd9c5"
    )
    assert artifacts["long_travel"]["size_bytes"] == 745
    assert artifacts["programmed_dwell"]["size_bytes"] == 1403
    assert artifacts["long_travel"]["included_in_fixture"] is False
    assert artifacts["programmed_dwell"]["included_in_fixture"] is False

    for name in (
        "stage_c_natural_job",
        "stage_d_panel_pause_resume",
    ):
        receipt = captures[name]["receipt"]
        assert receipt["packet_count"] == receipt["transmissions"] == 1
        assert receipt["packet_sha256"] == long_hash
        assert receipt["retries"] == 0
    job_receipt = captures["stage_f_software_stop"]["job_receipt"]
    assert job_receipt["packet_count"] == job_receipt["transmissions"] == 1
    assert job_receipt["packet_sha256"] == long_hash
    assert job_receipt["retries"] == 0

    dwell_receipt = captures["stage_e_programmed_dwell"]["receipt"]
    assert dwell_receipt["job_submissions"] == 1
    assert dwell_receipt["packet_lengths"] == [1024, 379]
    assert (
        sum(dwell_receipt["packet_lengths"])
        == artifacts["programmed_dwell"]["size_bytes"]
    )
    assert dwell_receipt["packets_concatenated_sha256"] == dwell_hash
    assert dwell_receipt["retries"] == 0


def test_operator_quotes_and_nonclaims_remain_exact_and_scoped() -> None:
    manifest = _manifest()
    captures = manifest["captures"]
    result = manifest["result"]

    assert captures["stage_a_idle_baseline"]["operator_quotes"] == [
        "No, nothing happened"
    ]
    assert captures["stage_b_manual_panel_jogs"]["operator_quotes"] == [
        (
            "I had to move for less than 3 seconds, my rapid speed is too "
            "high to do that consistentyl"
        ),
        "Several jogs happened, maybe 10",
        "No emission, alarm, change or unexpected behavior",
    ]
    assert captures["stage_c_natural_job"]["operator_quotes"] == [
        "That's all correct, source is off, everything else is operable",
        "Route is clear",
        "Approved",
        (
            "Motion started, stopped, head is near there, no issues or "
            "expected duration. Total motion was maybe 10s?"
        ),
    ]
    assert captures["stage_d_panel_pause_resume"]["operator_quotes"] == [
        "All clear, proceed",
        "paused",
        "paused",
        "resumed",
        "Yes, and no issues",
    ]
    assert captures["stage_e_programmed_dwell"]["operator_quotes"] == [
        "Confirmed, proceed",
        "Yes, everything looks right",
    ]
    assert captures["stage_f_software_stop"]["operator_quotes"] == [
        "Proceed",
        "Everything looked as expected",
    ]
    assert result["generic_ruida_bit_semantics"] == "not-established"
    assert result["generic_part_end_completion_semantics"] == (
        "contradicted-by-paused-observation"
    )
    assert result["udp_transport"] == "not-tested"
    assert result["emergency_stop"] == "not-tested-and-not-claimed"
    assert result["safety_rating"] == "not-established"


def test_public_fixture_contains_no_private_paths_or_clock_origins() -> None:
    manifest = _manifest()
    serialized = MANIFEST.read_text(encoding="utf-8").lower()
    for private_value in (
        "/dev/",
        "/private/",
        "/tmp/",
        "/users/",
        "cu.usb",
        "tty.usb",
        "usbserial",
        "usbmodem",
    ):
        assert private_value not in serialized

    forbidden_clock_keys = {
        "start_monotonic",
        "end_monotonic",
        "t_monotonic",
        "timestamp_monotonic",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            assert forbidden_clock_keys.isdisjoint(value)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(manifest)
