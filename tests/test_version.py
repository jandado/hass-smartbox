import json
from pathlib import Path
import re

import custom_components.smartbox

MANIFEST = Path(custom_components.smartbox.__file__).parent / "manifest.json"


def test_version():
    assert re.match(
        r"^[0-9\.]+(-[a-z0-9\.]+)?$", custom_components.smartbox.__version__
    )


def test_manifest_version_matches_module():
    """The manifest and the module must carry the same version (HA reads the manifest)."""
    manifest_version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    assert manifest_version == custom_components.smartbox.__version__
