from pathlib import Path

import pytest

from scripts.pixi_raygeo_override import MARKER, apply_local_override


def test_apply_local_override_only_replaces_override_table(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "pixi.toml"
    checkout = tmp_path / "local raygeo"
    checkout.mkdir()
    manifest.write_text(
        """
[feature.app.pypi-dependencies]
raygeo = "*"

[pypi-options.dependency-overrides]
raygeo = { git = "https://example.com/raygeo.git", rev = "abc123" }
ruida-re = { git = "https://example.com/ruida-re.git", rev = "def456" }

[tasks]
test = "pytest"
""".lstrip()
    )

    apply_local_override(manifest, checkout)

    result = manifest.read_text()
    assert result.count(MARKER) == 1
    assert '[feature.app.pypi-dependencies]\nraygeo = "*"' in result
    assert (
        f"raygeo = {{ path = {json_path(checkout)}, editable = true }}"
        in result
    )
    assert "ruida-re = { git =" in result


def test_apply_local_override_rejects_an_active_override(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "pixi.toml"
    manifest.write_text(f"{MARKER}\n")

    with pytest.raises(ValueError, match="already exists"):
        apply_local_override(manifest, tmp_path)


def json_path(path: Path) -> str:
    import json

    return json.dumps(str(path.resolve()))
