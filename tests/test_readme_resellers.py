"""The README's supported-reseller table must match the reseller table."""

from pathlib import Path

from custom_components.smartbox.smartbox_protocol.reseller import AvailableResellers

README = Path(__file__).resolve().parents[1] / "README.md"
HEADING = "## Supported resellers"


def _table_rows() -> list[tuple[str, str]]:
    """Return ``(brand, link)`` rows from the README section, in order."""
    lines = README.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(HEADING)
    except ValueError as ex:
        msg = f"README is missing the {HEADING!r} section"
        raise AssertionError(msg) from ex

    rows: list[tuple[str, str]] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in stripped.strip("|").split("|")]
        if len(cells) != 3:
            msg = f"README reseller row must have 3 cells: {line!r}"
            raise AssertionError(msg)
        if cells[0] == "Brand" or all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        rows.append((cells[0], cells[1]))
    return rows


def test_readme_reseller_table_matches_available_resellers() -> None:
    """Every configured reseller appears once, in order, with its link."""
    expected = [
        (reseller.name, reseller.web_url)
        for reseller in AvailableResellers.resellers.values()
    ]
    assert _table_rows() == expected
