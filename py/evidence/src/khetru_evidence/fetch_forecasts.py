"""Network retrieval of ECMWF ENS precipitation (unfrozen, KTD9).

Two adapters produce the same normalised record (``forecasts``):

- **Open data** (live, rabi 2026 onward) via ``ecmwf-opendata``. Since IFS cycle
  50r1 (12 May 2026) the ENS control is published as ``stream=oper, type=fc``
  and ``stream=enfo`` carries only the 50 perturbed members (``type=pf``). Both
  run to 360 h at 00 and 12 UTC. Each step is fetched (tp only), hashed, decoded,
  cropped and deleted, so the raw GRIB under ``.cache/evidence/`` never piles up.
  ``raw_sha256`` covers the downloaded bytes in fetch order: per step ascending,
  ``oper/fc`` then ``enfo/pf``.
- **TIGGE** (hindcast) via ``cdsapi`` against the ECMWF Data Store (ECDS). The
  TIGGE WebAPI closed on 27 May 2026. Credentials live in ``~/.cdsapirc`` and are
  never committed::

      url: https://ecds.ecmwf.int/api
      key: <personal access token from ecds.ecmwf.int>

  Request (MARS-like syntax, dataset ``tigge-forecasts``): ``class=ti``,
  ``expver=prod``, ``origin=ecmf``, ``levtype=sfc``, ``param=228228`` (tp,
  kg m-2), ``date=YYYY-MM-DD``, ``time=HH:00:00``, ``step=0/6/.../360``,
  ``grid=0.25/0.25`` (interpolated onto the IMD lattice), ``area=N/W/S/E`` (box
  plus margin), and ``type=cf`` or ``type=pf`` with ``number=1/to/50``. The
  ``area`` key and post-50r1 control layout are unconfirmed on ECDS; U1 checks
  them, and the era-decoding tests in ``test_forecasts_crosssource.py`` exercise
  them. ``raw_sha256`` covers the cf file bytes then the pf file bytes.

Both adapters call ``guard`` first: pre-2026 Oct–Nov forecast values are never
fetched before the bundle tag and its hindcast ``started`` run is on origin
(KTD10, KTD11).

GRIB is decoded with ``eccodes`` (binary wheels via ``eccodeslib``).
"""

import hashlib
import json
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import eccodes
import numpy as np

from khetru_evidence import forecasts
from khetru_evidence.forecasts import Field

LEDGER = "mandi-wheat"
OPENDATA_STEPS = forecasts.OPENDATA_STEPS
OPENDATA_PERTURBED = forecasts.ENS_PERTURBED
TIGGE_STEPS = forecasts.TIGGE_STEPS
TIGGE_PERTURBED = forecasts.ENS_PERTURBED
ARCHIVE_BOX = forecasts.MANDI_BOX
TIGGE_DATASET = "tigge-forecasts"
TIGGE_URL = "https://ecds.ecmwf.int/api"
RUN_HOURS = (0, 12)
ARCHIVE_SEASON = ((10, 1), (11, 30))  # inclusive (month, day)

# Pre-2026 Oct–Nov forecasts stay closed until the bundle tag (KTD11).
PROTECTED_MONTHS = (10, 11)
PROTECTED_BEFORE_YEAR = 2026
MAX_STEP_HOURS = 360
BUNDLE_TAG_PATTERN = f"{LEDGER}/bundle-v*"

_CONTROL_TYPES = ("cf", "fc")
_TP_PARAMS = (228, 228228)


class ProtectedDateError(RuntimeError):
    """The request would open pre-2026 Oct–Nov forecast values before pre-registration."""


# --- the shared date guard ---------------------------------------------------


def is_protected(init: datetime) -> bool:
    """Whether any day from ``init`` to its last step falls in a pre-2026 Oct or Nov."""
    day, last = init.date(), (init + timedelta(hours=MAX_STEP_HOURS)).date()
    while day <= last:
        if day.year < PROTECTED_BEFORE_YEAR and day.month in PROTECTED_MONTHS:
            return True
        day += timedelta(days=1)
    return False


def guard(init: datetime, repo: Path) -> None:
    """Refuse a protected run unless a bundle tag and its ``started`` run are on origin.

    Fails closed: any git problem counts as "not unlocked".
    """
    if not is_protected(init):
        return
    if _unlocked(Path(repo)):
        return
    raise ProtectedDateError(
        f"refusing {init:%Y-%m-%dT%HZ}: pre-{PROTECTED_BEFORE_YEAR} Oct–Nov forecasts stay closed "
        f"until a {BUNDLE_TAG_PATTERN} tag and its hindcast started run are on origin (KTD11)"
    )


def _unlocked(repo: Path) -> bool:
    """Whether origin advertises a bundle tag whose started run sits on an origin branch.

    The branch holding the run must also contain the tagged commit.
    """
    try:
        tags = _origin_bundle_tags(repo)
        refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/").split()
    except subprocess.CalledProcessError:
        return False
    for tag, commit in tags.items():
        bundle = tag.removeprefix(f"{LEDGER}/bundle-")
        runs = f"ledger/{LEDGER}/hindcast/{bundle}/runs.jsonl"
        for ref in refs:
            try:
                text = _git(repo, "show", f"{ref}:{runs}")
            except subprocess.CalledProcessError:
                continue
            if _has_started_run(text, bundle) and _is_ancestor(repo, commit, ref):
                return True
    return False


def _origin_bundle_tags(repo: Path) -> dict[str, str]:
    """Bundle tags on origin mapped to the commit each one points at (annotated tags peeled)."""
    listing = _git(repo, "ls-remote", "--tags", "origin", f"refs/tags/{BUNDLE_TAG_PATTERN}")
    tags: dict[str, str] = {}
    for line in listing.splitlines():
        sha, ref = line.split("\t")
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            tags[name.removesuffix("^{}")] = sha  # peeled commit wins over the tag object
        else:
            tags.setdefault(name, sha)
    return tags


def _has_started_run(text: str, bundle: str) -> bool:
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(entry, dict) and entry.get("type") == "run"
                and entry.get("status") == "started" and entry.get("bundle") == bundle):
            return True
    return False


def _is_ancestor(repo: Path, commit: str, ref: str) -> bool:
    try:
        _git(repo, "merge-base", "--is-ancestor", commit, ref)
    except subprocess.CalledProcessError:
        return False
    return True


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


# --- GRIB decoding -----------------------------------------------------------


def decode_grib(path: Path) -> Iterator[Field]:
    """Every tp message in ``path`` as a ``Field`` (full grid; crop before keeping many)."""
    with open(path, "rb") as f:
        while (h := eccodes.codes_grib_new_from_file(f)) is not None:
            try:
                yield _field(h, path)
            finally:
                eccodes.codes_release(h)


def _field(h, path: Path) -> Field:
    get = eccodes.codes_get
    if get(h, "paramId") not in _TP_PARAMS:
        raise forecasts.RecordError(f"{path}: unexpected parameter {get(h, 'shortName')}")
    if get(h, "gridType") != "regular_ll":
        raise forecasts.RecordError(f"{path}: unsupported grid {get(h, 'gridType')}")
    data_type = get(h, "dataType")
    if data_type in _CONTROL_TYPES:
        member = "control"
    elif data_type == "pf":
        member = "perturbed"
    else:
        raise forecasts.RecordError(f"{path}: unexpected data type {data_type}")
    init = datetime.strptime(f"{get(h, 'dataDate'):08d}{get(h, 'dataTime'):04d}", "%Y%m%d%H%M")
    ni, nj = get(h, "Ni"), get(h, "Nj")
    lats, lons = _grid_axes(h, ni, nj)
    values = eccodes.codes_get_values(h).reshape(nj, ni)
    if get(h, "stepUnits") != 1:
        raise forecasts.RecordError(f"{path}: step units are not hours")
    return Field(
        member=member,
        number=0 if member == "control" else int(get(h, "number")),
        init=init.replace(tzinfo=UTC),
        step=int(get(h, "endStep")),
        units=get(h, "units"),
        lats=[float(x) for x in lats],
        lons=[float(x) for x in lons],
        values=np.asarray(values, dtype=float),
        process_id=int(get(h, "generatingProcessIdentifier")),
    )


_GRID_KEYS = (
    "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
    "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees", "jScansPositively",
)
_grid_axes_cache: dict[tuple, tuple] = {}


def _grid_axes(h, ni: int, nj: int):
    """Latitude and longitude axes of a regular grid, decoded once per geometry.

    Per-point coordinates come in scan order, so rows and columns need no flipping.
    """
    key = (ni, nj, *(eccodes.codes_get(h, k) for k in _GRID_KEYS))
    if key not in _grid_axes_cache:
        lats = eccodes.codes_get_array(h, "latitudes").reshape(nj, ni)[:, 0]
        lons = eccodes.codes_get_array(h, "longitudes").reshape(nj, ni)[0, :]
        _grid_axes_cache[key] = (lats, lons)
    return _grid_axes_cache[key]


def _model_cycle(fields: list[Field]) -> str:
    """ECMWF's GRIB generating-process identifier(s), which change with the IFS cycle."""
    ids = sorted({f.process_id for f in fields if f.process_id is not None})
    return "ecmwf-gpi-" + "/".join(str(i) for i in ids) if ids else "unknown"


# --- open data ---------------------------------------------------------------


def opendata_client(source: str):
    from ecmwf.opendata import Client

    return Client(source=source, model="ifs", resol="0p25")


def opendata_url(source: str, init: datetime) -> str:
    from ecmwf.opendata.urls import URLS

    return f"{URLS[source]}/{init:%Y%m%d}/{init:%H}z/ifs/0p25/"


def fetch_opendata(init: datetime, *, repo: Path, cache_dir: Path, source: str = "ecmwf",
                   box: forecasts.Box | None = None) -> dict:
    """One open-data ENS run as a normalised record. Raises on any missing step or member."""
    guard(init, repo)
    box = box or ARCHIVE_BOX
    client = opendata_client(source)
    workdir = Path(cache_dir) / "opendata" / f"{init:%Y%m%d%H}"
    workdir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    fields: list[Field] = []
    for step in OPENDATA_STEPS:
        for stream, type_ in (("oper", "fc"), ("enfo", "pf")):
            target = workdir / f"{stream}-{type_}-{step:03d}.grib2"
            try:
                client.retrieve(date=f"{init:%Y%m%d}", time=init.hour, stream=stream, type=type_,
                                param="tp", step=step, target=str(target))
                with open(target, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        digest.update(chunk)
                fields += [forecasts.crop(f, box) for f in decode_grib(target)]
            except forecasts.RecordError:
                raise
            except Exception as e:
                raise forecasts.IncompleteRunError(
                    f"open-data run {init:%Y-%m-%dT%HZ} step {step} h {stream}/{type_}: {e}"
                ) from e
            finally:
                target.unlink(missing_ok=True)
    _remove_empty(workdir)
    return forecasts.build_record(
        fields, source="opendata", init=init, steps=OPENDATA_STEPS, perturbed=OPENDATA_PERTURBED,
        model_cycle=_model_cycle(fields), source_url=opendata_url(source, init),
        raw_sha256=digest.hexdigest(), box=box,
    )


def _remove_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


# --- TIGGE -------------------------------------------------------------------


def tigge_client():
    import cdsapi

    return cdsapi.Client()  # url and key from ~/.cdsapirc


def tigge_requests(init: datetime, box: forecasts.Box) -> list[dict]:
    lats, lons = forecasts.box_cells(box)
    base = {
        "class": "ti",
        "expver": "prod",
        "origin": "ecmf",
        "levtype": "sfc",
        "param": "228228",
        "date": f"{init:%Y-%m-%d}",
        "time": f"{init:%H}:00:00",
        "step": "/".join(str(s) for s in TIGGE_STEPS),
        "grid": f"{forecasts.LATTICE}/{forecasts.LATTICE}",
        "area": f"{lats[-1]}/{lons[0]}/{lats[0]}/{lons[-1]}",
    }
    numbers = "/".join(str(n) for n in TIGGE_PERTURBED)
    return [{**base, "type": "cf"}, {**base, "type": "pf", "number": numbers}]


def fetch_tigge(init: datetime, *, repo: Path, cache_dir: Path,
                box: forecasts.Box | None = None) -> dict:
    """One TIGGE ECMWF ENS run as a normalised record. Raw GRIB stays in the cache."""
    guard(init, repo)
    box = box or ARCHIVE_BOX
    client = tigge_client()
    workdir = Path(cache_dir) / "tigge"
    workdir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    fields: list[Field] = []
    for request in tigge_requests(init, box):
        target = workdir / f"{init:%Y%m%d%H}-{request['type']}.grib2"
        client.retrieve(TIGGE_DATASET, request, str(target))
        digest.update(target.read_bytes())
        fields += [forecasts.crop(f, box) for f in decode_grib(target)]
    return forecasts.build_record(
        fields, source="tigge", init=init, steps=TIGGE_STEPS, perturbed=TIGGE_PERTURBED,
        model_cycle=_model_cycle(fields), source_url=f"{TIGGE_URL}#{TIGGE_DATASET}",
        raw_sha256=digest.hexdigest(), box=box,
    )


# --- daily archiving ---------------------------------------------------------


def in_archive_season(day: date) -> bool:
    (m0, d0), (m1, d1) = ARCHIVE_SEASON
    return date(day.year, m0, d0) <= day <= date(day.year, m1, d1)


def archive(day: date, hours=RUN_HOURS, *, repo: Path, source: str = "ecmwf") -> int:
    """Save each open-data run of ``day`` not yet in the ledger. Returns an exit status.

    Init times already saved are skipped before any download; a saved file that
    does not decode is reported and left alone (write-once). A run that fails
    (missing step, network error) is reported on stderr and makes the status 1;
    the other runs are still archived.
    """
    if not in_archive_season(day):
        print(f"evidence archive: {day} is outside the 1 Oct–30 Nov archive season; nothing to do")
        return 0
    repo = Path(repo)
    root, cache = repo / "ledger" / LEDGER, repo / ".cache" / "evidence"
    status = 0
    for hour in hours:
        init = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
        label = f"{init:%Y-%m-%dT%H:%M:%SZ}"
        rel = forecasts.record_path("opendata", init)
        if (root / rel).exists():
            try:
                forecasts.decode_record((root / rel).read_bytes())
            except forecasts.RecordError as e:  # write-once: report, never overwrite
                print(f"evidence archive: {label} saved as {rel} is unreadable: {e}", file=sys.stderr)
                status = 1
                continue
            print(f"evidence archive: {label} already saved as {rel}; skipped")
            continue
        try:
            record = fetch_opendata(init, repo=repo, cache_dir=cache, source=source)
            rel, _ = forecasts.write_record(root, record)
        except Exception as e:  # report and carry on with the other run
            print(f"evidence archive: {label} failed: {e}", file=sys.stderr)
            status = 1
            continue
        print(f"evidence archive: {label} saved as {rel}")
    return status
