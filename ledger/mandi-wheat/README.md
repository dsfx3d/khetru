# Mandi wheat claim ledger

Append-only record of khetru's sow-or-wait rain claims for rainfed wheat in
Mandi district. `uv run --all-packages evidence verify --base <ref>` checks
that the ledger only grew since `<ref>`; CI runs it on every push and PR
(`.github/workflows/evidence-verify.yml`).

## Repository rules (owner action)

The owner sets up a GitHub ruleset on `github.com/dsfx3d/khetru`
(Settings → Rules → Rulesets); it cannot be set from code:

- Branch ruleset targeting `main`: block force pushes and block deletion.
- Tag ruleset targeting `mandi-wheat/*`: restrict updates and restrict
  deletions, so a bundle tag can never be moved or removed (R14).

`verify` also fails when a tag recorded in `tags.jsonl` no longer resolves to
its recorded commit, so a moved tag is caught even without the ruleset.

## Forecast inputs

`inputs/ens-<source>-<YYYYMMDDHH>.json.gz` holds one ECMWF ENS run each
(`source` is `opendata` or `tigge`): gzip-compressed canonical JSON with the
control and every perturbed member kept apart, cumulative precipitation in mm at
every native step from 0 to 360 h, on IMD 0.25° lattice cells covering the
Mandi box plus one cell of margin, and the run's init time, model cycle, source
URL and raw-GRIB SHA-256. Window totals are not stored; rebuild them with
`khetru_evidence.forecasts.window_totals`. Files are write-once: `verify`
fails if a committed one changes. The daily `evidence-archive` workflow saves
the 00 and 12 UTC open-data runs from 1 Oct to 30 Nov.

## Band map

`bands/band-map.csv` lists every IMD 0.25° lattice cell holding part of Mandi
district: the cell's share of the district's area (`weight`) and the verdict
band it reports to. There is one verdict band, `district`, because no
independent gauges stand behind separate bands (R17). Elevation zones are not
registered; `docs/findings/2026-10-mandi-elevation-zones.md` says why. `khetru_evidence.bands.band_mean` turns a forecast or
observed field into the band value with these weights and one coverage rule.
`bands/PROVENANCE.md` names the sources, their hashes and the method;
`uv run --all-packages evidence bands build` (needs `uv sync --all-extras`)
rebuilds the map and fails unless it matches the committed file byte for byte.

## Data attribution

Forecast data: ECMWF, licensed under Creative Commons Attribution 4.0
International (CC BY 4.0), https://creativecommons.org/licenses/by/4.0/.
Open-data runs come from ECMWF open data (https://www.ecmwf.int/en/forecasts/datasets/open-data);
hindcast runs from the TIGGE archive via the ECMWF Data Store, ECMWF fields only
(CC BY 4.0). The files here are cropped and converted (metres or kg m-2 to mm,
rounded to 0.001 mm); ECMWF does not endorse this use.

District boundary: geoBoundaries gbOpen India ADM2 (https://www.geoboundaries.org),
Open Database License 1.0. Elevation: Copernicus DEM GLO-30, produced using
Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH
2014-2018, provided under COPERNICUS by the European Union and ESA; all rights
reserved.
