"""U5: normalised ENS records, the shared date guard and daily archiving.

GRIB fixtures are real GRIB2 bytes written with eccodes and decoded through the
same path the adapters use. Only the network clients are faked.
"""

import gzip
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import eccodes
import numpy as np
import pytest

from khetru_evidence import cli
from khetru_evidence import fetch_forecasts as ff
from khetru_evidence import forecasts as fc
from khetru_evidence import ledger as L

INIT = datetime(2026, 10, 8, 0, tzinfo=UTC)
STEPS = (0, 6, 12, 18, 24)
# A 2x2-cell box: its extent falls in cells 31.5/31.75 N and 76.75/77.0 E.
BOX = fc.Box(south=31.4, north=31.8, west=76.7, east=77.1)
# Fixture grid: 6x6 lattice cells, wider than the box plus one cell of margin.
GRID_LATS = [32.25, 32.0, 31.75, 31.5, 31.25, 31.0]  # north to south, as GRIB scans
GRID_LONS = [76.25, 76.5, 76.75, 77.0, 77.25, 77.5]


# --- fixture GRIB ------------------------------------------------------------


def _increments(member: int) -> np.ndarray:
    """Deterministic 6-hourly rain increments in metres, shape (step, lat, lon)."""
    rng = np.random.default_rng(1000 + member)
    inc = rng.uniform(0.0, 0.004, size=(len(STEPS), len(GRID_LATS), len(GRID_LONS)))
    inc[0] = 0.0  # nothing has accumulated at step 0
    return inc


def write_grib(path: Path, messages, *, init=INIT, lats=GRID_LATS, lons=GRID_LONS) -> Path:
    """Write GRIB2 tp messages; each message is (data_type, number, step, units, values)."""
    with open(path, "wb") as f:
        for data_type, number, step, units, values in messages:
            h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            eccodes.codes_set(h, "centre", "ecmf")
            eccodes.codes_set(h, "dataDate", int(init.strftime("%Y%m%d")))
            eccodes.codes_set(h, "dataTime", init.hour * 100)
            eccodes.codes_set(h, "Ni", len(lons))
            eccodes.codes_set(h, "Nj", len(lats))
            eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", lats[0])
            eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", lats[-1])
            eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", lons[0])
            eccodes.codes_set(h, "longitudeOfLastGridPointInDegrees", lons[-1])
            eccodes.codes_set(h, "iDirectionIncrementInDegrees", abs(lons[1] - lons[0]))
            eccodes.codes_set(h, "jDirectionIncrementInDegrees", abs(lats[0] - lats[1]))
            eccodes.codes_set(h, "productDefinitionTemplateNumber", 11)
            eccodes.codes_set(h, "paramId", 228 if units == "m" else 228228)
            if data_type == "fc":
                eccodes.codes_set(h, "productDefinitionTemplateNumber", 8)
                eccodes.codes_set(h, "typeOfProcessedData", 1)
            else:
                eccodes.codes_set(h, "typeOfProcessedData", 3 if data_type == "cf" else 4)
                eccodes.codes_set(h, "perturbationNumber", number)
            eccodes.codes_set(h, "stepRange", "0" if step == 0 else f"0-{step}")
            eccodes.codes_set(h, "bitsPerValue", 24)
            eccodes.codes_set_values(h, np.asarray(values, dtype=float).ravel())
            eccodes.codes_write(h, f)
            eccodes.codes_release(h)
    return path


def ens_messages(*, steps=STEPS, units="m", control_type="cf"):
    """Control (number 0) and one perturbed member (number 1), cumulative tp."""
    factor = 1.0 if units == "m" else 1000.0
    messages = []
    for data_type, number in ((control_type, 0), ("pf", 1)):
        cumulative = np.cumsum(_increments(number), axis=0) * factor
        for i, step in enumerate(STEPS):
            if step in steps:
                messages.append((data_type, number, step, units, cumulative[i]))
    return messages


def build(fields, **overrides):
    kwargs = dict(
        source="opendata", init=INIT, steps=STEPS, perturbed=(1,), model_cycle="ecmwf-gpi-161",
        source_url="https://example.invalid/enfo/", raw_sha256="0" * 64, box=BOX,
    )
    kwargs.update(overrides)
    return fc.build_record(fields, **kwargs)


def record_from_grib(tmp_path, messages, **overrides):
    path = write_grib(tmp_path / "fixture.grib2", messages)
    return build(list(ff.decode_grib(path)), **overrides)


# --- the record --------------------------------------------------------------


def test_window_totals_from_saved_record_match_direct_sum_of_grib(tmp_path):
    record = record_from_grib(tmp_path, ens_messages())
    saved = fc.decode_record(fc.encode_record(record))

    # Box plus one cell of margin: lats 31.25..32.0, lons 76.5..77.25 (rows/cols 1..4).
    assert saved["lats"] == [31.25, 31.5, 31.75, 32.0]
    assert saved["lons"] == [76.5, 76.75, 77.0, 77.25]
    rows = [GRID_LATS.index(lat) for lat in saved["lats"]]
    cols = [GRID_LONS.index(lon) for lon in saved["lons"]]
    assert saved["units"] == "mm"
    assert not any("total" in key for key in saved)  # window totals are never stored

    for start, end in ((0, 24), (6, 18), (12, 24), (0, 6), (18, 24)):
        totals = fc.window_totals(saved, start, end)
        assert totals.shape == (2, 4, 4)
        for m, number in enumerate((0, 1)):  # control first, then perturbed
            inc = _increments(number)
            i0, i1 = STEPS.index(start), STEPS.index(end)
            direct = inc[i0 + 1 : i1 + 1].sum(axis=0)[np.ix_(rows, cols)] * 1000.0
            np.testing.assert_allclose(totals[m], direct, atol=2e-3)


def test_opendata_metres_and_tigge_kg_per_m2_give_the_same_mm():
    lats, lons = [31.75, 31.5], [76.75, 77.0]

    def fields(units, value):
        return [
            fc.Field(member=kind, number=num, init=INIT, step=step, units=units,
                     lats=lats, lons=lons, values=np.full((2, 2), value * step / 24))
            for kind, num in (("control", 0), ("perturbed", 1)) for step in STEPS
        ]

    no_margin = dict(box=fc.Box(31.5, 31.75, 76.75, 77.0), margin=0)
    from_metres = build(fields("m", 0.0123), **no_margin)
    from_kg = build(fields("kg m**-2", 12.3), source="tigge", **no_margin)

    np.testing.assert_array_equal(
        fc.window_totals(from_metres, 0, 24), fc.window_totals(from_kg, 0, 24)
    )
    assert fc.window_totals(from_kg, 0, 24)[0, 0, 0] == pytest.approx(12.3)
    assert from_metres["control"] == from_kg["control"]


def test_unknown_units_are_refused():
    field = fc.Field(member="control", number=0, init=INIT, step=0, units="K",
                     lats=[31.5], lons=[76.75], values=np.zeros((1, 1)))
    with pytest.raises(fc.RecordError, match="units"):
        build([field], steps=(0,), perturbed=(), box=fc.Box(31.5, 31.5, 76.75, 76.75), margin=0)


def test_control_kept_separate_from_perturbed_and_n_counts_both(tmp_path):
    record = record_from_grib(tmp_path, ens_messages())
    assert np.asarray(record["control"]).shape == (len(STEPS), 4, 4)
    assert sorted(record["perturbed"]) == ["1"]
    assert record["control"] != record["perturbed"]["1"]
    assert fc.member_labels(record) == ["control", "1"]
    assert fc.member_count(record) == 2


def test_opendata_control_from_oper_fc_is_the_control(tmp_path):
    # Since IFS 50r1 the open-data ENS control is published as stream=oper, type=fc.
    record = record_from_grib(tmp_path, ens_messages(control_type="fc"))
    assert fc.member_count(record) == 2
    assert np.asarray(record["control"]).shape == (len(STEPS), 4, 4)


def test_record_without_control_is_refused(tmp_path):
    messages = [m for m in ens_messages() if m[0] == "pf"]
    with pytest.raises(fc.IncompleteRunError, match="control"):
        record_from_grib(tmp_path, messages)


def test_off_lattice_grid_is_rejected(tmp_path):
    shifted_lons = [lon + 0.1 for lon in GRID_LONS]
    path = write_grib(tmp_path / "off.grib2", ens_messages(), lons=shifted_lons)
    with pytest.raises(fc.LatticeError):
        build(list(ff.decode_grib(path)))


def test_off_lattice_record_is_rejected(tmp_path):
    record = record_from_grib(tmp_path, ens_messages())
    record["lons"][0] = 76.6
    with pytest.raises(fc.LatticeError):
        fc.validate_record(record)
    with pytest.raises(fc.LatticeError):
        fc.encode_record(record)
    tampered = gzip.compress(json.dumps(record).encode(), mtime=0)
    with pytest.raises(fc.LatticeError):
        fc.decode_record(tampered)


def test_missing_step_is_a_failure_not_a_short_series(tmp_path):
    messages = ens_messages(steps=(0, 6, 18, 24))  # step 12 missing for both members
    with pytest.raises(fc.IncompleteRunError, match="12"):
        record_from_grib(tmp_path, messages)


def test_missing_perturbed_member_is_a_failure(tmp_path):
    with pytest.raises(fc.IncompleteRunError, match="2"):
        record_from_grib(tmp_path, ens_messages(), perturbed=(1, 2))


def test_mandi_box_cells_are_on_the_lattice_with_one_cell_margin():
    lats, lons = fc.box_cells(fc.MANDI_BOX)
    assert lats == [31.0, 31.25, 31.5, 31.75, 32.0, 32.25]
    assert lons == [76.25, 76.5, 76.75, 77.0, 77.25, 77.5, 77.75]


# --- write-once file ---------------------------------------------------------


def test_writing_the_same_record_twice_leaves_one_identical_file(tmp_path):
    record = record_from_grib(tmp_path, ens_messages())
    root = tmp_path / "ledger" / "mandi-wheat"
    rel, written = fc.write_record(root, record)
    first = (root / rel).read_bytes()
    again, written_again = fc.write_record(root, record)

    assert (written, written_again) == (True, False)
    assert again == rel == "inputs/ens-opendata-2026100800.json.gz"
    assert (root / rel).read_bytes() == first
    assert [p.name for p in (root / "inputs").iterdir()] == ["ens-opendata-2026100800.json.gz"]
    assert fc.decode_record(first) == record


def test_a_different_record_for_a_saved_init_is_refused(tmp_path):
    record = record_from_grib(tmp_path, ens_messages())
    root = tmp_path / "ledger" / "mandi-wheat"
    rel, _ = fc.write_record(root, record)
    before = (root / rel).read_bytes()
    with pytest.raises(fc.RecordError, match="write-once"):
        fc.write_record(root, {**record, "raw_sha256": "1" * 64})
    assert (root / rel).read_bytes() == before


def test_a_write_that_fails_midway_leaves_no_file(tmp_path, monkeypatch):
    record = record_from_grib(tmp_path, ens_messages())
    root = tmp_path / "ledger" / "mandi-wheat"

    class DiskFull:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, data):
            self.f.write(data[: len(data) // 2])
            raise OSError(28, "No space left on device")

        def __getattr__(self, name):
            return getattr(self.f, name)

    monkeypatch.setattr(fc, "open", lambda *a, **k: DiskFull(open(*a, **k)), raising=False)
    with pytest.raises(OSError, match="No space"):
        fc.write_record(root, record)

    assert list((root / "inputs").iterdir()) == []


# --- the shared date guard ---------------------------------------------------


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "tag.gpgsign", "false")
    (repo / "README").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


class FakeTigge:
    def __init__(self):
        self.requests = []

    def retrieve(self, dataset, request, target):
        self.requests.append((dataset, request))
        raise AssertionError("the guard must refuse before any request")


class FakeOpenData:
    """Serves fixture GRIB bytes per (stream, type, step); records every request."""

    def __init__(self, missing_steps=()):
        self.requests = []
        self.missing_steps = set(missing_steps)

    def retrieve(self, *, date, time, stream, type, param, step, target, **_):
        self.requests.append(dict(date=date, time=time, stream=stream, type=type, step=step))
        if step in self.missing_steps:
            raise ValueError(f"No index entries for step={step}")
        control_type = "fc" if stream == "oper" else None
        messages = [
            m for m in ens_messages(control_type="fc")
            if m[2] == step and (m[0] == control_type or (stream == "enfo" and m[0] == type))
        ]
        init = datetime.strptime(f"{date}{time:02d}", "%Y%m%d%H").replace(tzinfo=UTC)
        write_grib(Path(target), messages, init=init)


@pytest.fixture
def no_network(monkeypatch):
    tigge, opendata = FakeTigge(), FakeOpenData()
    monkeypatch.setattr(ff, "tigge_client", lambda: tigge)
    monkeypatch.setattr(ff, "opendata_client", lambda source: opendata)
    return tigge, opendata


def test_pre_2026_oct_nov_tigge_request_without_tag_is_refused(repo, tmp_path, no_network):
    tigge, _ = no_network
    with pytest.raises(ff.ProtectedDateError):
        ff.fetch_tigge(datetime(2023, 10, 23, tzinfo=UTC), repo=repo, cache_dir=tmp_path / "c")
    assert tigge.requests == []


def test_2024_10_opendata_request_without_tag_is_refused(repo, tmp_path, no_network):
    _, opendata = no_network
    with pytest.raises(ff.ProtectedDateError):
        ff.fetch_opendata(
            datetime(2024, 10, 21, tzinfo=UTC), repo=repo, cache_dir=tmp_path / "c", source="aws"
        )
    assert opendata.requests == []


def test_run_whose_steps_reach_into_pre_2026_oct_is_refused(repo, tmp_path, no_network):
    # A 2025-09-25 run's 360 h steps cover 1-10 Oct 2025.
    with pytest.raises(ff.ProtectedDateError):
        ff.guard(datetime(2025, 9, 25, tzinfo=UTC), repo)
    ff.guard(datetime(2025, 7, 15, tzinfo=UTC), repo)  # outside Oct-Nov: allowed
    ff.guard(datetime(2026, 10, 15, tzinfo=UTC), repo)  # rabi 2026 development data: allowed


PROTECTED = datetime(2023, 10, 23, tzinfo=UTC)
TAG = "mandi-wheat/bundle-v1"


def add_origin(repo, tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(repo, "remote", "add", "origin", str(origin))


def record_run(repo, rel="hindcast/v1/runs.jsonl", **run):
    clock = lambda: datetime(2027, 8, 1, tzinfo=UTC)  # noqa: E731
    lg = L.Ledger(repo / "ledger" / "mandi-wheat", clock=clock)
    lg.append(rel, L.run(ledger="mandi-wheat", **{"bundle": "v1", "seq": 1, "status": "started", **run}))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "record run")


def test_guard_opens_only_with_bundle_tag_and_started_run_on_origin(repo, tmp_path):
    add_origin(repo, tmp_path)

    git(repo, "tag", "-a", TAG, "-m", "bundle v1")
    with pytest.raises(ff.ProtectedDateError):  # tag alone is not enough
        ff.guard(PROTECTED, repo)

    record_run(repo)
    with pytest.raises(ff.ProtectedDateError):  # started, but not on origin yet
        ff.guard(PROTECTED, repo)

    git(repo, "push", "-q", "origin", "main")
    with pytest.raises(ff.ProtectedDateError):  # run on origin, but the tag is local only
        ff.guard(PROTECTED, repo)

    git(repo, "push", "-q", "origin", TAG)
    ff.guard(PROTECTED, repo)


def test_guard_refuses_when_origin_branch_lacks_the_tagged_commit(repo, tmp_path):
    add_origin(repo, tmp_path)
    record_run(repo)
    git(repo, "push", "-q", "origin", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "bundle")
    git(repo, "tag", "-a", TAG, "-m", "bundle v1")  # tag is ahead of the pushed run
    git(repo, "push", "-q", "origin", TAG)
    with pytest.raises(ff.ProtectedDateError):
        ff.guard(PROTECTED, repo)


def test_guard_refuses_without_a_remote(repo):
    git(repo, "tag", "-a", TAG, "-m", "bundle v1")
    record_run(repo)
    with pytest.raises(ff.ProtectedDateError):
        ff.guard(PROTECTED, repo)


@pytest.mark.parametrize("rel, run", [
    ("hindcast/v1/runs.jsonl", dict(status="aborted")),
    ("hindcast/v1/runs.jsonl", dict(status="recorded")),
    ("hindcast/v2/runs.jsonl", dict(bundle="v2")),
], ids=["aborted", "recorded", "other-bundle"])
def test_guard_needs_a_started_run_of_the_tagged_bundle(repo, tmp_path, rel, run):
    add_origin(repo, tmp_path)
    git(repo, "tag", "-a", TAG, "-m", "bundle v1")
    record_run(repo, rel, **run)
    git(repo, "push", "-q", "origin", "main", TAG)
    with pytest.raises(ff.ProtectedDateError):
        ff.guard(PROTECTED, repo)


def test_guard_ignores_another_bundles_run_misfiled_under_the_tagged_bundle(repo, tmp_path):
    add_origin(repo, tmp_path)
    git(repo, "tag", "-a", TAG, "-m", "bundle v1")
    entry = {**L.run(ledger="mandi-wheat", bundle="v2", seq=1, status="started"),
             "recorded_at": "2027-08-01T00:00:00Z"}
    path = repo / "ledger" / "mandi-wheat" / "hindcast" / "v1" / "runs.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(L.encode(entry))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "misfiled run")
    git(repo, "push", "-q", "origin", "main", TAG)
    with pytest.raises(ff.ProtectedDateError):
        ff.guard(PROTECTED, repo)


def test_november_is_protected(repo):
    with pytest.raises(ff.ProtectedDateError):
        ff.guard(datetime(2023, 11, 20, tzinfo=UTC), repo)


def test_protection_starts_with_the_first_run_whose_last_step_reaches_october():
    assert ff.is_protected(datetime(2025, 9, 16, tzinfo=UTC))  # +360 h = 1 Oct 00Z
    assert not ff.is_protected(datetime(2025, 9, 15, tzinfo=UTC))  # +360 h = 30 Sep 00Z


# --- archiving ---------------------------------------------------------------


@pytest.fixture
def small_run(monkeypatch):
    """Shrink the open-data run to the fixture's steps, members and box."""
    monkeypatch.setattr(ff, "OPENDATA_STEPS", STEPS)
    monkeypatch.setattr(ff, "OPENDATA_PERTURBED", (1,))
    monkeypatch.setattr(ff, "ARCHIVE_BOX", BOX)


def test_archiving_the_same_init_twice_writes_one_file(repo, tmp_path, no_network, small_run):
    _, opendata = no_network
    root = repo / "ledger" / "mandi-wheat"
    argv = ["archive", "--date", "2026-10-08", "--time", "0", "--repo", str(repo)]

    assert cli.main(argv) == 0
    files = sorted((root / "inputs").iterdir())
    assert [p.name for p in files] == ["ens-opendata-2026100800.json.gz"]
    first = files[0].read_bytes()
    record = fc.decode_record(first)
    assert record["source"] == "opendata"
    assert record["init"] == "2026-10-08T00:00:00Z"
    assert fc.member_count(record) == 2
    assert len(record["raw_sha256"]) == 64
    assert record["source_url"].startswith("https://data.ecmwf.int/forecasts/20261008/00z/")
    calls = len(opendata.requests)

    assert cli.main(argv) == 0
    assert sorted((root / "inputs").iterdir()) == files
    assert files[0].read_bytes() == first
    assert len(opendata.requests) == calls  # skipped before any download
    assert not list((repo / ".cache" / "evidence").rglob("*.grib2"))  # raw files cleaned up


def test_archive_records_a_missing_step_as_failure(repo, tmp_path, monkeypatch, small_run, capsys):
    opendata = FakeOpenData(missing_steps={12})
    monkeypatch.setattr(ff, "opendata_client", lambda source: opendata)
    argv = ["archive", "--date", "2026-10-08", "--time", "0", "--repo", str(repo)]

    assert cli.main(argv) == 1
    assert "2026-10-08T00:00:00Z" in capsys.readouterr().err
    assert not (repo / "ledger" / "mandi-wheat" / "inputs").exists()


def test_archive_reports_an_unreadable_saved_record_and_leaves_it(repo, no_network, small_run, capsys):
    _, opendata = no_network
    path = repo / "ledger" / "mandi-wheat" / fc.record_path("opendata", INIT)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x1f\x8b truncated")
    argv = ["archive", "--date", "2026-10-08", "--time", "0", "--repo", str(repo)]

    assert cli.main(argv) == 1
    assert "2026-10-08T00:00:00Z" in capsys.readouterr().err
    assert path.read_bytes() == b"\x1f\x8b truncated"
    assert opendata.requests == []


def test_archive_skips_days_outside_1_oct_to_30_nov(repo, no_network, small_run, capsys):
    _, opendata = no_network
    argv = ["archive", "--date", "2026-12-01", "--repo", str(repo)]
    assert cli.main(argv) == 0
    assert opendata.requests == []
    assert "outside" in capsys.readouterr().out


def test_archive_defaults_to_both_runs_of_the_day(repo, no_network, small_run):
    assert cli.main(["archive", "--date", "2026-11-30", "--repo", str(repo)]) == 0
    names = sorted(p.name for p in (repo / "ledger" / "mandi-wheat" / "inputs").iterdir())
    assert names == ["ens-opendata-2026113000.json.gz", "ens-opendata-2026113012.json.gz"]


def test_archived_inputs_pass_verify_and_may_not_change(repo, no_network, small_run):
    assert cli.main(["archive", "--date", "2026-10-08", "--time", "0", "--repo", str(repo)]) == 0
    assert cli.verify(repo, "HEAD") == []  # a new inputs file is allowed
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "archive")
    path = next((repo / "ledger" / "mandi-wheat" / "inputs").iterdir())
    path.write_bytes(path.read_bytes() + b"x")
    assert any("write-once" in p for p in cli.verify(repo, "HEAD"))
