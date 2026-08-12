from pathlib import Path

MAC_BUILD_SCRIPT = Path("scripts/mac/mac_build.sh")


def test_frozen_app_uses_only_bundled_vips_modules():
    script = MAC_BUILD_SCRIPT.read_text()

    assert 'export VIPSHOME="$APP_DIR/Resources/vips"' in script
    assert 'mkdir -p "$RES_DIR/vips/lib"' in script


def test_frozen_app_excludes_local_python_caches_and_addon_tests():
    script = MAC_BUILD_SCRIPT.read_text()

    assert (
        'find "$RES_DIR/rayforge/builtin_addons" -type d -name tests '
        "-prune" in script
    )
    assert 'find "$APP_ROOT" -type d -name __pycache__ -prune' in script
    assert "-name '*.pyc' -o -name '*.pyo'" in script


def test_frozen_app_force_installs_exact_git_revisions():
    script = MAC_BUILD_SCRIPT.read_text()

    assert "git\\+" in script
    assert "--force-reinstall" in script
    assert '--no-deps "$direct_requirement"' in script
