# HealthLabs nutrition sources

HealthLabs imports only local, checksum-pinned source files. The importer never downloads a URL, so a mutable upstream file cannot change an existing release silently.

## Approved source releases

| Provider | Pinned release | Country | Source | Licence and required attribution |
|---|---|---|---|---|
| `usda` | FoodData Central Foundation Foods 04/2026 | `OTHER` | [USDA downloads](https://fdc.nal.usda.gov/download-datasets/) | [CC0 1.0](https://fdc.nal.usda.gov/api-guide/); attribute “U.S. Department of Agriculture, Agricultural Research Service. FoodData Central.” |
| `cofid` | CoFID 2021 | `GB` | [GOV.UK release](https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid) | [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/); attribute Public Health England and McCance and Widdowson’s CoFID 2021. |
| `ciqual` | ANSES-CIQUAL 2025 | `FR` | [ANSES release](https://ciqual.anses.fr/cms/en/2025-anses-ciqual-table) | [Licence Ouverte](https://www.etalab.gouv.fr/licence-ouverte-open-licence/); attribute “Anses. 2025. Table de composition nutritionnelle des aliments Ciqual 2025”, DOI `10.57745/RDMHWY`. |
| `bls` | Bundeslebensmittelschlüssel 4.0 (2025) | `DE` | [BLS download](https://blsdb.prod.se.ble.de/bls) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); attribute Max Rubner-Institut (2025), DOI `10.25826/Data20251217-134202-0`. |

Poland is blocked: the NIZP PZH full database is supplied under a purchased licence agreement, so it is not imported until HealthLabs records redistribution-compatible terms. Ukraine has no verified national open food-composition release in this policy; Ukrainian aliases may reference an approved source while retaining that source’s country and provenance.

## Accepted files

- USDA: the unzipped FoodData Central Foundation/FNDDS JSON document.
- CoFID: UTF-8 CSV exported from the published workbook, preserving the published column headers.
- CIQUAL: UTF-8 CSV exported from the official workbook's `composition nutritionnelle` sheet.
- BLS: UTF-8 CSV exported from the published workbook, preserving BLS component codes.

Values such as `<0.5`, `trace`, blank, or `-` import as unknown (`null`), never zero. Original nutrient columns are retained under `_raw`; the canonical calculation keys are `kcal`, `protein_g`, `fibre_g`, `carbs_g`, `fat_g`, and `water_ml`.

## Manifest and import

Compute the checksum from the exact unzipped JSON or CSV passed to the importer:

```sh
shasum -a 256 /absolute/path/source.csv
```

Create a manifest whose version and URLs match the table above:

```json
{
  "provider": "cofid",
  "version": "2021",
  "country": "GB",
  "sourceUrl": "https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid",
  "licenseUrl": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
  "attribution": "Public Health England; McCance and Widdowson's CoFID 2021",
  "sha256": "<64 lowercase hex characters>"
}
```

Run from the repository root:

```sh
uv run python scripts/healthlabs/import-nutrition-source.py manifest.json /absolute/path/source.csv
```

The command verifies the bytes before parsing. Reusing a provider/version/country with different bytes fails. Re-importing identical bytes updates the same source-food rows and keeps reviewed Mealie food matches attached to their existing source-food IDs.
Source-managed portions are refreshed while nutrition-editor portions remain unchanged.

Run exact/alias matching and produce an aggregate-only coverage report:

```sh
uv run python scripts/healthlabs/backfill-food-matches.py --country GB
uv run python scripts/healthlabs/report-nutrition-coverage.py --external-subject-id <subject-uuid>
```

Structure legacy free-text ingredient rows after the group food and unit
catalogues exist. The first command is a dry run; review its aggregate counts
before applying the same exact-only pass:

```sh
uv run python scripts/healthlabs/backfill-ingredient-structure.py
uv run python scripts/healthlabs/backfill-ingredient-structure.py --apply
```

The structure backfill writes only when a positive quantity, unit, and food all
resolve to unique existing Mealie records. It preserves the source text, rejects
fuzzy and ambiguous names, and never creates foods, units, nutrient values, or
gram conversions. Existing mass metadata and source-backed food portions remain
the only conversion inputs.

New automatic matches always require review. Ambiguous and fuzzy candidates remain unknown. The report contains counts and source provenance only; it excludes user identifiers, recipe text, and nutrient values.

## Restored-data verification — 2026-10-17

The four pinned inputs imported 13,874 authoritative food rows:

| Provider | Version | Imported foods | SHA-256 |
|---|---:|---:|---|
| `usda` | `2026-04` | 363 | `27d1fe3fd89edfbe528ed915da5619320e1d004d4594603a1b19bdb1511590cc` |
| `cofid` | `2021` | 2,887 | `7064c92d619b95a9db38f42258a18bfa23d2f524ae5422836c43a29045065f05` |
| `ciqual` | `2025` | 3,484 | `860d9d9820fa0944062a2027aa25c83f48db07342d6a4fe726aada4364eebf5b` |
| `bls` | `4.0-2025` | 7,140 | `7e6f3deacb6c5e495085b40b9b70d39a60191526783e73f25e10ec9f0e580156` |

On a fresh copy of the running DEV database, the target workspace contained 5 recipes, 33 distinct foods, and 36 structured ingredient rows. Its classification was 0 reviewed matches, 3 review-required exact/alias candidates, and 30 unknown foods; a second global backfill created 0 matches, and the report found 0 mock source releases.

The full migrated catalogue restore contained 2,736 recipes, 315 distinct foods, and 27,828 ingredient rows. Food classification was 0 reviewed matches, 12 review-required exact/alias candidates, and 303 unknown foods. Ingredient coverage was 4,788 structured rows (0 matched, 369 review-required, 4,419 unknown) plus 23,040 explicitly unparsed rows. A second backfill created 0 matches, and the report found 0 mock source releases.
