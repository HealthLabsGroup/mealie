# HealthLabs nutrition sources

HealthLabs imports only local, checksum-pinned source files. The importer never downloads a URL, so a mutable upstream file cannot change an existing release silently.

## Approved source releases

| Provider | Pinned release | Country | Source | Licence and required attribution |
|---|---|---|---|---|
| `usda` | FoodData Central Foundation Foods 04/2026 | `OTHER` | [USDA downloads](https://fdc.nal.usda.gov/download-datasets/) | [CC0 1.0](https://fdc.nal.usda.gov/api-guide/); attribute “U.S. Department of Agriculture, Agricultural Research Service. FoodData Central.” |
| `usda` | FoodData Central Survey (FNDDS) 2021–2023, published 2024-10-31 | `OTHER` | [USDA archive](https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_survey_food_json_2024-10-31.zip) | [CC0 1.0](https://fdc.nal.usda.gov/api-guide/); attribute “U.S. Department of Agriculture, Agricultural Research Service. FoodData Central.” |
| `cofid` | CoFID 2021 | `GB` | [GOV.UK release](https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid) | [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/); attribute Public Health England and McCance and Widdowson’s CoFID 2021. |
| `ciqual` | ANSES-CIQUAL 2025 | `FR` | [ANSES release](https://ciqual.anses.fr/cms/en/2025-anses-ciqual-table) | [Licence Ouverte](https://www.etalab.gouv.fr/licence-ouverte-open-licence/); attribute “Anses. 2025. Table de composition nutritionnelle des aliments Ciqual 2025”, DOI `10.57745/RDMHWY`. |
| `bls` | Bundeslebensmittelschlüssel 4.0 (2025) | `DE` | [BLS download](https://blsdb.prod.se.ble.de/bls) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); attribute Max Rubner-Institut (2025), DOI `10.25826/Data20251217-134202-0`. |
| `fao_infoods_density` | FAO/INFOODS Density Database 2.0 (2012) | `OTHER` | [FAO release](https://www.fao.org/food-composition/tables-and-databases/detail/%28global--2012%29-fao-infoods-density-database---version-2/en) | [FAO Statistical Database Terms](https://www.fao.org/contact-us/terms/db-terms-of-use/en); requested commercial storage, transformation, and API-derived use confirmed by phone on 2026-10-19. Attribute “FAO. 2012. FAO/INFOODS Density Database. Version 2.0. Rome, FAO.” |

Poland is blocked: the NIZP PZH full database is supplied under a purchased licence agreement, so it is not imported until HealthLabs records redistribution-compatible terms. Ukraine has no verified national open food-composition release in this policy; Ukrainian aliases may reference an approved source while retaining that source’s country and provenance.

USDA releases use the country-neutral `OTHER` selection tier so they can act as a
fallback when the requested national catalogue has no match. Their US provenance
remains explicit in the provider, release name, source URL, and attribution.

## Accepted files

- USDA: the unzipped FoodData Central Foundation/FNDDS JSON document.
- CoFID: UTF-8 CSV exported from the published workbook, preserving the published column headers.
- CIQUAL: UTF-8 CSV exported from the official workbook's `composition nutritionnelle` sheet.
- BLS: UTF-8 CSV exported from the published workbook, preserving BLS component codes.
- FAO/INFOODS density: the original version 2.0 XLSX workbook. The importer
  stores the 500 positive scalar `g/ml` rows; ranges and specific-gravity-only
  rows are excluded from conversion evidence.

Values such as `<0.5`, `trace`, blank, or `-` import as unknown (`null`), never zero. Original nutrient columns are retained under `_raw`; the canonical calculation keys are `kcal`, `protein_g`, `fibre_g`, `carbs_g`, `fat_g`, and `water_ml`.

## Manifest and import

Compute the checksum from the exact JSON, CSV, or XLSX passed to the importer:

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

For `fao_infoods_density`, use version `2.0`, country `OTHER`, and SHA-256
`329caaecbaa2031a73dd50c32e0cee8453d85655342829d80a3cd92053854227`.
The importer preserves the worksheet row, original food description, scalar
density, and bibliography ID. It creates a source-managed `1 ml` portion only
when the density name is unique and exactly matches an existing nutrient-food
name. Ambiguous density rows remain stored without an automatic link, and
density-derived portions survive ordinary nutrient-source reimports.

Run exact/alias matching and produce an aggregate-only coverage report:

```sh
uv run python scripts/healthlabs/backfill-food-matches.py --country GB
uv run python scripts/healthlabs/report-nutrition-coverage.py --external-subject-id <subject-uuid>
```

New recipe creates, updates, and patches structure free-text ingredients inside Mealie's
running recipe service after the group food and unit catalogues exist. The same
path is used by the Mealie UI, imports, and the HealthLabs API.

For rows saved before this behavior existed, the first command below is a dry
run; review its aggregate counts before applying the same exact-only pass:

```sh
uv run python scripts/healthlabs/backfill-ingredient-structure.py
uv run python scripts/healthlabs/backfill-ingredient-structure.py --apply
```

Runtime structuring and the legacy backfill write only when a positive quantity,
unit, and food all resolve to unique existing Mealie records. They preserve the
source text, reject fuzzy and ambiguous names, and never create foods, units,
nutrient values, or gram conversions. Existing mass metadata and source-backed
food portions remain the only conversion inputs.

The running service performs the same deterministic checks every five minutes.
A unique normalized-name or curated-alias result from the requested country or
`OTHER` source is applied with `auto_applied=true`. It also discovers ambiguous
or missing ingredient, source-match, and portion-conversion work and keeps that
group-scoped progress across restarts. The scripts remain diagnostic and
recovery tools; normal operation does not require an operator to run them.

Apple, the managed nutrition-curator agent, may resolve only a bounded list of
existing catalogue/source/portion IDs. Apple cannot create foods, source truth,
nutrient values, or grams. Low confidence, a narrow winner margin, incompatible
country/preparation, missing candidates, stale data, or an invalid provider
selection records immutable `insufficient_evidence` and performs no write.
Malformed or schema-invalid provider output instead follows the 5/30-minute
provider-failure retry policy and can become `failed` on attempt three.
Accepted source matches use the ordinary reviewed-match contract with Apple as
the `user_type=agent` reviewer; source releases and existing authoritative
portions remain the source of all nutrition values and conversions.

Apple runs separately with the same image/database/configuration:

```sh
mealie-nutrition-curator
```

It defaults off. Enable it only after configuring a valid group provider/model,
positive daily request and USD limits, and a positive input or output token
rate. Before enabling a new prompt/model pair, compare a representative live
golden set using Apple and the policy-only control and require identical
validated selections/abstentions. See
[`nutrition-service-v1.md`](nutrition-service-v1.md#background-reconciliation)
for retry, shutdown, monitoring, rollback, and persona-version details.

Ambiguous candidates remain unknown unless Apple or a human break-glass editor
accepts a defensible source-backed selection. Coverage reports contain counts
and source provenance only; they exclude user identifiers, recipe text, and
nutrient values.

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

## FNDDS parser verification — 2026-10-19

The unzipped `surveyDownload.json` from the approved 2024-10-31 archive has
SHA-256 `2e7eb9fda92adf1d4d784dba5eaa3a7fd4418cd86ccff383c7c9294d79e9b808`.
The importer reads 5,432 foods and 16,468 positive numeric described portions.
Non-numeric guidance such as `Quantity not specified` is intentionally not used
as a gram conversion.

## FAO/INFOODS density parser verification — 2026-10-19

The official version 2.0 XLSX has SHA-256
`329caaecbaa2031a73dd50c32e0cee8453d85655342829d80a3cd92053854227`.
The importer accepts 500 positive scalar `g/ml` rows. Of those, 476 have unique
normalized names and 24 belong to 11 ambiguous name groups. The ambiguous rows,
34 density ranges, and 104 specific-gravity-only rows never create automatic
gram conversions.
