"""The areas claims are made for: the registered band map, and exploratory views (unfrozen, KTD9).

The registered map, ``bands/band-map.csv``, holds the verdict bands. A view is
one more area that claims are issued and scored for, by the same rules and the
same ``bands.band_mean``, to be read beside the verdict bands and never as one
of them. Each view is its own write-once file, ``bands/view-<name>.csv``, in
the band map's format: its cells, their weights summing to 1, and the one band
the file is named after.

A view's band name starts with ``view-`` and a registered band's never does,
so the name alone says which a claim or score belongs to. The drivers give a
view's entries the kind ``exploratory`` whatever the bundle, the ledger refuses
them under any other kind, and ``registered`` drops them from whatever feeds a
verdict.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from khetru_evidence import bands
from khetru_evidence.ledger import VIEW_PREFIX

VIEWS_GLOB = f"bands/{VIEW_PREFIX}*.csv"
EXPLORATORY = "exploratory"


@dataclass(frozen=True)
class Area:
    """A band file and the map it holds."""

    path: Path
    band_map: bands.BandMap


def is_view(band: str) -> bool:
    return band.startswith(VIEW_PREFIX)


def kind(band: str, registered_kind: str) -> str:
    """The kind of an entry for ``band``: a view's is always exploratory."""
    return EXPLORATORY if is_view(band) else registered_kind


def registered(entries: Iterable[Mapping]) -> list:
    """The claims or scores of registered bands only: what a verdict or a skill figure may read."""
    return [e for e in entries if not is_view(e["band"])]


def areas(ledger_root: Path) -> list[Area]:
    """The registered map, then every view by file name."""
    root = Path(ledger_root)
    main = Area(root / bands.BAND_MAP_PATH, bands.decode((root / bands.BAND_MAP_PATH).read_bytes()))
    for band in main.band_map.verdict_bands:
        if is_view(band):
            raise bands.BandMapError(f"registered band {band!r} starts with {VIEW_PREFIX!r}, which names a view")
    found = [main]
    for path in sorted(root.glob(VIEWS_GLOB)):
        view = Area(path, bands.decode(path.read_bytes()))
        if view.band_map.verdict_bands != (path.stem,) or any(not c.verdict_band for c in view.band_map.cells):
            raise bands.BandMapError(f"{path.name} must hold the one band {path.stem}, on every row")
        found.append(view)
    return found


def by_band(ledger_root: Path) -> dict[str, Area]:
    """Every band claims are made for, registered bands first, with the area that holds it."""
    return {band: area for area in areas(ledger_root) for band in area.band_map.verdict_bands}
