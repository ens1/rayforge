import pytest

from rayforge.pipeline.encoder.base import EncodedOutput, MachineCodeOpMap


def test_payload_populates_legacy_binary_data():
    payload = b"\x00\x88\xff"
    output = EncodedOutput(
        text="program",
        op_map=MachineCodeOpMap(),
        payload=payload,
    )

    assert output.payload == payload
    assert output.driver_data["binary"] == payload


def test_legacy_binary_data_populates_payload():
    payload = b"\x00\x88\xff"
    output = EncodedOutput(
        text="program",
        op_map=MachineCodeOpMap(),
        driver_data={"binary": payload},
    )

    assert output.payload == payload


@pytest.mark.parametrize("value", [bytearray(b"x"), "x", 1])
def test_payload_rejects_non_bytes(value):
    with pytest.raises(TypeError, match="payload must be bytes"):
        EncodedOutput(
            text="program",
            op_map=MachineCodeOpMap(),
            payload=value,
        )


def test_payload_rejects_conflicting_legacy_data():
    with pytest.raises(ValueError, match="disagree"):
        EncodedOutput(
            text="program",
            op_map=MachineCodeOpMap(),
            driver_data={"binary": b"legacy"},
            payload=b"canonical",
        )


def test_encoder_warnings_are_preserved():
    warnings = ("controller owns rapid speed",)
    output = EncodedOutput(
        text="program",
        op_map=MachineCodeOpMap(),
        warnings=warnings,
    )

    assert output.warnings == warnings


@pytest.mark.parametrize("value", [["warning"], ("",), (1,)])
def test_encoder_warnings_require_nonempty_string_tuple(value):
    with pytest.raises(TypeError, match="warnings must be a tuple"):
        EncodedOutput(
            text="program",
            op_map=MachineCodeOpMap(),
            warnings=value,
        )


def test_op_map_converts_to_raygeo_spans_and_owners():
    op_map = MachineCodeOpMap(
        op_to_machine_code={0: [0, 1], 1: [], 2: [3]},
        machine_code_to_op={0: 0, 1: 0, 3: 2},
    )

    assert op_map.to_line_spans() == [(0, 2), (0, 0), (3, 1)]
    assert op_map.to_line_owners() == [0, 0, -1, 2]


def test_op_map_decodes_raygeo_packed_buffers():
    op_ranges = bytearray(
        b"\x00\x00\x00\x00\x02\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x03\x00\x00\x00\x01\x00\x00\x00"
    )
    line_owners = bytearray(
        b"\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\xff\xff\x02\x00\x00\x00"
    )

    op_map = MachineCodeOpMap.from_raygeo(op_ranges, line_owners)

    assert op_map.op_to_machine_code == {0: [0, 1], 1: [], 2: [3]}
    assert op_map.machine_code_to_op == {0: 0, 1: 0, 3: 2}


def test_op_map_rejects_noncontiguous_lines():
    op_map = MachineCodeOpMap(op_to_machine_code={0: [0, 2]})

    with pytest.raises(ValueError, match="must be contiguous"):
        op_map.to_line_spans()
