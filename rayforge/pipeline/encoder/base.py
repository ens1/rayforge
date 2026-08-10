from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from struct import iter_unpack
from typing import Any

from raygeo.ops import Ops


@dataclass
class MachineCodeOpMap:
    """
    A container for a bidirectional mapping between Ops command indices and
    Machine language (e.g. G-code) line numbers.

    Attributes:
        op_to_machine_code: Maps an Ops command index to a list of G-code line
                     numbers it generated. An empty list means the command
                     produced no G-code.
        machine_code_to_op: Maps a G-code line number back to the Ops command
                     index that generated it.
    """

    op_to_machine_code: dict[int, list[int]] = field(default_factory=dict)
    machine_code_to_op: dict[int, int] = field(default_factory=dict)

    def to_line_spans(self) -> list[tuple[int, int]]:
        """Return Raygeo's contiguous ``(start, count)`` op spans."""
        if not self.op_to_machine_code:
            return []
        last_op = max(self.op_to_machine_code)
        return [
            _line_span(self.op_to_machine_code.get(op_index, []))
            for op_index in range(last_op + 1)
        ]

    def to_line_owners(self) -> list[int]:
        """Return Raygeo's dense line-to-op array with ``-1`` gaps."""
        if not self.machine_code_to_op:
            return []
        last_line = max(self.machine_code_to_op)
        if last_line < 0:
            raise ValueError("machine-code line indices must be nonnegative")
        owners = [-1] * (last_line + 1)
        for line_index, op_index in self.machine_code_to_op.items():
            if line_index < 0 or op_index < 0:
                raise ValueError("op-map indices must be nonnegative")
            owners[line_index] = op_index
        return owners

    @classmethod
    def from_raygeo(
        cls,
        op_to_machine_code: Any,
        machine_code_to_op: Any,
    ) -> "MachineCodeOpMap":
        """Decode either mapping or packed-buffer Raygeo op maps."""
        return cls(
            op_to_machine_code=_decode_op_lines(op_to_machine_code),
            machine_code_to_op=_decode_line_owners(machine_code_to_op),
        )


def _line_span(lines: list[int]) -> tuple[int, int]:
    if not lines:
        return (0, 0)
    start = lines[0]
    if start < 0 or any(
        line != start + offset for offset, line in enumerate(lines)
    ):
        raise ValueError("machine-code lines for an op must be contiguous")
    return (start, len(lines))


def _decode_op_lines(value: Any) -> dict[int, list[int]]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {int(key): list(lines) for key, lines in value.items()}
    raw = bytes(value)
    if len(raw) % 8:
        raise ValueError("packed op-to-machine-code map is malformed")
    return {
        op_index: list(range(start, start + count))
        for op_index, (start, count) in enumerate(iter_unpack("=II", raw))
    }


def _decode_line_owners(value: Any) -> dict[int, int]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {int(key): int(owner) for key, owner in value.items()}
    raw = bytes(value)
    if len(raw) % 4:
        raise ValueError("packed machine-code-to-op map is malformed")
    owners = (owner for (owner,) in iter_unpack("=i", raw))
    return {
        line_index: owner
        for line_index, owner in enumerate(owners)
        if owner >= 0
    }


@dataclass
class EncodedOutput:
    """
    Base class for encoder output.

    Attributes:
        text: Human-readable machine code representation for UI display.
        op_map: Bidirectional mapping between ops indices and line numbers.
        driver_data: Optional driver-specific metadata. The legacy ``binary``
            entry is synchronized with ``payload``.
        payload: Optional opaque machine-program bytes for binary protocols.
        warnings: Non-fatal, user-facing encoder warnings.
    """

    text: str
    op_map: MachineCodeOpMap
    driver_data: dict[str, Any] = field(default_factory=dict)
    payload: bytes | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        legacy_payload = self.driver_data.get("binary")
        if self.payload is not None and not isinstance(self.payload, bytes):
            raise TypeError("payload must be bytes or None")
        if legacy_payload is not None and not isinstance(
            legacy_payload, bytes
        ):
            raise TypeError("driver_data['binary'] must be bytes or None")
        if (
            self.payload is not None
            and legacy_payload is not None
            and self.payload != legacy_payload
        ):
            raise ValueError("payload and driver_data['binary'] disagree")
        if self.payload is None:
            self.payload = legacy_payload
        elif legacy_payload is None:
            self.driver_data["binary"] = self.payload
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(warning, str) and warning for warning in self.warnings
        ):
            raise TypeError("warnings must be a tuple of nonempty strings")


class OpsEncoder(ABC):
    """
    Transforms an Ops object into something else.
    Examples:

    - Ops to image (a cairo surface)
    - Ops to a G-code string
    """

    @abstractmethod
    def encode(self, ops: Ops, *args, **kwargs) -> Any:
        pass
