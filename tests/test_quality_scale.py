"""Tests for the integration quality-scale audit."""

import json
from pathlib import Path

import yaml

INTEGRATION = Path(__file__).resolve().parents[1] / "custom_components" / "smartbox"
STATUSES = {"done", "todo", "exempt"}


def _status(rule: object) -> str:
    if isinstance(rule, str):
        return rule
    assert isinstance(rule, dict)
    return rule["status"]


def test_quality_scale_audit_is_valid() -> None:
    """Every rule has a known status and every gap records its reason."""
    rules = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    assert rules
    for name, rule in rules.items():
        status = _status(rule)
        assert status in STATUSES, name
        if status != "done":
            assert isinstance(rule, dict), name
            assert rule.get("comment"), f"{name} needs a reason"


def test_manifest_stays_custom_while_gaps_remain() -> None:
    """Do not claim a quality tier before every rule in it is done."""
    rules = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text())["rules"]
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    if any(_status(rule) == "todo" for rule in rules.values()):
        assert manifest["quality_scale"] == "custom"
