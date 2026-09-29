"""Test that translation files stay in sync with strings.json."""

import json
from pathlib import Path

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "smartbox"
# strings.json uses [%key:common::...] references into HA core common strings,
# which translation files inline instead — so parity is checked on key paths,
# not on values.
TRANSLATION_LOCALES = ("de", "en", "es", "fr")


def _load(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _key_paths(obj: object, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}::{key}" if prefix else key
            keys.add(path)
            keys |= _key_paths(value, path)
    return keys


def test_translations_cover_strings_json() -> None:
    """Every locale file must provide a value for every strings.json key."""
    strings_keys = _key_paths(_load(COMPONENT_DIR / "strings.json"))
    for locale in TRANSLATION_LOCALES:
        translation_keys = _key_paths(
            _load(COMPONENT_DIR / "translations" / f"{locale}.json")
        )
        missing = strings_keys - translation_keys
        assert not missing, f"{locale}.json is missing keys: {sorted(missing)}"


def test_translations_are_fully_inlined() -> None:
    """Translation files must not contain unresolved [%key:] references."""
    for locale in TRANSLATION_LOCALES:
        text = (COMPONENT_DIR / "translations" / f"{locale}.json").read_text(
            encoding="utf-8"
        )
        assert "[%key:" not in text, f"{locale}.json contains a [%key:] reference"
