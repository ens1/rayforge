from pathlib import Path

MAC_BUILD_SCRIPT = Path("scripts/mac/mac_build.sh")


def test_frozen_app_uses_only_bundled_vips_modules():
    script = MAC_BUILD_SCRIPT.read_text()

    assert 'export VIPSHOME="$APP_DIR/Resources/vips"' in script
    assert 'mkdir -p "$RES_DIR/vips/lib"' in script
