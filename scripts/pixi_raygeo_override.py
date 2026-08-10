"""Temporarily replace Pixi's exact Raygeo override with a local checkout."""

import json
import sys
from pathlib import Path

MARKER = "# pixi-raygeo: temporary override (auto-removed)"
SECTION = "[pypi-options.dependency-overrides]"


def apply_local_override(manifest_path: Path, checkout: Path) -> None:
    text = manifest_path.read_text()
    if MARKER in text:
        raise ValueError("temporary Raygeo override marker already exists")
    lines = text.splitlines(keepends=True)
    section_index = next(
        (index for index, line in enumerate(lines) if line.strip() == SECTION),
        None,
    )
    if section_index is None:
        raise ValueError(f"{SECTION} is missing")

    raygeo_index = None
    for index in range(section_index + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("["):
            break
        if stripped.startswith("raygeo ="):
            raygeo_index = index
            break
    if raygeo_index is None:
        raise ValueError(f"Raygeo override is missing from {SECTION}")

    quoted_path = json.dumps(str(checkout.resolve()))
    newline = "\n" if lines[raygeo_index].endswith("\n") else ""
    lines[raygeo_index] = (
        f"raygeo = {{ path = {quoted_path}, editable = true }}{newline}"
    )
    lines.insert(section_index, f"{MARKER}\n")
    manifest_path.write_text("".join(lines))


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: pixi_raygeo_override.py PIXI_TOML RAYGEO_CHECKOUT",
            file=sys.stderr,
        )
        return 2
    apply_local_override(Path(sys.argv[1]), Path(sys.argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
