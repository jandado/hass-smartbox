"""Import-direction enforcement.

The three-layer split is kept by an import-direction rule - the
architecture you keep is the one the linter enforces. This test is that
rule:

- protocol layer: HA-free, core-free, adapter-free;
- core layer: HA-free, adapter-free (protocol imports allowed);
- (the HA adapter may import both - no rule here).

Relative imports are RESOLVED rather than skipped. Inside the integration
package ``from ..adapter import X`` is both the natural way to write the
violation this rule exists to prevent and the import style the vendored
layers actually use (ruff TID252 is ignored for them), so a scan that only
looked at absolute imports would enforce nothing for the direction that can
be broken by hand.
"""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
import textwrap

REPO_ROOT = Path(__file__).resolve().parents[1]
LAYERS_ROOT = REPO_ROOT / "custom_components" / "smartbox"

# The integration package the layers live in: a resolved import inside it
# names one of these subpackages/modules as its third component.
_INTEGRATION_PACKAGE = ("custom_components", "smartbox")
_INTEGRATION_ROOT = "<the integration package root>"
# Reported rather than skipped: nothing in this repo climbs out of the
# integration package, and a scan that cannot resolve an import must not
# silently pass it.
_ESCAPES_INTEGRATION = "<climbs out of the integration package>"

# layer package -> (forbidden top-level names,
#                   integration-internal targets the layer may import,
#                   rule label)
_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "smartbox_protocol": (
        ("homeassistant",),
        (),
        "protocol layer is HA-free and must not depend on core/adapter",
    ),
    "smartbox_core": (
        ("homeassistant",),
        ("smartbox_protocol",),
        "core layer is HA-free and must not depend on the adapter",
    ),
}


def _resolved_modules(path: Path) -> list[str]:
    """Return every imported module in ``path`` as an absolute dotted name.

    A relative import resolves against the file's own package: with the file
    in ``custom_components/smartbox/<layer>/``, ``from ..adapter import X``
    becomes ``custom_components.smartbox.adapter`` (the file's package minus
    ``level - 1`` components, plus the named module).
    """
    package = list(path.relative_to(REPO_ROOT).with_suffix("").parts[:-1])
    modules: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    modules.append(node.module)
            elif node.level > len(package):
                modules.append(_ESCAPES_INTEGRATION)
            else:
                base = package[: len(package) - (node.level - 1)]
                modules.append(
                    ".".join([*base, *node.module.split(".")] if node.module else base)
                )
    return modules


def _integration_target(module: str) -> str | None:
    """Return the integration subpackage/module an import names, or ``None``.

    ``custom_components.smartbox.adapter`` -> ``"adapter"``;
    ``custom_components.smartbox`` -> the package root (adapter-side);
    ``homeassistant.helpers.entity`` -> ``None`` (not integration-internal).
    """
    parts = module.split(".")
    if tuple(parts[:2]) != _INTEGRATION_PACKAGE:
        return None
    if len(parts) == 2:
        return _INTEGRATION_ROOT
    return parts[2]


def _violations(path: Path, allowed: set[str], forbidden_roots: set[str]) -> list[str]:
    """Return the imports in ``path`` that break the layer's rule."""
    bad: list[str] = []
    for module in _resolved_modules(path):
        if module == _ESCAPES_INTEGRATION or module.split(".")[0] in forbidden_roots:
            bad.append(module)
            continue
        target = _integration_target(module)
        if target is not None and target not in allowed:
            bad.append(module)
    return bad


def test_layer_import_direction() -> None:
    for layer, (forbidden_roots, allowed_siblings, why) in _RULES.items():
        sources = sorted((LAYERS_ROOT / layer).rglob("*.py"))
        assert sources, f"{layer} package missing from the vendored layers"
        allowed = {layer, *allowed_siblings}
        for src in sources:
            bad = _violations(src, allowed, set(forbidden_roots))
            assert not bad, f"{src.relative_to(LAYERS_ROOT)} imports {bad}: {why}"


# Loads the two layer packages by path under synthetic top-level names, so the
# adapter's __init__ (which does import homeassistant) never runs. ``..x``
# then resolves to the synthetic top level, matching the real layout.
_RUNTIME_PROBE = textwrap.dedent(
    """
    import importlib, pathlib, sys, types
    root = pathlib.Path("custom_components/smartbox")
    # A synthetic parent package over the layer directories, so the layers'
    # ``..`` imports resolve exactly as they do inside the integration.
    parent = types.ModuleType("layers")
    parent.__path__ = [str(root)]
    sys.modules["layers"] = parent
    for package in ("smartbox_protocol", "smartbox_core"):
        importlib.import_module(f"layers.{package}")
        # Every module, not just the package __init__: a string-built import
        # inside a submodule must show up too.
        for source in sorted((root / package).glob("*.py")):
            if source.name != "__init__.py":
                importlib.import_module(f"layers.{package}.{source.stem}")
    print("homeassistant" in sys.modules)
    """
)


def test_lower_layers_import_no_homeassistant_at_runtime() -> None:
    """The AST scan cannot see a dynamic import; a fresh interpreter can.

    ``importlib.import_module("homeassistant...")`` or a string-built import
    would slip past the static scan. Importing every module of both layers for
    real (the adapter's own ``__init__`` excluded) and checking ``sys.modules``
    closes that hole. One subprocess for both layers: the interpreter start is
    the cost, not the imports.
    """
    result = subprocess.run(  # noqa: S603 - literal argv, fixed interpreter
        [sys.executable, "-c", _RUNTIME_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", (
        f"importing the lower layers pulled in homeassistant:\n{result.stderr}"
    )
