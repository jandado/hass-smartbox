"""Test that translation files stay in sync with strings.json."""

import json
from pathlib import Path
import re

import yaml

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "smartbox"
# strings.json uses [%key:common::...] references into HA core common strings,
# which translation files inline instead — so parity is checked on key paths,
# not on values.
TRANSLATION_LOCALES = ("de", "en", "es", "fr")

# English user-facing text with a sentence period glued to the next word
# ("...stable again.If the endpoint..." is the typo this catches). The
# two-word minimum before the period keeps the usual abbreviations out (e.g.
# "e.g. The" and "i.e. Use" have a single letter before the dot), and the check
# runs on the English files only: French spacing and German capitalisation would
# make it meaningless elsewhere.
_MISSING_SPACE = re.compile(r"\w{2,}\.[A-Z][a-z]")

# The Home Assistant frontend compiles every translated string as an ICU
# message, so braces are reserved for placeholders. A literal brace (a JSON
# example inside a description, say) fails that compilation, and the frontend
# renders "Translation error: MALFORMED_ARGUMENT" instead of the string.
_ICU_PLACEHOLDER = re.compile(r"\{[A-Za-z0-9_]+\}")


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


def test_translation_placeholders_match_strings_json() -> None:
    """A locale must not add, drop or rename an ICU placeholder."""
    strings = dict(_string_values(_load(COMPONENT_DIR / "strings.json")))
    for locale in TRANSLATION_LOCALES:
        translated = dict(
            _string_values(_load(COMPONENT_DIR / "translations" / f"{locale}.json"))
        )
        mismatched = [
            key_path
            for key_path, source in strings.items()
            if set(_ICU_PLACEHOLDER.findall(source))
            != set(_ICU_PLACEHOLDER.findall(translated[key_path]))
        ]
        assert not mismatched, f"{locale}.json has placeholder drift at: {mismatched}"


def test_translation_values_are_non_empty() -> None:
    """A present-but-empty translation is still a broken UI string."""
    sources: list[tuple[str, object]] = [
        ("strings.json", _load(COMPONENT_DIR / "strings.json")),
    ]
    sources.extend(
        (
            f"translations/{locale}.json",
            _load(COMPONENT_DIR / "translations" / f"{locale}.json"),
        )
        for locale in TRANSLATION_LOCALES
    )
    empty = [
        f"{name}:{key_path}"
        for name, data in sources
        for key_path, value in _string_values(data)
        if not value.strip()
    ]
    assert not empty, f"empty translation values: {empty}"


def test_english_text_has_no_glued_sentences() -> None:
    """No English sentence runs into the next one without a space (L7).

    Applies to the English sources: ``strings.json`` (the source of truth) and
    ``translations/en.json``. It is a heuristic, deliberately narrow: it only
    looks for a word of two or more characters followed by a period and a
    capitalised word.
    """
    for path in (
        COMPONENT_DIR / "strings.json",
        COMPONENT_DIR / "translations" / "en.json",
    ):
        text = path.read_text(encoding="utf-8")
        glued = [match.group(0) for match in _MISSING_SPACE.finditer(text)]
        assert not glued, f"{path.name} has sentences glued together: {glued}"


def _string_values(obj: object, prefix: str = "") -> list[tuple[str, str]]:
    """Return (key path, value) for every string in a nested object."""
    found: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}::{key}" if prefix else key
            found.extend(_string_values(value, path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found.extend(_string_values(value, f"{prefix}[{index}]"))
    elif isinstance(obj, str):
        found.append((prefix, obj))
    return found


def test_user_facing_strings_are_icu_safe() -> None:
    """Braces in user-facing strings must be well-formed ICU placeholders."""
    sources: list[tuple[str, object]] = [
        ("strings.json", _load(COMPONENT_DIR / "strings.json")),
    ]
    sources.extend(
        (
            f"translations/{locale}.json",
            _load(COMPONENT_DIR / "translations" / f"{locale}.json"),
        )
        for locale in TRANSLATION_LOCALES
    )
    service_specs = yaml.safe_load(
        (COMPONENT_DIR / "services.yaml").read_text(encoding="utf-8")
    )
    sources.extend(
        (f"services.yaml::{service}", spec) for service, spec in service_specs.items()
    )

    offenders: list[str] = []
    for name, data in sources:
        for key_path, value in _string_values(data):
            stripped = _ICU_PLACEHOLDER.sub("", value)
            if "{" in stripped or "}" in stripped:
                offenders.append(f"{name}:{key_path}: {value!r}")
    assert not offenders, f"strings with non-placeholder braces: {offenders}"
