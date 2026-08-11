"""Verify scoped Boss LS2040 native-raster negative evidence."""

from __future__ import annotations

import json
import math
from hashlib import sha256
from pathlib import Path
from typing import Any

from ruida_re.program import KnownCommand, decode

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = (
    ROOT
    / "tests"
    / "machine"
    / "driver"
    / "ruida"
    / "fixtures"
    / "hardware"
    / "boss-ls2040-usb-serial-rayforge-native-raster-failure-v1"
)
MANIFEST = EVIDENCE / "manifest-v1.json"
ARTIFACT = EVIDENCE / (
    "boss-ls2040-proven-vector-native-raster-bottom-strip-offline-v1"
    ".rd.quarantined"
)
EXPECTED_SHA256 = (
    "ee1ae328fd431b75f43f0bf61feaf1e08e4ef511fb0c3615d95b0d38aab5e3b4"
)


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _known_records() -> list[KnownCommand]:
    program = decode(ARTIFACT.read_bytes())
    return [
        record
        for record in program.records
        if isinstance(record, KnownCommand)
    ]


def _raster_marks() -> list[tuple[str, float, float]]:
    position: tuple[float, float] | None = None
    modulation: float | None = None
    selected_layer: int | None = None
    marks = []
    for record in _known_records():
        if record.name == "select_layer":
            selected_layer = record.values["layer"]
            position = None
            modulation = None
            continue
        if selected_layer != 1:
            continue
        if record.name == "immediate_power_1":
            modulation = record.values["power_percent"]
            continue
        if record.name in {"immediate_power_3", "block_end"}:
            continue
        if record.name in {"move_absolute", "cut_absolute"}:
            target = (record.values["x_mm"], record.values["y_mm"])
        elif record.name in {"move_horizontal", "cut_horizontal"}:
            assert position is not None
            target = (position[0] + record.values["dx_mm"], position[1])
        elif record.name in {"move_vertical", "cut_vertical"}:
            assert position is not None
            target = (position[0], position[1] + record.values["dy_mm"])
        elif record.name.startswith(("move_", "cut_")):
            raise AssertionError(f"Unexpected motion record {record.name}")
        else:
            continue
        if record.name.startswith("cut_"):
            assert position is not None
            assert modulation is not None
            marks.append(
                (record.name, modulation, math.dist(position, target))
            )
        position = target
    return marks


def test_artifact_is_exact_and_quarantined() -> None:
    raw = ARTIFACT.read_bytes()
    manifest = _manifest()
    artifact = manifest["artifact"]

    assert len(raw) == 917
    assert sha256(raw).hexdigest() == EXPECTED_SHA256
    assert artifact["sha256"] == EXPECTED_SHA256
    assert artifact["size_bytes"] == len(raw)
    assert artifact["file"] == ARTIFACT.name
    assert artifact["original_capture_filename"].endswith(".rd")
    assert artifact["quarantine"] == "negative-evidence-do-not-resend"
    assert not list(EVIDENCE.glob("*.rd"))


def test_artifact_decodes_losslessly_with_expected_layer_modes() -> None:
    raw = ARTIFACT.read_bytes()
    program = decode(raw)
    records = _known_records()

    assert program.issues == []
    assert len(program.records) == 147
    assert len(records) == 147
    assert program.encode(checksum_policy="preserve") == raw
    assert program.encode(checksum_policy="recompute") == raw
    assert [
        record.values
        for record in records
        if record.name == "layer_mode_or_attributes"
    ] == [
        {"layer": 0, "value": 0},
        {"layer": 1, "value": 2},
    ]
    assert [
        record.values["operation"]
        for record in records
        if record.name == "layer_control"
    ] == [0, 48, 16, 18, 1, 48, 16, 18]
    assert [
        record.values["enabled"]
        for record in records
        if record.name == "enable_laser_tube_start"
    ] == [1, 1]


def test_dark_spans_are_absolute_and_only_low_edges_are_axial() -> None:
    marks = _raster_marks()
    high = [mark for mark in marks if math.isclose(mark[1], 14.8995910395)]
    low = [mark for mark in marks if math.isclose(mark[1], 1.95934810474)]

    assert [mark[0] for mark in high] == ["cut_absolute"] * 6
    assert [mark[2] for mark in high] == [24, 17, 23, 23, 17, 24]
    assert [mark[0] for mark in low] == ["cut_horizontal"] * 8
    assert [mark[2] for mark in low] == [1] * 8
    assert len(marks) == 14


def test_observation_and_transport_claims_remain_scoped() -> None:
    manifest = _manifest()
    quotes = manifest["operator_observation"]["reported_verbatim"]
    result = manifest["result"]
    host_log = manifest["transmission"]["host_log"]

    assert quotes == [
        (
            "I see the first row, not the second row. I saw the second row "
            "movement, but the laser didn't fire"
        ),
        "Only one row is showing, 3 lines about 30cm",
        "yes, 30mm/3cm",
    ]
    assert manifest["operator_observation"]["instrumented_metrology"] is False
    assert result["vector_row"]["status"] == "pass"
    assert result["native_raster"]["status"] == "fail"
    assert result["causal_mechanism"] == "not-isolated"
    assert result["broad_profile_conclusion"] == "not-established"
    assert result["artifact_disposition"] == "quarantined-do-not-resend"
    assert host_log == {
        "scope": "host-side driver transfer summary",
        "packets": 1,
        "payload_bytes": 917,
        "retries": 0,
        "controller_acknowledgement": False,
        "execution_acknowledgement": False,
    }
