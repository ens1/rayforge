"""Sync pip requirement files with the pins in pixi.toml.

App and Debian requirements retain exact Pixi pins, including Git
revisions. Public distribution metadata uses registry-compatible versions
declared in ``tool.rayforge.public-dependency-overrides``. Entries without
a pixi.toml counterpart are left as-is.
"""

import re
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent
PIXI_TOML = ROOT / "pixi.toml"
PYPROJECT_TOML = ROOT / "pyproject.toml"
APP_REQUIREMENTS = ROOT / "requirements.txt"
PUBLIC_REQUIREMENTS = ROOT / "requirements-pypi.txt"
BUNDLE_REQUIREMENTS = ROOT / "debian" / "requirements-bundle.txt"
REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(.*)$")


def normalize(name: str) -> str:
    return name.lower().replace("_", "-")


def _is_direct_reference(spec: str) -> bool:
    return spec.lstrip().startswith("@")


def load_specs(public: bool = False) -> dict[str, str]:
    with PIXI_TOML.open("rb") as handle:
        manifest = tomllib.load(handle)
    specs: dict[str, str] = {}
    features = manifest.get("feature", {})
    sections = [
        manifest.get("pypi-options", {}).get("dependency-overrides", {}),
        features.get("app", {}).get("pypi-dependencies", {}),
        features.get("app", {}).get("dependencies", {}),
        manifest.get("dependencies", {}),
    ]
    for section in sections:
        for name, spec in section.items():
            if isinstance(spec, str):
                specs.setdefault(normalize(name), spec)
            elif isinstance(spec, dict) and "git" in spec:
                ref = spec.get("rev") or spec.get("tag") or spec.get("branch")
                if ref:
                    requirement = f" @ git+{spec['git']}@{ref}"
                    specs.setdefault(normalize(name), requirement)
    if not public:
        return specs

    with PYPROJECT_TOML.open("rb") as handle:
        project_manifest = tomllib.load(handle)
    overrides = (
        project_manifest.get("tool", {})
        .get("rayforge", {})
        .get("public-dependency-overrides", {})
    )
    for name, spec in overrides.items():
        if not isinstance(spec, str) or _is_direct_reference(spec):
            raise ValueError(
                f"Invalid public dependency override for {name}: {spec!r}"
            )
        specs[normalize(name)] = spec

    unresolved = sorted(
        name for name, spec in specs.items() if _is_direct_reference(spec)
    )
    if unresolved:
        names = ", ".join(unresolved)
        raise ValueError(f"Missing public dependency overrides: {names}")
    return specs


def sync_file(path: Path, specs: dict[str, str], exact: bool) -> None:
    text = path.read_text()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = REQUIREMENT.match(line.strip())
        if not match:
            continue
        name = match.group(1)
        spec = specs.get(normalize(name))
        if spec is None or (
            exact
            and not spec.startswith("==")
            and not spec.startswith(" @ git+")
        ):
            continue
        marker_match = re.search(r"\s*;\s*(.+)$", line.strip())
        marker = f" ; {marker_match.group(1)}" if marker_match else ""
        requirement = f"{name}{spec}" if spec != "*" else name
        new_line = f"{requirement}{marker}"
        if new_line != line.strip():
            indent = line[: len(line) - len(line.lstrip())]
            lines[index] = f"{indent}{new_line}"
    content = "\n".join(lines)
    if text.endswith("\n"):
        content += "\n"
    path.write_text(content)


def validate_public_requirements(
    path: Path, required_specs: dict[str, str] | None = None
) -> None:
    direct_references = []
    found_specs: dict[str, str] = {}
    for line in path.read_text().splitlines():
        match = REQUIREMENT.match(line.strip())
        if not match:
            continue
        spec = match.group(2).split(";", maxsplit=1)[0]
        found_specs[normalize(match.group(1))] = spec.strip()
        if _is_direct_reference(spec):
            direct_references.append(match.group(1))
    if direct_references:
        names = ", ".join(sorted(direct_references))
        raise ValueError(
            f"Public requirements contain direct references: {names}"
        )
    if required_specs is None:
        return
    missing = sorted(set(required_specs) - set(found_specs))
    if missing:
        names = ", ".join(missing)
        raise ValueError(f"Public requirements omit dependencies: {names}")
    mismatched = sorted(
        name
        for name, spec in required_specs.items()
        if found_specs[name] != spec.strip()
    )
    if mismatched:
        names = ", ".join(mismatched)
        raise ValueError(
            f"Public requirements have stale dependency specs: {names}"
        )


def main() -> int:
    specs = load_specs()
    public_specs = load_specs(public=True)
    sync_file(APP_REQUIREMENTS, specs, exact=False)
    sync_file(BUNDLE_REQUIREMENTS, specs, exact=True)
    sync_file(PUBLIC_REQUIREMENTS, public_specs, exact=False)
    required_public_specs = {
        name: public_specs[name]
        for name, spec in specs.items()
        if _is_direct_reference(spec)
    }
    validate_public_requirements(PUBLIC_REQUIREMENTS, required_public_specs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
