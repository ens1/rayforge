from pathlib import Path

import pytest

from scripts import sync_requirements


def _write_manifest(path: Path, include_ruida_override: bool = True) -> None:
    ruida_override = 'ruida-re = "==0.1.0"' if include_ruida_override else ""
    path.write_text(
        f"""
[feature.app.pypi-dependencies]
raygeo = "*"
ruida-re = "*"
aiohttp = "==3.14.3"

[pypi-options.dependency-overrides]
raygeo = {{ git = "https://example.com/raygeo.git", rev = "abc123" }}
ruida-re = {{ git = "https://example.com/ruida-re.git", rev = "def456" }}

[tool.rayforge.public-dependency-overrides]
raygeo = "==1.37.0"
{ruida_override}
""".lstrip()
    )


def test_main_keeps_app_pins_and_public_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "pixi.toml"
    app = tmp_path / "requirements.txt"
    public = tmp_path / "requirements-pypi.txt"
    bundle = tmp_path / "requirements-bundle.txt"
    _write_manifest(manifest)
    app.write_text(
        'raygeo==1.36.1\nruida-re==0.0.1 ; python_version >= "3.11"\n'
        "aiohttp==3.0.0\n"
    )
    public.write_text(
        'raygeo==1.36.1\nruida-re==0.0.1 ; python_version >= "3.11"\n'
        "aiohttp==3.0.0\n"
    )
    bundle.write_text("raygeo==1.36.1\nruida-re==0.0.1\n")
    monkeypatch.setattr(sync_requirements, "PIXI_TOML", manifest)
    monkeypatch.setattr(sync_requirements, "PYPROJECT_TOML", manifest)
    monkeypatch.setattr(sync_requirements, "APP_REQUIREMENTS", app)
    monkeypatch.setattr(sync_requirements, "PUBLIC_REQUIREMENTS", public)
    monkeypatch.setattr(sync_requirements, "BUNDLE_REQUIREMENTS", bundle)

    assert sync_requirements.main() == 0
    first_result = (app.read_text(), public.read_text(), bundle.read_text())
    assert sync_requirements.main() == 0
    assert (app.read_text(), public.read_text(), bundle.read_text()) == (
        first_result
    )

    assert app.read_text() == (
        "raygeo @ git+https://example.com/raygeo.git@abc123\n"
        "ruida-re @ git+https://example.com/ruida-re.git@def456 ; "
        'python_version >= "3.11"\n'
        "aiohttp==3.14.3\n"
    )
    assert public.read_text() == (
        "raygeo==1.37.0\n"
        'ruida-re==0.1.0 ; python_version >= "3.11"\n'
        "aiohttp==3.14.3\n"
    )
    assert bundle.read_text() == (
        "raygeo @ git+https://example.com/raygeo.git@abc123\n"
        "ruida-re @ git+https://example.com/ruida-re.git@def456\n"
    )


def test_public_specs_require_an_override_for_every_direct_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "pixi.toml"
    _write_manifest(manifest, include_ruida_override=False)
    monkeypatch.setattr(sync_requirements, "PIXI_TOML", manifest)
    monkeypatch.setattr(sync_requirements, "PYPROJECT_TOML", manifest)

    with pytest.raises(
        ValueError, match="Missing public dependency overrides: ruida-re"
    ):
        sync_requirements.load_specs(public=True)


def test_public_requirements_reject_direct_references(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements-pypi.txt"
    requirements.write_text(
        "safe==1.0\nunsafe @ git+https://example.com/unsafe.git@abc123\n"
    )

    with pytest.raises(
        ValueError,
        match="Public requirements contain direct references: unsafe",
    ):
        sync_requirements.validate_public_requirements(requirements)


def test_public_requirements_require_every_override(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements-pypi.txt"
    requirements.write_text("raygeo==1.37.0\n")

    with pytest.raises(
        ValueError, match="Public requirements omit dependencies: ruida-re"
    ):
        sync_requirements.validate_public_requirements(
            requirements,
            {"raygeo": "==1.37.0", "ruida-re": "==0.1.0"},
        )
