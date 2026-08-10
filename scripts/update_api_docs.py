"""Download and sync API docs from the pinned Raygeo source archive."""

import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO_URL = "https://github.com/barebaric/raygeo"
REQUIREMENTS = Path("requirements.txt")
OUTPUT_DIR = Path("website/docs/developer/raygeo-api")


def _get_raygeo_source() -> tuple[str, str, str]:
    text = REQUIREMENTS.read_text()
    vcs = re.search(
        r"^raygeo\s*@\s*git\+(https://github\.com/[^\s@]+?)(?:\.git)?"
        r"@([^\s;]+)",
        text,
        re.MULTILINE,
    )
    if vcs:
        repository, revision = vcs.groups()
        return repository, revision, revision[:12]
    match = re.search(r"^raygeo==([\d.]+)", text, re.MULTILINE)
    if match:
        version = match.group(1)
        return REPO_URL, f"v{version}", f"v{version}"
    print(
        f"Could not find a pinned Raygeo source in {REQUIREMENTS}.",
        file=sys.stderr,
    )
    sys.exit(1)


def _newest_mtime(files: list[Path]) -> float:
    return max((f.stat().st_mtime for f in files if f.exists()), default=0)


def _find_files(directory: Path, pattern: str) -> list[Path]:
    return sorted(directory.rglob(pattern)) if directory.exists() else []


def _needs_update(src_dir: Path, out_dir: Path) -> bool:
    src_files = _find_files(src_dir, "*.*")
    out_files = _find_files(out_dir, "*.*")
    if not out_files:
        return True
    return _newest_mtime(src_files) > _newest_mtime(out_files)


def _sync_dir(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)

    src_files = set(_find_files(src, "*.*"))
    existing_dst_files = set(_find_files(dst, "*.*"))

    for src_path in src_files:
        rel = src_path.relative_to(src)
        dst_path = dst / rel
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        if (
            not dst_path.exists()
            or src_path.stat().st_mtime > dst_path.stat().st_mtime
        ):
            shutil.copy2(src_path, dst_path)

    for dst_path in existing_dst_files:
        rel = dst_path.relative_to(dst)
        if rel not in {p.relative_to(src) for p in src_files} and (
            dst_path.is_file()
        ):
            dst_path.unlink()


def main() -> int:
    repository, revision, label = _get_raygeo_source()
    tar_url = f"{repository}/archive/{revision}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / "raygeo.tar.gz"
        print(f"Downloading Raygeo {label} source...")
        urllib.request.urlretrieve(tar_url, archive_path)

        print("Extracting docs/api from archive...")
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=tmp, filter="data")

        candidates = list(Path(tmp).glob("*/docs/api"))
        docs_src = candidates[0] if len(candidates) == 1 else None

        if docs_src is None or not docs_src.exists():
            print(
                f"docs/api/ not found in the Raygeo {label} archive.",
                file=sys.stderr,
            )
            return 1

        if not _needs_update(docs_src, OUTPUT_DIR):
            print("API docs are up to date.")
            return 0

        print("Syncing raygeo API docs...")
        _sync_dir(docs_src, OUTPUT_DIR)

    return 0


if __name__ == "__main__":
    sys.exit(main())
